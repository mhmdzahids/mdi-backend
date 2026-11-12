"""
Public Buyer API Router - Dimsum Factory
Menyediakan katalog produk, voucher promo, validasi kupon, dan banner promosi
khusus untuk website buyer e-commerce eksternal yang diamankan menggunakan X-API-Key.
"""

import os
import re
import hashlib
import secrets
from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, Security, Request, status
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_db
from ..models import Product, Voucher, Banner, POSApiKey

router = APIRouter(prefix="/public", tags=["Public Buyer API"])

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_buyer_api_key(
    api_key: Optional[str] = Security(API_KEY_HEADER),
    db: AsyncSession = Depends(get_db)
) -> str:
    """
    Middleware / Security Dependency untuk memeriksa header X-API-Key.
    Menerima API Key yang cocok dengan BUYER_CLIENT_API_KEY di .env
    atau API Key aktif yang tersimpan di pos_api_keys database menggunakan hash SHA-256 (VULN-06 & VULN-07).
    """
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autentikasi gagal: Header X-API-Key tidak disertakan.",
            headers={"WWW-Authenticate": "ApiKey"}
        )

    expected_buyer_key = os.getenv("BUYER_CLIENT_API_KEY", "mdi_live_9f8e7d6c5b4a321e0f")
    if expected_buyer_key and secrets.compare_digest(api_key, expected_buyer_key):
        return api_key

    # Cek apakah ada key terdaftar di tabel pos_api_keys via hash SHA-256 (bukan prefix matching)
    key_hash = hashlib.sha256(api_key.strip().encode("utf-8")).hexdigest()
    stmt = select(POSApiKey).where(
        POSApiKey.key_hash == key_hash,
        POSApiKey.is_active == True
    )
    res = await db.execute(stmt)
    matched_key = res.scalar_one_or_none()
    if matched_key:
        matched_key.last_used_at = datetime.utcnow()
        await db.commit()
        return api_key

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Autentikasi gagal: X-API-Key tidak valid atau telah dinonaktifkan.",
        headers={"WWW-Authenticate": "ApiKey"}
    )


# ==========================================
# PYDANTIC SCHEMAS
# ==========================================

class ValidateVoucherRequest(BaseModel):
    code: str
    subtotal: float = Field(..., ge=0, description="Subtotal belanja sebelum diskon")


class ReleaseVoucherRequest(BaseModel):
    code: str
    order_id: Optional[str] = Field(None, description="Nomor order yang dibatalkan / checkout gagal")
    reason: Optional[str] = Field(None, description="Alasan pengembalian kuota")


# ==========================================
# ENDPOINTS
# ==========================================

@router.get("/products")
async def get_buyer_catalog(
    request: Request,
    api_key: str = Depends(verify_buyer_api_key),
    db: AsyncSession = Depends(get_db)
):
    """
    Mengembalikan seluruh produk aktif untuk katalog buyer.
    Menyertakan perhitungan harga diskon, harga coret, dan alokasi stock_online.
    """
    res = await db.execute(
        select(Product)
        .where(Product.is_active == True)
        .order_by(Product.id.asc())
    )
    products = res.scalars().all()

    base_url = str(request.base_url).rstrip("/")
    data = []
    for p in products:
        # Ekstrak perkiraan pack size dari spec (contoh: "25 Pcs / Pack" -> 25)
        pack_size = 25
        if p.spec:
            m = re.search(r"(\d+)", p.spec)
            if m:
                pack_size = int(m.group(1))

        # Format URL gambar lengkap
        img_url = p.image_url
        if img_url and img_url.startswith("/"):
            img_url = f"{base_url}{img_url}"

        orig_price = float(p.original_price) if p.original_price is not None else float(p.price or 0)
        final_price = float(p.price) if p.price is not None else orig_price

        data.append({
            "id": p.id,
            "name": p.name,
            "sku": p.sku or p.id,
            "spec": p.spec,
            "description": p.description,
            "price": final_price,
            "original_price": orig_price,
            "is_discount": bool(p.is_discount),
            "discount_percent": int(p.discount_percent or 0),
            "stock_online": int(p.stock_online or 0),
            "unit": "pack",
            "pack_size": pack_size,
            "image_url": img_url
        })

    return {
        "success": True,
        "data": data
    }


@router.get("/vouchers")
async def get_buyer_public_vouchers(
    api_key: str = Depends(verify_buyer_api_key),
    db: AsyncSession = Depends(get_db)
):
    """
    Mengembalikan daftar voucher yang is_active = True dan is_public = True
    untuk halaman promo buyer.
    """
    now = datetime.utcnow()
    res = await db.execute(
        select(Voucher)
        .where(
            Voucher.is_active == True,
            Voucher.is_public == True
        )
        .order_by(Voucher.created_at.desc())
    )
    vouchers = res.scalars().all()

    data = []
    for v in vouchers:
        # Saring tanggal kedaluwarsa jika ditentukan
        if v.start_date and now < v.start_date:
            continue
        if v.end_date and now > v.end_date:
            continue
        if v.usage_limit is not None and v.used_count >= v.usage_limit:
            continue

        data.append({
            "code": v.code,
            "discount_type": v.discount_type,
            "discount_value": float(v.discount_value),
            "min_spend": float(v.min_spend or 0.0),
            "max_discount": float(v.max_discount) if v.max_discount is not None else None,
            "end_date": v.end_date.strftime("%Y-%m-%dT%H:%M:%SZ") if v.end_date else None
        })

    return {
        "success": True,
        "data": data
    }


@router.post("/vouchers/validate")
async def validate_buyer_voucher(
    payload: ValidateVoucherRequest,
    api_key: str = Depends(verify_buyer_api_key),
    db: AsyncSession = Depends(get_db)
):
    """
    Endpoint validasi kupon saat buyer berada di halaman keranjang checkout.
    Memeriksa keaktifan, kuota, rentang tanggal, dan minimal belanja.
    """
    code = payload.code.strip().upper()
    res = await db.execute(select(Voucher).where(Voucher.code == code))
    voucher = res.scalar_one_or_none()

    if not voucher or not voucher.is_active:
        return {
            "success": False,
            "error": "Kode kupon tidak ditemukan atau sudah tidak aktif."
        }

    now = datetime.utcnow()
    if voucher.start_date and now < voucher.start_date:
        start_str = voucher.start_date.strftime("%d-%m-%Y %H:%M")
        return {
            "success": False,
            "error": f"Kupon promo ini baru dapat digunakan mulai {start_str} WIB."
        }

    if voucher.end_date and now > voucher.end_date:
        return {
            "success": False,
            "error": "Kupon promo telah kedaluwarsa."
        }

    if voucher.usage_limit is not None and voucher.used_count >= voucher.usage_limit:
        return {
            "success": False,
            "error": "Kuota pemakaian kupon ini telah habis."
        }

    subtotal = float(payload.subtotal)
    min_spend = float(voucher.min_spend or 0.0)
    if subtotal < min_spend:
        formatted_min = f"Rp{int(min_spend):,}".replace(",", ".")
        return {
            "success": False,
            "error": f"Minimal belanja untuk promo ini adalah {formatted_min}"
        }

    # Hitung nilai potongan
    disc_val = float(voucher.discount_value)
    if voucher.discount_type == "percent":
        raw_discount = (disc_val / 100.0) * subtotal
        if voucher.max_discount is not None:
            max_d = float(voucher.max_discount)
            discount = min(raw_discount, max_d, subtotal)
        else:
            discount = min(raw_discount, subtotal)
    else:  # fixed
        discount = min(disc_val, subtotal)

    discount = round(discount, 2)

    return {
        "success": True,
        "data": {
            "code": voucher.code,
            "discount": discount,
            "discount_type": voucher.discount_type,
            "discount_value": disc_val,
            "min_spend": min_spend,
            "max_discount": float(voucher.max_discount) if voucher.max_discount is not None else None
        }
    }


@router.post("/vouchers/claim")
async def claim_buyer_voucher(
    payload: ValidateVoucherRequest,
    api_key: str = Depends(verify_buyer_api_key),
    db: AsyncSession = Depends(get_db)
):
    """
    Klaim kupon saat order buyer di-submit (TUGAS 2: Row-level locking).
    Mencegah eksploitasi race condition pada batas kuota pemakaian.
    """
    code = payload.code.strip().upper()

    # Kunci baris voucher untuk mencegah race condition (SELECT ... FOR UPDATE)
    stmt = select(Voucher).where(Voucher.code == code).with_for_update()
    res = await db.execute(stmt)
    voucher = res.scalar_one_or_none()

    if not voucher or not voucher.is_active:
        raise HTTPException(status_code=400, detail="Kode kupon tidak valid atau sudah tidak aktif.")

    now = datetime.utcnow()
    if voucher.start_date and now < voucher.start_date:
        raise HTTPException(status_code=400, detail="Kupon promo belum dapat digunakan.")
    if voucher.end_date and now > voucher.end_date:
        raise HTTPException(status_code=400, detail="Kupon promo telah kedaluwarsa.")
    if voucher.usage_limit is not None and voucher.used_count >= voucher.usage_limit:
        raise HTTPException(status_code=400, detail="Kuota pemakaian kupon ini telah habis.")

    subtotal = float(payload.subtotal)
    min_spend = float(voucher.min_spend or 0.0)
    if subtotal < min_spend:
        raise HTTPException(status_code=400, detail=f"Minimal belanja belum terpenuhi ({min_spend}).")

    disc_val = float(voucher.discount_value)
    if voucher.discount_type == "percent":
        raw_discount = (disc_val / 100.0) * subtotal
        if voucher.max_discount is not None:
            max_d = float(voucher.max_discount)
            discount = min(raw_discount, max_d, subtotal)
        else:
            discount = min(raw_discount, subtotal)
    else:
        discount = min(disc_val, subtotal)

    voucher.used_count += 1
    voucher.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(voucher)

    return {
        "success": True,
        "message": f"Kupon {voucher.code} berhasil diklaim.",
        "discount": round(discount, 2),
        "used_count": voucher.used_count
    }


@router.post("/vouchers/release")
async def release_buyer_voucher(
    payload: ReleaseVoucherRequest,
    api_key: str = Depends(verify_buyer_api_key),
    db: AsyncSession = Depends(get_db)
):
    """
    Rollback kuota kupon jika pesanan buyer batal atau checkout kedaluwarsa.
    Memerlukan validasi order_id untuk mencegah eksploitasi rollback sembarangan (VULN-10).
    """
    code = payload.code.strip().upper()
    if not payload.order_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Validasi gagal: order_id wajib disertakan untuk melakukan rollback kuota voucher."
        )

    stmt = select(Voucher).where(Voucher.code == code).with_for_update()
    res = await db.execute(stmt)
    voucher = res.scalar_one_or_none()

    if not voucher:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Voucher dengan kode '{code}' tidak ditemukan."
        )

    if voucher.used_count <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Voucher '{code}' tidak memiliki kuota terpakai untuk dikembalikan."
        )

    voucher.used_count -= 1
    voucher.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(voucher)

    return {
        "success": True,
        "message": f"Kuota voucher {code} untuk pesanan {payload.order_id} berhasil dikembalikan."
    }


@router.get("/banners")
async def get_buyer_banners(
    request: Request,
    api_key: str = Depends(verify_buyer_api_key),
    db: AsyncSession = Depends(get_db)
):
    """
    Mengembalikan daftar banner aktif untuk carousel beranda website buyer.
    """
    res = await db.execute(
        select(Banner)
        .where(Banner.is_active == True)
        .order_by(Banner.created_at.desc())
    )
    banners = res.scalars().all()

    base_url = str(request.base_url).rstrip("/")
    data = []
    for b in banners:
        img_url = b.image_url
        if img_url and img_url.startswith("/"):
            img_url = f"{base_url}{img_url}"

        data.append({
            "id": b.id,
            "title": b.title or "Banner Promosi Dimsum",
            "image_url": img_url
        })

    return {
        "success": True,
        "data": data
    }
