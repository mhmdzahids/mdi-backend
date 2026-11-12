"""
Router Manajemen Promosi Internal - Dimsum MES
Mengelola modul Voucher / Kupon Promo, Banner Promosi, dan Informasi Kunci API Buyer.
"""

import os
import io
import uuid
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from PIL import Image

from ..database import get_db
from ..models import Voucher, Banner, POSApiKey
from ..auth import get_current_user
from ..rbac import require_permission
from ..audit_logger import log_audit_event
from .products import trigger_buyer_sync

router = APIRouter(prefix="/promotions", tags=["Manajemen Promosi & Voucher"])

UPLOAD_BANNER_DIR = "uploads/banners"
os.makedirs(UPLOAD_BANNER_DIR, exist_ok=True)


# ==========================================
# PYDANTIC SCHEMAS - VOUCHER
# ==========================================

class VoucherResponse(BaseModel):
    id: str
    code: str
    discount_type: str
    discount_value: float
    min_spend: float
    max_discount: Optional[float] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    usage_limit: Optional[int] = None
    used_count: int
    is_active: bool
    is_public: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class VoucherCreateRequest(BaseModel):
    code: str = Field(..., min_length=3, max_length=50)
    discount_type: str = Field("fixed", pattern="^(fixed|percent)$")
    discount_value: float = Field(..., gt=0)
    min_spend: float = Field(0.0, ge=0)
    max_discount: Optional[float] = Field(None, ge=0)
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    usage_limit: Optional[int] = Field(None, ge=1)
    is_active: bool = True
    is_public: bool = True


class VoucherUpdateRequest(BaseModel):
    discount_type: Optional[str] = Field(None, pattern="^(fixed|percent)$")
    discount_value: Optional[float] = Field(None, gt=0)
    min_spend: Optional[float] = Field(None, ge=0)
    max_discount: Optional[float] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    usage_limit: Optional[int] = None
    is_active: Optional[bool] = None
    is_public: Optional[bool] = None


# ==========================================
# PYDANTIC SCHEMAS - BANNER
# ==========================================

class BannerResponse(BaseModel):
    id: str
    title: Optional[str] = None
    image_url: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class BannerCreateRequest(BaseModel):
    title: Optional[str] = None
    image_url: str
    is_active: bool = True


# ==========================================
# VOUCHER CRUD ENDPOINTS
# ==========================================

@router.get("/vouchers", response_model=List[VoucherResponse])
async def list_vouchers(
    search: Optional[str] = None,
    status_filter: Optional[str] = "all",  # all, active, inactive
    visibility: Optional[str] = "all",     # all, public, private
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_permission("promotions:view"))
):
    """
    Daftar seluruh kupon / voucher promosi untuk panel admin.
    """
    stmt = select(Voucher).order_by(Voucher.created_at.desc())

    if search:
        s = f"%{search.strip().upper()}%"
        stmt = stmt.where(Voucher.code.ilike(s))

    if status_filter == "active":
        stmt = stmt.where(Voucher.is_active == True)
    elif status_filter == "inactive":
        stmt = stmt.where(Voucher.is_active == False)

    if visibility == "public":
        stmt = stmt.where(Voucher.is_public == True)
    elif visibility == "private":
        stmt = stmt.where(Voucher.is_public == False)

    res = await db.execute(stmt)
    vouchers = res.scalars().all()

    return [
        VoucherResponse(
            id=v.id,
            code=v.code,
            discount_type=v.discount_type,
            discount_value=float(v.discount_value),
            min_spend=float(v.min_spend or 0.0),
            max_discount=float(v.max_discount) if v.max_discount is not None else None,
            start_date=v.start_date,
            end_date=v.end_date,
            usage_limit=v.usage_limit,
            used_count=v.used_count,
            is_active=v.is_active,
            is_public=v.is_public,
            created_at=v.created_at,
            updated_at=v.updated_at
        )
        for v in vouchers
    ]


@router.post("/vouchers", response_model=VoucherResponse)
async def create_voucher(
    payload: VoucherCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_permission("promotions:manage"))
):
    """
    Buat kupon / voucher promo baru.
    Kode otomatis diubah ke uppercase tanpa spasi.
    """
    code_clean = payload.code.strip().upper()

    # Cek duplikat kode
    check = await db.execute(select(Voucher).where(Voucher.code == code_clean))
    if check.scalar_one_or_none():
        raise HTTPException(
            status_code=400,
            detail=f"Kode kupon '{code_clean}' sudah digunakan. Harap gunakan kode unik lain."
        )

    if payload.discount_type == "percent" and payload.discount_value > 100:
        raise HTTPException(status_code=400, detail="Persentase diskon tidak boleh melebihi 100%.")

    new_v = Voucher(
        id=str(uuid.uuid4()),
        code=code_clean,
        discount_type=payload.discount_type,
        discount_value=payload.discount_value,
        min_spend=payload.min_spend,
        max_discount=payload.max_discount,
        start_date=payload.start_date,
        end_date=payload.end_date,
        usage_limit=payload.usage_limit,
        used_count=0,
        is_active=payload.is_active,
        is_public=payload.is_public
    )

    db.add(new_v)
    await db.commit()
    await db.refresh(new_v)

    await log_audit_event(
        db=db,
        aksi="Buat kupon",
        objek_tipe="VOUCHER",
        objek_id=new_v.id,
        ringkasan=f"Buat voucher baru '{new_v.code}' ({new_v.discount_type} {new_v.discount_value})",
        user_id=current_user.get("user_id"),
        nama_user=current_user.get("full_name") or current_user.get("username") or "ADMIN",
        request=request
    )
    trigger_buyer_sync()

    return VoucherResponse(
        id=new_v.id,
        code=new_v.code,
        discount_type=new_v.discount_type,
        discount_value=float(new_v.discount_value),
        min_spend=float(new_v.min_spend or 0.0),
        max_discount=float(new_v.max_discount) if new_v.max_discount is not None else None,
        start_date=new_v.start_date,
        end_date=new_v.end_date,
        usage_limit=new_v.usage_limit,
        used_count=new_v.used_count,
        is_active=new_v.is_active,
        is_public=new_v.is_public,
        created_at=new_v.created_at,
        updated_at=new_v.updated_at
    )


@router.put("/vouchers/{voucher_id}", response_model=VoucherResponse)
async def update_voucher(
    voucher_id: str,
    payload: VoucherUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_permission("promotions:manage"))
):
    """
    Perbarui data kupon / voucher promo.
    """
    res = await db.execute(select(Voucher).where(Voucher.id == voucher_id))
    voucher = res.scalar_one_or_none()
    if not voucher:
        raise HTTPException(status_code=404, detail="Kupon tidak ditemukan.")

    if payload.discount_type is not None:
        voucher.discount_type = payload.discount_type
    if payload.discount_value is not None:
        if voucher.discount_type == "percent" and payload.discount_value > 100:
            raise HTTPException(status_code=400, detail="Persentase diskon tidak boleh melebihi 100%.")
        voucher.discount_value = payload.discount_value
    if payload.min_spend is not None:
        voucher.min_spend = payload.min_spend
    if "max_discount" in payload.model_fields_set:
        voucher.max_discount = payload.max_discount
    if "start_date" in payload.model_fields_set:
        voucher.start_date = payload.start_date
    if "end_date" in payload.model_fields_set:
        voucher.end_date = payload.end_date
    if "usage_limit" in payload.model_fields_set:
        voucher.usage_limit = payload.usage_limit
    if payload.is_active is not None:
        voucher.is_active = payload.is_active
    if payload.is_public is not None:
        voucher.is_public = payload.is_public

    voucher.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(voucher)

    await log_audit_event(
        db=db,
        aksi="Ubah kupon",
        objek_tipe="VOUCHER",
        objek_id=voucher.id,
        ringkasan=f"Perbarui voucher '{voucher.code}'",
        user_id=current_user.get("user_id"),
        nama_user=current_user.get("full_name") or current_user.get("username") or "ADMIN",
        request=request
    )
    trigger_buyer_sync()

    return VoucherResponse(
        id=voucher.id,
        code=voucher.code,
        discount_type=voucher.discount_type,
        discount_value=float(voucher.discount_value),
        min_spend=float(voucher.min_spend or 0.0),
        max_discount=float(voucher.max_discount) if voucher.max_discount is not None else None,
        start_date=voucher.start_date,
        end_date=voucher.end_date,
        usage_limit=voucher.usage_limit,
        used_count=voucher.used_count,
        is_active=voucher.is_active,
        is_public=voucher.is_public,
        created_at=voucher.created_at,
        updated_at=voucher.updated_at
    )


@router.patch("/vouchers/{voucher_id}/toggle-active", response_model=VoucherResponse)
async def toggle_voucher_active(
    voucher_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_permission("promotions:manage"))
):
    """
    Toggle cepat status aktif/nonaktif kupon.
    """
    res = await db.execute(select(Voucher).where(Voucher.id == voucher_id))
    voucher = res.scalar_one_or_none()
    if not voucher:
        raise HTTPException(status_code=404, detail="Kupon tidak ditemukan.")

    voucher.is_active = not voucher.is_active
    voucher.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(voucher)
    trigger_buyer_sync()

    return VoucherResponse(
        id=voucher.id,
        code=voucher.code,
        discount_type=voucher.discount_type,
        discount_value=float(voucher.discount_value),
        min_spend=float(voucher.min_spend or 0.0),
        max_discount=float(voucher.max_discount) if voucher.max_discount is not None else None,
        start_date=voucher.start_date,
        end_date=voucher.end_date,
        usage_limit=voucher.usage_limit,
        used_count=voucher.used_count,
        is_active=voucher.is_active,
        is_public=voucher.is_public,
        created_at=voucher.created_at,
        updated_at=voucher.updated_at
    )


@router.delete("/vouchers/{voucher_id}")
async def delete_voucher(
    voucher_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_permission("promotions:manage"))
):
    """
    Hapus voucher promo dari sistem.
    """
    res = await db.execute(select(Voucher).where(Voucher.id == voucher_id))
    voucher = res.scalar_one_or_none()
    if not voucher:
        raise HTTPException(status_code=404, detail="Kupon tidak ditemukan.")

    await db.delete(voucher)
    await db.commit()
    trigger_buyer_sync()
    return {"success": True, "message": f"Kupon {voucher.code} berhasil dihapus."}


# ==========================================
# BANNER CRUD & UPLOAD ENDPOINTS
# ==========================================

@router.get("/banners", response_model=List[BannerResponse])
async def list_banners(
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_permission("promotions:view"))
):
    """
    Daftar seluruh banner promosi untuk panel admin.
    """
    res = await db.execute(select(Banner).order_by(Banner.created_at.desc()))
    banners = res.scalars().all()
    return [
        BannerResponse(
            id=b.id,
            title=b.title,
            image_url=b.image_url,
            is_active=b.is_active,
            created_at=b.created_at
        )
        for b in banners
    ]


@router.post("/banners/upload")
async def upload_banner_image(
    file: UploadFile = File(...),
    current_user: Dict[str, Any] = Depends(require_permission("promotions:manage"))
):
    """
    Upload file banner baru (JPG, PNG, WEBP, maks 5MB) dan simpan sebagai WebP teroptimasi.
    """
    MAX_BYTES = 5 * 1024 * 1024  # 5 MB
    content = await file.read(MAX_BYTES + 1024)

    if len(content) > MAX_BYTES:
        raise HTTPException(status_code=400, detail="Ukuran banner melebihi batas maksimal 5 MB.")

    if len(content) < 12:
        raise HTTPException(status_code=400, detail="File gambar banner tidak valid.")

    # Validasi Magic Bytes
    is_jpeg = content.startswith(b"\xff\xd8\xff")
    is_png = content.startswith(b"\x89PNG\r\n\x1a\n")
    is_webp = content.startswith(b"RIFF") and b"WEBP" in content[8:16]

    if not (is_jpeg or is_png or is_webp):
        raise HTTPException(
            status_code=400,
            detail="Format file tidak didukung. Harap unggah format JPG, PNG, atau WebP."
        )

    try:
        raw_img = Image.open(io.BytesIO(content))
        raw_img.load()
    except Exception as e:
        logger.error(f"Gagal memproses file banner: {e}")
        raise HTTPException(
            status_code=400,
            detail="Gagal memproses file gambar banner. Pastikan file gambar valid."
        )

    # Convert ke RGB
    if raw_img.mode != "RGB":
        raw_img = raw_img.convert("RGB")

    # Resize jika gambar terlalu raksasa (maksimal lebar 1920px)
    w, h = raw_img.size
    if w > 1920:
        new_h = int(h * (1920 / w))
        raw_img = raw_img.resize((1920, new_h), Image.Resampling.LANCZOS)

    file_id = uuid.uuid4().hex
    rel_path = f"{UPLOAD_BANNER_DIR}/{file_id}.webp"
    raw_img.save(rel_path, format="WEBP", quality=88)

    return {
        "success": True,
        "image_url": f"/{rel_path}"
    }


@router.post("/banners", response_model=BannerResponse)
async def create_banner(
    payload: BannerCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_permission("promotions:manage"))
):
    """
    Simpan banner baru ke database setelah upload.
    """
    new_b = Banner(
        id=str(uuid.uuid4()),
        title=payload.title.strip() if payload.title else "Banner Promosi Dimsum",
        image_url=payload.image_url.strip(),
        is_active=payload.is_active
    )
    db.add(new_b)
    await db.commit()
    await db.refresh(new_b)
    trigger_buyer_sync()

    return BannerResponse(
        id=new_b.id,
        title=new_b.title,
        image_url=new_b.image_url,
        is_active=new_b.is_active,
        created_at=new_b.created_at
    )


@router.patch("/banners/{banner_id}/toggle-active", response_model=BannerResponse)
async def toggle_banner_active(
    banner_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_permission("promotions:manage"))
):
    """
    Toggle aktif/nonaktif penayangan banner di website buyer.
    """
    res = await db.execute(select(Banner).where(Banner.id == banner_id))
    banner = res.scalar_one_or_none()
    if not banner:
        raise HTTPException(status_code=404, detail="Banner tidak ditemukan.")

    banner.is_active = not banner.is_active
    await db.commit()
    await db.refresh(banner)
    trigger_buyer_sync()

    return BannerResponse(
        id=banner.id,
        title=banner.title,
        image_url=banner.image_url,
        is_active=banner.is_active,
        created_at=banner.created_at
    )


@router.delete("/banners/{banner_id}")
async def delete_banner(
    banner_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_permission("promotions:manage"))
):
    """
    Hapus banner dan bersihkan file fisiknya dari disk.
    """
    res = await db.execute(select(Banner).where(Banner.id == banner_id))
    banner = res.scalar_one_or_none()
    if not banner:
        raise HTTPException(status_code=404, detail="Banner tidak ditemukan.")

    # Bersihkan file fisik dengan verifikasi path kanonikal di dalam uploads/banners (VULN-15)
    if banner.image_url:
        base_dir = os.path.abspath(UPLOAD_BANNER_DIR)
        filename = os.path.basename(banner.image_url)
        target_path = os.path.abspath(os.path.join(base_dir, filename))
        if target_path.startswith(base_dir) and os.path.isfile(target_path):
            try:
                os.remove(target_path)
            except Exception as e:
                logger.warning(f"Gagal menghapus file banner {target_path}: {e}")

    await db.delete(banner)
    await db.commit()
    trigger_buyer_sync()
    return {"success": True, "message": "Banner berhasil dihapus."}


# ==========================================
# API KEY INFO FOR ADMIN
# ==========================================

@router.get("/api-key-info")
async def get_buyer_api_key_info(
    current_user: Dict[str, Any] = Depends(require_permission("pos:api_keys"))
):
    """
    Mengambil info API Key integrasi website buyer agar admin dapat menyalinnya.
    """
    buyer_key = os.getenv("BUYER_CLIENT_API_KEY", "mdi_live_9f8e7d6c5b4a321e0f")
    return {
        "header_name": "X-API-Key",
        "api_key": buyer_key,
        "description": "Gunakan API Key ini pada header HTTP 'X-API-Key' di website buyer / e-commerce untuk mengakses katalog produk, promo voucher, dan banner."
    }
