"""
Router Katalog Produk POS - Dimsum MES
Mengelola master katalog produk, harga rupiah, dan upload foto persegi 1:1
dengan penegakan hak akses RBAC granular (Lihat Harga & Ubah Harga).
"""

import os
import io
import uuid
import json
from datetime import datetime
from typing import List, Optional, Dict, Any
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from PIL import Image

from ..database import get_db
from ..models import Product
from ..auth import get_current_user
from ..rbac import require_permission, get_effective_user_permissions
from ..audit_logger import log_audit_event

router = APIRouter(prefix="/products", tags=["Katalog Produk POS"])

UPLOAD_DIR = "uploads/products"
os.makedirs(UPLOAD_DIR, exist_ok=True)


class ProductResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    spec: Optional[str] = None
    price: Optional[int] = None
    image_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    is_active: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ProductCreate(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    spec: Optional[str] = None
    price: Optional[int] = None
    is_active: bool = False


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    spec: Optional[str] = None
    price: Optional[int] = None
    is_active: Optional[bool] = None


async def user_can_view_price(current_user: Dict[str, Any], db: AsyncSession) -> bool:
    """Periksa apakah user memiliki hak akses melihat harga produk."""
    if current_user.get("is_superadmin"):
        return True
    user_id = current_user.get("user_id")
    username = current_user.get("sub") or current_user.get("username")
    perms = await get_effective_user_permissions(user_id, username, db)
    return "freezer_sales:view_price" in perms


async def user_can_edit_price(current_user: Dict[str, Any], db: AsyncSession) -> bool:
    """Periksa apakah user memiliki hak akses mengubah nominal harga produk."""
    if current_user.get("is_superadmin"):
        return True
    user_id = current_user.get("user_id")
    username = current_user.get("sub") or current_user.get("username")
    perms = await get_effective_user_permissions(user_id, username, db)
    return "freezer_sales:edit_price" in perms


def _cleanup_file(rel_path: Optional[str]):
    """Hapus file gambar lama jika ada."""
    if not rel_path:
        return
    clean_p = rel_path.lstrip("/")
    if os.path.exists(clean_p):
        try:
            os.remove(clean_p)
        except Exception:
            pass


@router.get("", response_model=List[ProductResponse])
async def list_products(
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_permission("freezer_sales:view"))
):
    """
    Daftar seluruh produk katalog POS.
    Jika pemanggil tidak memiliki izin 'freezer_sales:view_price', field harga dikosongkan (None).
    """
    can_view = await user_can_view_price(current_user, db)
    res = await db.execute(select(Product).order_by(Product.id.asc()))
    products = res.scalars().all()

    result = []
    for p in products:
        p_dict = {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "spec": p.spec,
            "price": p.price if can_view else None,
            "image_url": p.image_url,
            "thumbnail_url": p.thumbnail_url,
            "is_active": p.is_active,
            "created_at": p.created_at,
            "updated_at": p.updated_at
        }
        result.append(ProductResponse(**p_dict))
    return result


@router.get("/{product_id}", response_model=ProductResponse)
async def get_product_detail(
    product_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_permission("freezer_sales:view"))
):
    """Detail satu produk katalog."""
    res = await db.execute(select(Product).where(Product.id == product_id))
    product = res.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Produk tidak ditemukan.")

    can_view = await user_can_view_price(current_user, db)
    return ProductResponse(
        id=product.id,
        name=product.name,
        description=product.description,
        spec=product.spec,
        price=product.price if can_view else None,
        image_url=product.image_url,
        thumbnail_url=product.thumbnail_url,
        is_active=product.is_active,
        created_at=product.created_at,
        updated_at=product.updated_at
    )


@router.post("", response_model=ProductResponse)
async def create_product(
    payload: ProductCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_permission("freezer_sales:update"))
):
    """
    Buat produk baru.
    Jika user tidak memiliki 'freezer_sales:edit_price', harga dipaksa None.
    Produk tanpa harga tidak boleh langsung aktif.
    """
    pid = payload.id.strip().upper()
    check = await db.execute(select(Product).where(Product.id == pid))
    if check.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"Produk dengan kode '{pid}' sudah ada.")

    can_edit = await user_can_edit_price(current_user, db)
    final_price = payload.price if can_edit else None

    # Produk tanpa harga tidak bisa diaktifkan
    is_act = payload.is_active
    if not final_price and is_act:
        is_act = False

    new_prod = Product(
        id=pid,
        name=payload.name.strip(),
        description=payload.description.strip() if payload.description else None,
        spec=payload.spec.strip() if payload.spec else None,
        price=final_price,
        is_active=is_act
    )
    db.add(new_prod)
    await db.commit()
    await db.refresh(new_prod)

    await log_audit_event(
        db=db,
        aksi="Ubah produk",
        objek_tipe="PRODUCT",
        objek_id=pid,
        ringkasan=f"Buat produk baru '{new_prod.name}' ({pid})",
        user_id=current_user.get("user_id"),
        nama_user=current_user.get("full_name") or current_user.get("username") or "SYSTEM",
        request=request
    )

    can_view = await user_can_view_price(current_user, db)
    return ProductResponse(
        id=new_prod.id,
        name=new_prod.name,
        description=new_prod.description,
        spec=new_prod.spec,
        price=new_prod.price if can_view else None,
        image_url=new_prod.image_url,
        thumbnail_url=new_prod.thumbnail_url,
        is_active=new_prod.is_active,
        created_at=new_prod.created_at,
        updated_at=new_prod.updated_at
    )


@router.put("/{product_id}", response_model=ProductResponse)
async def update_product(
    product_id: str,
    payload: ProductUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_permission("freezer_sales:update"))
):
    """
    Ubah data produk.
    Perubahan harga memerlukan 'freezer_sales:edit_price' (ditolak 403 jika tidak berizin).
    Produk tanpa harga tidak bisa diaktifkan (ditolak 400).
    Perubahan harga dicatat sebagai aksi 'Ubah harga' di audit log.
    Perubahan data lain dicatat sebagai aksi 'Ubah produk'.
    """
    res = await db.execute(select(Product).where(Product.id == product_id))
    product = res.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Produk tidak ditemukan.")

    user_id = current_user.get("user_id")
    actor_name = current_user.get("full_name") or current_user.get("username") or "SYSTEM"

    # 1. Periksa perubahan harga
    price_changed = False
    old_price = product.price
    target_price = product.price

    if payload.price is not None or "price" in payload.model_fields_set:
        if payload.price != product.price:
            can_edit_p = await user_can_edit_price(current_user, db)
            if not can_edit_p:
                await log_audit_event(
                    db=db,
                    aksi="ACCESS_DENIED",
                    objek_tipe="PRODUCT",
                    objek_id=product_id,
                    ringkasan=f"Upaya ubah harga produk '{product.name}' ditolak (tanpa izin Ubah Harga)",
                    user_id=user_id,
                    nama_user=actor_name,
                    request=request,
                    hasil="DENIED"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Akses ditolak: Anda tidak memiliki izin 'freezer_sales:edit_price' untuk mengubah harga."
                )
            price_changed = True
            target_price = payload.price

    # 2. Periksa status aktif
    target_active = product.is_active if payload.is_active is None else payload.is_active
    if target_active and (target_price is None or target_price <= 0):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Produk tanpa harga tidak dapat diaktifkan. Harap isi harga terlebih dahulu."
        )

    # 3. Catat perubahan data umum
    other_changed = False
    if payload.name is not None and payload.name.strip() != product.name:
        product.name = payload.name.strip()
        other_changed = True

    if "description" in payload.model_fields_set:
        new_desc = payload.description.strip() if payload.description else None
        if new_desc != product.description:
            product.description = new_desc
            other_changed = True

    if "spec" in payload.model_fields_set:
        new_spec = payload.spec.strip() if payload.spec else None
        if new_spec != product.spec:
            product.spec = new_spec
            other_changed = True

    if payload.is_active is not None and payload.is_active != product.is_active:
        product.is_active = target_active
        other_changed = True

    # 4. Terapkan harga jika berubah
    if price_changed:
        product.price = target_price
        await log_audit_event(
            db=db,
            aksi="Ubah harga",
            objek_tipe="PRODUCT",
            objek_id=product.id,
            ringkasan=f"Ubah harga {product.name} dari {old_price or 'Belum ada'} ke {target_price}",
            data_sebelum=json.dumps({"price": old_price}),
            data_sesudah=json.dumps({"price": target_price}),
            user_id=user_id,
            nama_user=actor_name,
            request=request
        )

    if other_changed:
        await log_audit_event(
            db=db,
            aksi="Ubah produk",
            objek_tipe="PRODUCT",
            objek_id=product.id,
            ringkasan=f"Ubah data produk {product.name}",
            user_id=user_id,
            nama_user=actor_name,
            request=request
        )

    product.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(product)

    can_view = await user_can_view_price(current_user, db)
    return ProductResponse(
        id=product.id,
        name=product.name,
        description=product.description,
        spec=product.spec,
        price=product.price if can_view else None,
        image_url=product.image_url,
        thumbnail_url=product.thumbnail_url,
        is_active=product.is_active,
        created_at=product.created_at,
        updated_at=product.updated_at
    )


@router.post("/{product_id}/image", response_model=ProductResponse)
async def upload_product_image(
    product_id: str,
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_permission("freezer_sales:update"))
):
    """
    Upload foto produk rasio 1:1.
    Lapisan Server:
    - Validasi MIME dan magic bytes (JPEG, PNG, WebP; tolak SVG).
    - Batas maks 8 MB.
    - Strip metadata EXIF.
    - Center-crop jika belum persegi 1:1.
    - Generate Utama: 800x800 WebP (quality 85).
    - Generate Thumbnail: 200x200 WebP (quality 85).
    - Simpan dengan nama acak uuid.
    - Hapus file lama jika ada.
    - Catat aksi 'Ganti foto' ke audit log.
    """
    res = await db.execute(select(Product).where(Product.id == product_id))
    product = res.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Produk tidak ditemukan.")

    # 1. Baca byte dan batasi maks 8 MB
    MAX_BYTES = 8 * 1024 * 1024
    content = await file.read(MAX_BYTES + 1024)
    if len(content) > MAX_BYTES:
        raise HTTPException(status_code=400, detail="Ukuran file melebihi batas maksimal 8 MB.")

    if len(content) < 12:
        raise HTTPException(status_code=400, detail="File gambar tidak valid.")

    # 2. Validasi Magic Bytes (Tolak SVG dan non-image)
    is_jpeg = content.startswith(b"\xff\xd8\xff")
    is_png = content.startswith(b"\x89PNG\r\n\x1a\n")
    is_webp = content.startswith(b"RIFF") and b"WEBP" in content[8:16]

    if not (is_jpeg or is_png or is_webp):
        raise HTTPException(
            status_code=400,
            detail="Format file tidak didukung. Harap unggah foto JPEG, PNG, atau WebP."
        )

    # 3. Proses gambar dengan Pillow
    try:
        raw_img = Image.open(io.BytesIO(content))
        raw_img.load()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Gagal memproses file gambar: {str(e)}")

    # Normalisasi format & strip EXIF
    # Convert RGBA atau P ke RGB untuk konsistensi WebP murni
    if raw_img.mode in ("RGBA", "LA", "P"):
        img = Image.new("RGBA", raw_img.size, (255, 255, 255, 255))
        if raw_img.mode == "P":
            raw_img = raw_img.convert("RGBA")
        img.paste(raw_img, mask=raw_img.split()[-1] if raw_img.mode in ("RGBA", "LA") else None)
        img = img.convert("RGB")
    elif raw_img.mode != "RGB":
        img = raw_img.convert("RGB")
    else:
        # Buat image baru tanpa metadata EXIF
        img = Image.new("RGB", raw_img.size)
        img.putdata(list(raw_img.getdata()))

    # 4. Potong tengah (Center Crop) jika rasio belum persegi
    w, h = img.size
    if w != h:
        min_edge = min(w, h)
        left = (w - min_edge) // 2
        top = (h - min_edge) // 2
        img = img.crop((left, top, left + min_edge, top + min_edge))

    # 5. Buat 2 ukuran: Utama 800x800 & Thumbnail 200x200
    main_img = img.resize((800, 800), Image.Resampling.LANCZOS)
    thumb_img = img.resize((200, 200), Image.Resampling.LANCZOS)

    # 6. Simpan dengan nama acak UUID
    file_id = uuid.uuid4().hex
    main_rel = f"{UPLOAD_DIR}/{file_id}_main.webp"
    thumb_rel = f"{UPLOAD_DIR}/{file_id}_thumb.webp"

    main_img.save(main_rel, format="WEBP", quality=85)
    thumb_img.save(thumb_rel, format="WEBP", quality=85)

    # 7. Bersihkan file lama
    _cleanup_file(product.image_url)
    _cleanup_file(product.thumbnail_url)

    product.image_url = f"/{main_rel}"
    product.thumbnail_url = f"/{thumb_rel}"
    product.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(product)

    # 8. Catat ke Audit Log
    user_id = current_user.get("user_id")
    actor_name = current_user.get("full_name") or current_user.get("username") or "SYSTEM"
    await log_audit_event(
        db=db,
        aksi="Ganti foto",
        objek_tipe="PRODUCT",
        objek_id=product.id,
        ringkasan=f"Ganti foto produk {product.name}",
        user_id=user_id,
        nama_user=actor_name,
        request=request
    )

    can_view = await user_can_view_price(current_user, db)
    return ProductResponse(
        id=product.id,
        name=product.name,
        description=product.description,
        spec=product.spec,
        price=product.price if can_view else None,
        image_url=product.image_url,
        thumbnail_url=product.thumbnail_url,
        is_active=product.is_active,
        created_at=product.created_at,
        updated_at=product.updated_at
    )


@router.delete("/{product_id}/image", response_model=ProductResponse)
async def delete_product_image(
    product_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_permission("freezer_sales:update"))
):
    """Hapus foto produk dan thumbnail terkait."""
    res = await db.execute(select(Product).where(Product.id == product_id))
    product = res.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Produk tidak ditemukan.")

    _cleanup_file(product.image_url)
    _cleanup_file(product.thumbnail_url)

    product.image_url = None
    product.thumbnail_url = None
    product.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(product)

    user_id = current_user.get("user_id")
    actor_name = current_user.get("full_name") or current_user.get("username") or "SYSTEM"
    await log_audit_event(
        db=db,
        aksi="Ganti foto",
        objek_tipe="PRODUCT",
        objek_id=product.id,
        ringkasan=f"Hapus foto produk {product.name}",
        user_id=user_id,
        nama_user=actor_name,
        request=request
    )

    can_view = await user_can_view_price(current_user, db)
    return ProductResponse(
        id=product.id,
        name=product.name,
        description=product.description,
        spec=product.spec,
        price=product.price if can_view else None,
        image_url=product.image_url,
        thumbnail_url=product.thumbnail_url,
        is_active=product.is_active,
        created_at=product.created_at,
        updated_at=product.updated_at
    )
