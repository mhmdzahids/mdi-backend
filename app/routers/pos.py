"""
POS & Multi-Channel Sales Router - Dimsum MES
Menangani operasional penjualan kasir langsung (offline),
pemenuhan pesanan ojek online (ShopeeFood, GrabFood, GoFood),
dan penerimaan order e-commerce dari website eksternal melalui API Key.
"""

import hashlib
import secrets
from datetime import datetime, timezone, date
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Header, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func, and_, or_, desc
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import (
    POSOrder,
    POSOrderItem,
    POSOrderScannedLot,
    POSApiKey,
    Product,
    LotTracking,
    CustodyTransferLog,
    User
)
from app.auth import get_current_user
from app.rbac import require_permission, get_effective_user_permissions
from app.audit_logger import log_audit_event, extract_client_ip

router = APIRouter(prefix="/pos", tags=["Point of Sale"])


# ==========================================
# PYDANTIC SCHEMAS
# ==========================================

class ScanLotRequest(BaseModel):
    lot_qr_code: str = Field(..., description="Kode QR / Barcode pada kemasan dimsum")

class ScanLotResponse(BaseModel):
    valid: bool
    lot_qr_code: str
    product_id: str
    product_name: str
    spec: Optional[str] = None
    price: int
    lot_status: str
    quantity: float
    unit: str
    thumbnail_url: Optional[str] = None

class DirectOrderItem(BaseModel):
    product_id: str
    product_name: str
    qty: int
    price: int

class DirectScannedLot(BaseModel):
    lot_qr_code: str
    product_id: str
    product_name: Optional[str] = None

class DirectCheckoutRequest(BaseModel):
    customer_name: Optional[str] = "Walk-in"
    payment_method: str = "CASH" # CASH, QRIS, TRANSFER
    amount_paid: int = 0
    items: List[DirectOrderItem]
    scanned_lots: List[DirectScannedLot]
    notes: Optional[str] = None

class CreateQueueOrderRequest(BaseModel):
    order_no: str # misal "SPF-8821" atau "GRB-129"
    channel: str = "SHOPEEFOOD" # SHOPEEFOOD, GOFOOD, GRABFOOD, OFFLINE, WEBSITE
    customer_name: Optional[str] = None
    payment_method: str = "EXTERNAL_PAID" # EXTERNAL_PAID, CASH, QRIS, TRANSFER
    items: List[DirectOrderItem]
    notes: Optional[str] = None

class ExternalWebOrderItem(BaseModel):
    product_id: str
    qty: int

class ExternalWebOrderRequest(BaseModel):
    external_order_id: str
    customer_name: Optional[str] = None
    items: List[ExternalWebOrderItem]
    total_amount: Optional[int] = 0
    payment_method: Optional[str] = "EXTERNAL_PAID"
    notes: Optional[str] = None

class CreateApiKeyRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)


# ==========================================
# HELPER FUNCTIONS
# ==========================================

def generate_order_id(prefix: str = "ORD") -> str:
    now_str = datetime.now().strftime("%Y%m%d%H%M%S")
    rand_suffix = secrets.token_hex(2).upper()
    return f"{prefix}-{now_str}-{rand_suffix}"

def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()

async def verify_external_api_key(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db)
) -> POSApiKey:
    """Verifikasi API Key pada header X-API-Key untuk integrasi sistem eksternal."""
    if not x_api_key or not x_api_key.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Header X-API-Key diperlukan untuk mengakses endpoint ini."
        )

    clean_key = x_api_key.strip()
    key_hash = hash_api_key(clean_key)

    stmt = select(POSApiKey).where(
        POSApiKey.key_hash == key_hash,
        POSApiKey.is_active == True
    )
    res = await db.execute(stmt)
    api_key_obj = res.scalar_one_or_none()

    if not api_key_obj:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API Key tidak valid atau telah dinonaktifkan."
        )

    # Catat waktu pemakaian terakhir
    api_key_obj.last_used_at = datetime.now()
    await db.commit()

    return api_key_obj


# ==========================================
# 1. SCAN PACKAGING & VALIDATION
# ==========================================

@router.post("/scan-lot", response_model=ScanLotResponse)
async def scan_lot(
    payload: ScanLotRequest,
    current_user: Dict[str, Any] = Depends(require_permission("pos:operate")),
    db: AsyncSession = Depends(get_db)
):
    """
    Validasi kode QR kemasan fisik sebelum dimasukkan ke keranjang kasir.
    Memastikan kemasan valid, belum terjual, dan mengambil data harga produk.
    """
    code = payload.lot_qr_code.strip()
    if not code:
        raise HTTPException(status_code=400, detail="Kode QR kemasan kosong.")

    # 1. Cari lot di database
    stmt = select(LotTracking).where(LotTracking.lot_qr_code == code)
    res = await db.execute(stmt)
    lot = res.scalar_one_or_none()

    if not lot:
        raise HTTPException(status_code=404, detail=f"Kemasan dengan barcode '{code}' tidak terdaftar.")

    # 2. Cek apakah sudah pernah terjual
    if lot.current_status == "SOLD":
        # Cari info order yang menjual kemasan ini
        q_order = (
            select(POSOrderScannedLot, POSOrder)
            .join(POSOrder, POSOrderScannedLot.order_id == POSOrder.id)
            .where(POSOrderScannedLot.lot_qr_code == code)
        )
        sold_res = await db.execute(q_order)
        sold_row = sold_res.first()
        order_info = f" (Order #{sold_row[1].order_no})" if sold_row else ""
        raise HTTPException(status_code=400, detail=f"Kemasan '{code}' sudah terjual sebelumnya{order_info}.")

    if lot.current_status == "VOID":
        raise HTTPException(status_code=400, detail=f"Kemasan '{code}' berstatus VOID (rusak/dibatalkan).")

    # 3. Cari Product terkait dari lot_type
    prod_id = lot.lot_type
    p_stmt = select(Product).where(Product.id == prod_id)
    p_res = await db.execute(p_stmt)
    product = p_res.scalar_one_or_none()

    # Fallback produk jika belum ada record eksplisit
    prod_name = product.name if product else ("Dimsum Mika" if "MIKA" in prod_id else "Dimsum Vakum")
    prod_spec = product.spec if product else ("50 Pcs / Mika" if "MIKA" in prod_id else "25 Pcs / Pack")
    prod_price = (product.price or 0) if product else 0
    thumb_url = product.thumbnail_url if product else None

    return ScanLotResponse(
        valid=True,
        lot_qr_code=lot.lot_qr_code,
        product_id=prod_id,
        product_name=prod_name,
        spec=prod_spec,
        price=prod_price,
        lot_status=lot.current_status,
        quantity=float(lot.quantity or 1),
        unit=lot.unit or "PACK",
        thumbnail_url=thumb_url
    )


# ==========================================
# 2. DIRECT CHECKOUT (PENJUALAN OFFLINE)
# ==========================================

@router.post("/orders/direct")
async def direct_checkout(
    payload: DirectCheckoutRequest,
    current_user: Dict[str, Any] = Depends(require_permission("pos:operate")),
    db: AsyncSession = Depends(get_db)
):
    """
    Checkout kasir langsung (walk-in/offline).
    Scan kemasan, hitung total, terima uang, dan langsung selesaikan pesanan.
    """
    if not payload.items and not payload.scanned_lots:
        raise HTTPException(status_code=400, detail="Keranjang belanja kosong.")

    order_id = generate_order_id("ORD")
    now = datetime.now()

    # 1. Hitung total items & total rupiah
    total_amount = sum(item.qty * item.price for item in payload.items)
    total_items = sum(item.qty for item in payload.items)
    total_scanned = len(payload.scanned_lots)

    # 2. Verifikasi seluruh scanned lots agar tidak ada yang double-sold
    lot_codes = [s.lot_qr_code for s in payload.scanned_lots]
    if lot_codes:
        l_stmt = select(LotTracking).where(LotTracking.lot_qr_code.in_(lot_codes))
        l_res = await db.execute(l_stmt)
        lots_map = {l.lot_qr_code: l for l in l_res.scalars().all()}

        for s in payload.scanned_lots:
            lot = lots_map.get(s.lot_qr_code)
            if not lot:
                raise HTTPException(status_code=400, detail=f"Kemasan '{s.lot_qr_code}' tidak ditemukan di sistem.")
            if lot.current_status == "SOLD":
                raise HTTPException(status_code=400, detail=f"Kemasan '{s.lot_qr_code}' sudah berstatus TERJUAL.")

    # 3. Buat POSOrder
    order = POSOrder(
        id=order_id,
        order_no=order_id,
        channel="OFFLINE",
        customer_name=payload.customer_name or "Walk-in",
        status="COMPLETED",
        total_items=total_items,
        total_scanned=total_scanned,
        total_amount=total_amount,
        payment_method=payload.payment_method.upper(),
        amount_paid=payload.amount_paid or total_amount,
        cashier_id=str(current_user.get("id") or current_user.get("username")),
        cashier_name=current_user.get("full_name") or current_user.get("username"),
        notes=payload.notes,
        created_at=now,
        completed_at=now
    )
    db.add(order)
    await db.flush()

    # 4. Tambahkan items
    for item in payload.items:
        order_item = POSOrderItem(
            order_id=order.id,
            product_id=item.product_id,
            product_name=item.product_name,
            qty_ordered=item.qty,
            qty_scanned=item.qty,
            unit_price=item.price,
            subtotal=item.qty * item.price
        )
        db.add(order_item)

    # 5. Potong status stok di LotTracking & catat CustodyTransferLog
    for s in payload.scanned_lots:
        lot = lots_map.get(s.lot_qr_code)
        if lot:
            lot.current_status = "SOLD"
            lot.current_station = "CUSTOMER"

            transfer_log = CustodyTransferLog(
                lot_qr_code=lot.lot_qr_code,
                action_type="POS_SALE_DISPATCH",
                station_id="SALES_STATION",
                target_station="CUSTOMER",
                user_id=current_user.get("username") or "KASIR",
                user_role_active="CASHIER",
                notes=f"Penjualan langsung #{order.order_no} ({order.payment_method})"
            )
            db.add(transfer_log)

            scanned_record = POSOrderScannedLot(
                order_id=order.id,
                lot_qr_code=lot.lot_qr_code,
                product_id=s.product_id,
                product_name=s.product_name or s.product_id,
                scanned_at=now,
                scanned_by=current_user.get("username")
            )
            db.add(scanned_record)

    await db.commit()

    return {
        "success": True,
        "message": "Transaksi berhasil disimpan.",
        "order": {
            "id": order.id,
            "order_no": order.order_no,
            "channel": order.channel,
            "total_items": order.total_items,
            "total_amount": order.total_amount,
            "payment_method": order.payment_method,
            "completed_at": order.completed_at.isoformat()
        }
    }


# ==========================================
# 3. ANTREAN PESANAN (QUEUE / MULTI-CHANNEL)
# ==========================================

@router.get("/orders")
async def list_orders(
    status_filter: Optional[str] = Query("ALL", alias="status"),
    channel_filter: Optional[str] = Query(None, alias="channel"),
    limit: int = Query(50, ge=1, le=200),
    current_user: Dict[str, Any] = Depends(require_permission("pos:view")),
    db: AsyncSession = Depends(get_db)
):
    """
    Daftar antrean pesanan multi-channel.
    Diurutkan: pesanan yang belum lengkap (PENDING_SCAN) tampil paling atas.
    """
    stmt = (
        select(POSOrder)
        .options(
            selectinload(POSOrder.items),
            selectinload(POSOrder.scanned_lots)
        )
    )

    conditions = []
    if status_filter and status_filter.upper() != "ALL":
        conditions.append(POSOrder.status == status_filter.upper())
    if channel_filter and channel_filter.upper() != "ALL":
        conditions.append(POSOrder.channel == channel_filter.upper())

    if conditions:
        stmt = stmt.where(and_(*conditions))

    # Urutan: PENDING_SCAN di atas, lalu descending created_at
    stmt = stmt.order_by(
        (POSOrder.status == "PENDING_SCAN").desc(),
        POSOrder.created_at.desc()
    ).limit(limit)

    res = await db.execute(stmt)
    orders = res.scalars().all()

    user_id = current_user.get("user_id")
    username = current_user.get("sub") or current_user.get("username")
    perms = await get_effective_user_permissions(user_id, username, db)
    can_view_revenue = current_user.get("is_superadmin", False) or "pos:view_revenue" in perms

    result = []
    for o in orders:
        result.append({
            "id": o.id,
            "order_no": o.order_no,
            "channel": o.channel,
            "customer_name": o.customer_name,
            "status": o.status,
            "total_items": o.total_items,
            "total_scanned": o.total_scanned,
            "total_amount": o.total_amount if can_view_revenue else None,
            "payment_method": o.payment_method,
            "cashier_name": o.cashier_name,
            "notes": o.notes,
            "created_at": o.created_at.isoformat(),
            "completed_at": o.completed_at.isoformat() if o.completed_at else None,
            "items": [
                {
                    "id": it.id,
                    "product_id": it.product_id,
                    "product_name": it.product_name,
                    "qty_ordered": it.qty_ordered,
                    "qty_scanned": it.qty_scanned,
                    "unit_price": it.unit_price if can_view_revenue else None,
                    "subtotal": it.subtotal if can_view_revenue else None
                }
                for it in o.items
            ],
            "scanned_lots": [
                {
                    "lot_qr_code": sl.lot_qr_code,
                    "product_id": sl.product_id,
                    "scanned_at": sl.scanned_at.isoformat()
                }
                for sl in o.scanned_lots
            ]
        })

    return result


@router.post("/orders")
async def create_queue_order(
    payload: CreateQueueOrderRequest,
    current_user: Dict[str, Any] = Depends(require_permission("pos:manage_orders")),
    db: AsyncSession = Depends(get_db)
):
    """
    Kasir membuat pesanan ojek online (ShopeeFood/GrabFood/GoFood) secara manual
    untuk antrean scan pemenuhan kemasan sebelum diserahkan ke driver.
    """
    if not payload.items:
        raise HTTPException(status_code=400, detail="Daftar produk pesanan kosong.")

    order_id = generate_order_id("OJOL")
    now = datetime.now()

    total_items = sum(it.qty for it in payload.items)
    total_amount = sum(it.qty * it.price for it in payload.items)

    order = POSOrder(
        id=order_id,
        order_no=payload.order_no.strip(),
        channel=payload.channel.upper(),
        customer_name=payload.customer_name or "Driver Ojol",
        status="PENDING_SCAN",
        total_items=total_items,
        total_scanned=0,
        total_amount=total_amount,
        payment_method=payload.payment_method.upper(),
        amount_paid=total_amount if payload.payment_method.upper() == "EXTERNAL_PAID" else 0,
        cashier_id=str(current_user.get("id") or current_user.get("username")),
        cashier_name=current_user.get("full_name") or current_user.get("username"),
        notes=payload.notes,
        created_at=now
    )
    db.add(order)
    await db.flush()

    for item in payload.items:
        order_item = POSOrderItem(
            order_id=order.id,
            product_id=item.product_id,
            product_name=item.product_name,
            qty_ordered=item.qty,
            qty_scanned=0,
            unit_price=item.price,
            subtotal=item.qty * item.price
        )
        db.add(order_item)

    await db.commit()

    return {
        "success": True,
        "message": f"Pesanan {order.channel} #{order.order_no} berhasil masuk antrean scan.",
        "order_id": order.id
    }


@router.post("/orders/{order_id}/scan-lot")
async def scan_lot_for_order(
    order_id: str,
    payload: ScanLotRequest,
    current_user: Dict[str, Any] = Depends(require_permission("pos:manage_orders")),
    db: AsyncSession = Depends(get_db)
):
    """
    Scan kemasan untuk memenuhi satu pesanan dalam antrean (ShopeeFood / Website).
    Memvalidasi lot, mencocokkan dengan item pesanan, dan mengunci kemasan menjadi SOLD.
    """
    code = payload.lot_qr_code.strip()

    # 1. Cek Order
    stmt = (
        select(POSOrder)
        .options(
            selectinload(POSOrder.items),
            selectinload(POSOrder.scanned_lots)
        )
        .where(POSOrder.id == order_id)
    )
    res = await db.execute(stmt)
    order = res.scalar_one_or_none()

    if not order:
        raise HTTPException(status_code=404, detail="Pesanan tidak ditemukan.")

    if order.status != "PENDING_SCAN":
        raise HTTPException(status_code=400, detail=f"Pesanan sudah berstatus '{order.status}', tidak bisa di-scan lagi.")

    # 2. Cek Lot
    l_stmt = select(LotTracking).where(LotTracking.lot_qr_code == code)
    l_res = await db.execute(l_stmt)
    lot = l_res.scalar_one_or_none()

    if not lot:
        raise HTTPException(status_code=404, detail=f"Kemasan '{code}' tidak terdaftar di sistem.")

    if lot.current_status == "SOLD":
        raise HTTPException(status_code=400, detail=f"Kemasan '{code}' sudah berstatus TERJUAL.")

    # 3. Cek apakah kemasan cocok dengan produk yang masih dibutuhkan di pesanan ini
    prod_id = lot.lot_type
    matched_item = None
    for it in order.items:
        if it.product_id == prod_id and it.qty_scanned < it.qty_ordered:
            matched_item = it
            break

    if not matched_item:
        raise HTTPException(
            status_code=400,
            detail=f"Kemasan jenis '{prod_id}' tidak dibutuhkan pada pesanan ini atau jumlahnya sudah lengkap."
        )

    # 4. Bind kemasan ke pesanan
    now = datetime.now()
    matched_item.qty_scanned += 1
    order.total_scanned += 1

    lot.current_status = "SOLD"
    lot.current_station = "CUSTOMER"

    transfer_log = CustodyTransferLog(
        lot_qr_code=lot.lot_qr_code,
        action_type="POS_SALE_DISPATCH",
        station_id="SALES_STATION",
        target_station="CUSTOMER",
        user_id=current_user.get("username") or "KASIR",
        user_role_active="CASHIER",
        notes=f"Pemenuhan pesanan {order.channel} #{order.order_no}"
    )
    db.add(transfer_log)

    scanned_record = POSOrderScannedLot(
        order_id=order.id,
        lot_qr_code=lot.lot_qr_code,
        product_id=prod_id,
        product_name=matched_item.product_name,
        scanned_at=now,
        scanned_by=current_user.get("username")
    )
    db.add(scanned_record)

    is_complete = order.total_scanned >= order.total_items
    await db.commit()

    return {
        "success": True,
        "is_complete": is_complete,
        "total_scanned": order.total_scanned,
        "total_items": order.total_items,
        "message": "Semua kemasan lengkap! Siap diserahkan." if is_complete else f"Terscan {order.total_scanned} dari {order.total_items} pack.",
        "item_progress": {
            "product_id": matched_item.product_id,
            "qty_scanned": matched_item.qty_scanned,
            "qty_ordered": matched_item.qty_ordered
        }
    }


@router.post("/orders/{order_id}/complete")
async def complete_order(
    order_id: str,
    current_user: Dict[str, Any] = Depends(require_permission("pos:manage_orders")),
    db: AsyncSession = Depends(get_db)
):
    """
    Serahkan pesanan ke driver / kurir setelah kemasan lengkap di-scan.
    Mengubah status pesanan menjadi COMPLETED.
    """
    stmt = select(POSOrder).where(POSOrder.id == order_id)
    res = await db.execute(stmt)
    order = res.scalar_one_or_none()

    if not order:
        raise HTTPException(status_code=404, detail="Pesanan tidak ditemukan.")

    if order.total_scanned < order.total_items:
        raise HTTPException(
            status_code=400,
            detail=f"Belum lengkap! Baru di-scan {order.total_scanned} dari {order.total_items} pack."
        )

    order.status = "COMPLETED"
    order.completed_at = datetime.now()
    await db.commit()

    return {
        "success": True,
        "message": f"Pesanan #{order.order_no} berhasil diserahkan dan selesai."
    }


@router.post("/orders/{order_id}/cancel")
async def cancel_order(
    order_id: str,
    current_user: Dict[str, Any] = Depends(require_permission("pos:manage_orders")),
    db: AsyncSession = Depends(get_db)
):
    """
    Batalkan pesanan dalam antrean.
    Seluruh kemasan yang sudah sempat di-scan akan otomatis dikembalikan ke stok aktif (IN_SALES_STOCK).
    """
    stmt = (
        select(POSOrder)
        .options(selectinload(POSOrder.scanned_lots))
        .where(POSOrder.id == order_id)
    )
    res = await db.execute(stmt)
    order = res.scalar_one_or_none()

    if not order:
        raise HTTPException(status_code=404, detail="Pesanan tidak ditemukan.")

    if order.status == "COMPLETED":
        raise HTTPException(status_code=400, detail="Pesanan yang sudah selesai tidak dapat dibatalkan langsung.")

    # Kembalikan seluruh lot yang sempat di-scan ke status IN_SALES_STOCK
    scanned_codes = [s.lot_qr_code for s in order.scanned_lots]
    if scanned_codes:
        l_stmt = select(LotTracking).where(LotTracking.lot_qr_code.in_(scanned_codes))
        l_res = await db.execute(l_stmt)
        lots = l_res.scalars().all()

        for lot in lots:
            lot.current_status = "IN_SALES_STOCK"
            lot.current_station = "SALES_STATION"

            transfer_log = CustodyTransferLog(
                lot_qr_code=lot.lot_qr_code,
                action_type="POS_ORDER_CANCELLED_RETURN",
                station_id="CUSTOMER",
                target_station="SALES_STATION",
                user_id=current_user.get("username") or "KASIR",
                user_role_active="CASHIER",
                notes=f"Order #{order.order_no} dibatalkan: kemasan dikembalikan ke stok freezer"
            )
            db.add(transfer_log)

    order.status = "CANCELLED"
    await db.commit()

    return {
        "success": True,
        "message": f"Pesanan #{order.order_no} dibatalkan. {len(scanned_codes)} pack dikembalikan ke stok."
    }


# ==========================================
# 4. EXTERNAL WEB ORDER (API KEY AUTH)
# ==========================================

@router.post("/external-orders")
async def receive_external_web_order(
    payload: ExternalWebOrderRequest,
    api_key: POSApiKey = Depends(verify_external_api_key),
    db: AsyncSession = Depends(get_db)
):
    """
    Endpoint integrasi publik untuk website toko online.
    Diautentikasi menggunakan Header: X-API-Key.
    Secara otomatis memasukkan pesanan web ke antrean scan POS pabrik.
    """
    if not payload.items:
        raise HTTPException(status_code=400, detail="Item pesanan tidak boleh kosong.")

    ext_id = payload.external_order_id.strip()

    # Cegah duplikasi pesanan
    dup_stmt = select(POSOrder).where(POSOrder.order_no == ext_id)
    dup_res = await db.execute(dup_stmt)
    if dup_res.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Pesanan dengan ID '{ext_id}' sudah ada di sistem.")

    # Ambil katalog produk untuk hitung harga & nama
    prod_ids = [it.product_id for it in payload.items]
    p_stmt = select(Product).where(Product.id.in_(prod_ids))
    p_res = await db.execute(p_stmt)
    prod_map = {p.id: p for p in p_res.scalars().all()}

    now = datetime.now()
    total_items = sum(it.qty for it in payload.items)
    calculated_amount = 0

    order_id = generate_order_id("WEB")
    order = POSOrder(
        id=order_id,
        order_no=ext_id,
        channel="WEBSITE",
        customer_name=payload.customer_name or "Pembeli Web",
        status="PENDING_SCAN",
        total_items=total_items,
        total_scanned=0,
        total_amount=payload.total_amount or 0,
        payment_method=(payload.payment_method or "EXTERNAL_PAID").upper(),
        amount_paid=payload.total_amount or 0,
        cashier_id=f"API_KEY:{api_key.id}",
        cashier_name=api_key.name,
        notes=payload.notes or f"Integrasi via {api_key.name}",
        created_at=now
    )
    db.add(order)
    await db.flush()

    for item in payload.items:
        prod = prod_map.get(item.product_id)
        unit_price = prod.price if (prod and prod.price) else 0
        p_name = prod.name if prod else item.product_id
        subtotal = item.qty * unit_price
        calculated_amount += subtotal

        order_item = POSOrderItem(
            order_id=order.id,
            product_id=item.product_id,
            product_name=p_name,
            qty_ordered=item.qty,
            qty_scanned=0,
            unit_price=unit_price,
            subtotal=subtotal
        )
        db.add(order_item)

    if not payload.total_amount:
        order.total_amount = calculated_amount
        order.amount_paid = calculated_amount

    await db.commit()

    return {
        "success": True,
        "message": f"Pesanan web '{ext_id}' berhasil diterima dan masuk antrean packing POS.",
        "order_id": order.id,
        "order_no": order.order_no,
        "total_items": order.total_items
    }


# ==========================================
# 5. API KEYS MANAGEMENT (SUPER ADMIN)
# ==========================================

@router.get("/api-keys")
async def list_api_keys(
    current_user: Dict[str, Any] = Depends(require_permission("pos:api_keys")),
    db: AsyncSession = Depends(get_db)
):
    """Melihat daftar API Key untuk website luar (hanya prefix yang ditampilkan demi keamanan)."""
    stmt = select(POSApiKey).order_by(POSApiKey.created_at.desc())
    res = await db.execute(stmt)
    keys = res.scalars().all()

    return [
        {
            "id": k.id,
            "name": k.name,
            "key_prefix": k.key_prefix,
            "is_active": k.is_active,
            "created_by": k.created_by,
            "created_at": k.created_at.isoformat(),
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None
        }
        for k in keys
    ]


@router.post("/api-keys")
async def create_api_key(
    payload: CreateApiKeyRequest,
    current_user: Dict[str, Any] = Depends(require_permission("pos:api_keys")),
    db: AsyncSession = Depends(get_db)
):
    """
    Membuat API Key baru untuk website luar.
    Plaintext token hanya dikembalikan SATU KALI pada saat pembuatan.
    """
    raw_token = f"mdi_live_{secrets.token_hex(20)}"
    prefix = raw_token[:12] + "..."
    key_hash = hash_api_key(raw_token)

    api_key = POSApiKey(
        name=payload.name.strip(),
        key_prefix=prefix,
        key_hash=key_hash,
        is_active=True,
        created_by=current_user.get("username"),
        created_at=datetime.now()
    )
    db.add(api_key)
    await db.commit()

    return {
        "success": True,
        "message": "API Key berhasil dibuat. Salin dan simpan sekarang karena token tidak akan ditampilkan lagi.",
        "api_key": raw_token,
        "name": api_key.name,
        "prefix": api_key.key_prefix
    }


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(
    key_id: int,
    current_user: Dict[str, Any] = Depends(require_permission("pos:api_keys")),
    db: AsyncSession = Depends(get_db)
):
    """Mencabut / menonaktifkan API Key."""
    stmt = select(POSApiKey).where(POSApiKey.id == key_id)
    res = await db.execute(stmt)
    k = res.scalar_one_or_none()

    if not k:
        raise HTTPException(status_code=404, detail="API Key tidak ditemukan.")

    await db.delete(k)
    await db.commit()

    return {
        "success": True,
        "message": f"API Key '{k.name}' berhasil dicabut."
    }


# ==========================================
# 6. RINGKASAN HARIAN POS (DAILY SUMMARY)
# ==========================================

@router.get("/daily-summary")
async def get_daily_pos_summary(
    current_user: Dict[str, Any] = Depends(require_permission("pos:view")),
    db: AsyncSession = Depends(get_db)
):
    """
    Rangkuman transaksi kasir hari ini:
    Total rupiah, total pack terjual, per channel, dan per metode pembayaran.
    """
    today_start = datetime.combine(date.today(), datetime.min.time())

    # Ambil order COMPLETED hari ini
    stmt = (
        select(POSOrder)
        .where(
            POSOrder.status == "COMPLETED",
            POSOrder.completed_at >= today_start
        )
    )
    res = await db.execute(stmt)
    orders = res.scalars().all()

    total_revenue = sum(o.total_amount for o in orders)
    total_packs = sum(o.total_scanned for o in orders)
    total_transactions = len(orders)

    # Breakdown per Channel
    by_channel = {"OFFLINE": 0, "SHOPEEFOOD": 0, "GOFOOD": 0, "GRABFOOD": 0, "WEBSITE": 0}
    by_payment = {"CASH": 0, "QRIS": 0, "TRANSFER": 0, "EXTERNAL_PAID": 0}

    for o in orders:
        ch = o.channel or "OFFLINE"
        by_channel[ch] = by_channel.get(ch, 0) + o.total_amount
        pm = o.payment_method or "CASH"
        by_payment[pm] = by_payment.get(pm, 0) + o.total_amount

    # Hitung juga antrean pending
    pending_stmt = select(func.count(POSOrder.id)).where(POSOrder.status == "PENDING_SCAN")
    pending_res = await db.execute(pending_stmt)
    pending_count = pending_res.scalar() or 0

    user_id = current_user.get("user_id")
    username = current_user.get("sub") or current_user.get("username")
    perms = await get_effective_user_permissions(user_id, username, db)
    can_view_revenue = current_user.get("is_superadmin", False) or "pos:view_revenue" in perms

    return {
        "today_date": date.today().isoformat(),
        "can_view_revenue": can_view_revenue,
        "total_revenue": total_revenue if can_view_revenue else None,
        "total_packs_sold": total_packs,
        "total_transactions": total_transactions,
        "pending_queue_count": pending_count,
        "by_channel": by_channel if can_view_revenue else None,
        "by_payment": by_payment if can_view_revenue else None
    }
