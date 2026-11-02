from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone, timedelta

WIB = timezone(timedelta(hours=7))

def today_wib() -> str:
    """Tanggal hari ini format YYYYMMDD sesuai timezone WIB."""
    return datetime.now(WIB).strftime("%Y%m%d")

from ..database import get_db
from ..auth import require_division, get_current_user
from ..models import LotTracking, CustodyTransferLog, DiscrepancyLog
from ..schemas import (
    LotResponse,
    ReceiveMaterialRequest,
    BatchReceiveMaterialRequest,
    CompleteAssemblyBatchRequest,
    DispatchFgToFreezerRequest,
    DispatchFgToDistributionRequest,
    VerifyPackingRequest,
    ReceiveSalesStockRequest,
    DispatchTraysRequest,
    ReceiveTraysRequest,
    CompletePackingToFgRequest,
    AdjustMaterialStockRequest,
    MaterialUsageRecordRequest,
    SalesStagedVerificationRequest,
    SalesStagedVerificationResponse
)

router = APIRouter(prefix="/production2", tags=["MES Produksi 2 (Cetak & Finishing)"])

async def get_next_lot_index(db: AsyncSession, prefix: str) -> int:
    today_str = today_wib()
    pattern = f"{prefix}-{today_str}-%"
    query = select(LotTracking).where(LotTracking.lot_qr_code.like(pattern)).order_by(desc(LotTracking.lot_qr_code)).limit(1)
    result = await db.execute(query)
    last_lot = result.scalar_one_or_none()

    if last_lot:
        parts = last_lot.lot_qr_code.split("-")
        try:
            return int(parts[-1]) + 1
        except ValueError:
            return 1
    return 1



# 1. AMBIL DAFTAR BAHAN INBOUND (TRANSIT & DITERIMA DI PRODUKSI 2)
@router.get("/inbound-materials", response_model=Dict[str, Any])
async def get_inbound_materials(
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Mengambil daftar bahan masuk untuk Produksi 2:
    - Adonan: IN_TRANSIT_TO_ASSEMBLY (transit) atau IN_ASSEMBLY_STATION (diterima)
    - Kulit: IN_TRANSIT_TO_ASSEMBLY (transit) atau IN_ASSEMBLY_STATION (diterima)
    - Saos: IN_TRANSIT_TO_FINISHING (transit) atau IN_FINISHING_STATION (diterima)
    """
    query = select(LotTracking).where(
        LotTracking.current_status.in_([
            "IN_TRANSIT_TO_ASSEMBLY",
            "IN_ASSEMBLY_STATION",
            "READY_IN_ASSEMBLY_STATION",
            "IN_TRANSIT_TO_FINISHING",
            "IN_FINISHING_STATION",
            "READY_IN_FINISHING_STATION"
        ])
    ).order_by(LotTracking.created_at.desc())

    result = await db.execute(query)
    lots = result.scalars().all()

    transit_items = []
    received_items = []

    for lot in lots:
        item = {
            "lot_qr_code": lot.lot_qr_code,
            "plan_id": lot.plan_id,
            "lot_type": lot.lot_type,
            "current_status": lot.current_status,
            "current_station": lot.current_station,
            "quantity": float(lot.quantity),
            "unit": lot.unit,
            "created_at": lot.created_at
        }
        if lot.current_status in ["IN_TRANSIT_TO_ASSEMBLY", "IN_TRANSIT_TO_FINISHING"]:
            transit_items.append(item)
        else:
            received_items.append(item)

    return {
        "transit_count": len(transit_items),
        "received_count": len(received_items),
        "transit_items": transit_items,
        "received_items": received_items
    }


# 2. SCAN-IN TUNGGAL: TERIMA 1 BAHAN (ADONAN / KULIT / SAOS)
@router.post("/receive-material", response_model=LotResponse)
async def receive_material(
    payload: ReceiveMaterialRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_division(["CETAK", "ASSEMBLY", "PACKING", "FINISHING"]))
):
    """
    Operator Produksi 2 men-scan QR fisik Adonan, Kulit, atau Saos
    untuk menerima tanggung jawab fisik di Bagian Cetak atau Finishing.
    """
    clean_code = payload.lot_code.strip().upper()

    query = select(LotTracking).where(LotTracking.lot_qr_code == clean_code)
    result = await db.execute(query)
    lot = result.scalar_one_or_none()

    if not lot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Bahan '{clean_code}' tidak ditemukan di database."
        )

    # Idempoten jika sudah pernah diterima
    if lot.current_status in ["IN_ASSEMBLY_STATION", "IN_FINISHING_STATION"]:
        return lot

    # Validasi alur perpindahan
    if clean_code.startswith("AD-"):
        if lot.current_status != "IN_TRANSIT_TO_ASSEMBLY":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Adonan {clean_code} belum dikirim dari Mixer (status saat ini: {lot.current_status})."
            )
        new_status = "IN_ASSEMBLY_STATION"
        new_station = "ASSEMBLY_STATION"
        note_text = "Terima Adonan di Bagian Cetak"

    elif clean_code.startswith("KL-"):
        if lot.current_status != "IN_TRANSIT_TO_ASSEMBLY":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Kulit {clean_code} belum dikirim dari Bagian Kulit (status saat ini: {lot.current_status})."
            )
        new_status = "IN_ASSEMBLY_STATION"
        new_station = "ASSEMBLY_STATION"
        note_text = "Terima Kulit di Bagian Cetak"

    elif clean_code.startswith("SA-"):
        if lot.current_status != "IN_TRANSIT_TO_FINISHING":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Saos {clean_code} belum dikirim dari Bagian Saos (status saat ini: {lot.current_status})."
            )
        new_status = "IN_FINISHING_STATION"
        new_station = "FINISHING_STATION"
        note_text = "Terima Saos di Bagian Finishing"

    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Format barcode '{clean_code}' tidak dikenali sebagai bahan Produksi 2 (harus AD-, KL-, atau SA-)."
        )

    try:
        lot.current_status = new_status
        lot.current_station = new_station

        log = CustodyTransferLog(
            lot_qr_code=lot.lot_qr_code,
            action_type="SCAN_IN",
            station_id=new_station,
            target_station=None,
            user_id=payload.operator_id,
            user_role_active=payload.operator_role,
            notes=note_text
        )
        db.add(log)

        await db.commit()
        await db.refresh(lot)
        return lot

    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat menerima bahan {clean_code}: {str(e)}"
        )


# 3. SCAN-IN BATCH: TERIMA BANYAK BAHAN SEKALIGUS
@router.post("/receive-batch", response_model=List[LotResponse])
async def receive_material_batch(
    payload: BatchReceiveMaterialRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_division(["CETAK", "ASSEMBLY", "PACKING", "FINISHING"]))
):
    """
    Penerimaan rombongan bahan masuk sekaligus dalam 1 transaksi atomic.
    """
    if not payload.lot_codes:
        raise HTTPException(status_code=400, detail="Daftar lot tidak boleh kosong.")

    clean_codes = [c.strip().upper() for c in payload.lot_codes if c.strip()]
    query = select(LotTracking).where(LotTracking.lot_qr_code.in_(clean_codes))
    result = await db.execute(query)
    lots = result.scalars().all()

    if not lots:
        raise HTTPException(status_code=404, detail="Tidak ada lot yang ditemukan.")

    updated_lots = []
    try:
        for lot in lots:
            clean_code = lot.lot_qr_code
            if lot.current_status in ["IN_ASSEMBLY_STATION", "IN_FINISHING_STATION"]:
                updated_lots.append(lot)
                continue

            if clean_code.startswith("AD-") and lot.current_status == "IN_TRANSIT_TO_ASSEMBLY":
                lot.current_status = "IN_ASSEMBLY_STATION"
                lot.current_station = "ASSEMBLY_STATION"
                note_text = "Terima Adonan di Bagian Cetak"
            elif clean_code.startswith("KL-") and lot.current_status == "IN_TRANSIT_TO_ASSEMBLY":
                lot.current_status = "IN_ASSEMBLY_STATION"
                lot.current_station = "ASSEMBLY_STATION"
                note_text = "Terima Kulit di Bagian Cetak"
            elif clean_code.startswith("SA-") and lot.current_status == "IN_TRANSIT_TO_FINISHING":
                lot.current_status = "IN_FINISHING_STATION"
                lot.current_station = "FINISHING_STATION"
                note_text = "Terima Saos di Bagian Finishing"
            else:
                continue

            log = CustodyTransferLog(
                lot_qr_code=lot.lot_qr_code,
                action_type="SCAN_IN",
                station_id=lot.current_station,
                target_station=None,
                user_id=payload.operator_id,
                user_role_active=payload.operator_role,
                notes=note_text
            )
            db.add(log)
            updated_lots.append(lot)

        await db.commit()
        for lot in updated_lots:
            await db.refresh(lot)
        return updated_lots

    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat menerima batch bahan: {str(e)}"
        )


# 4. EKSEKUSI SELESAI BATCH CETAK: TERBITKAN FG MIKA (50 PCS) & FG VAKUM (25 PCS)
@router.post("/complete-assembly-batch", response_model=List[LotResponse], status_code=status.HTTP_201_CREATED)
async def complete_assembly_batch(
    payload: CompleteAssemblyBatchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_division(["CETAK", "ASSEMBLY"]))
):
    """
    Selesaikan batch cetak & wadah di Produksi 2.
    Menutup bahan adonan dan kulit menjadi CONSUMED,
    serta menerbitkan QR Finished Goods unik:
    - FG-MIKA-YYYYMMDD-XXX (@ 50 pcs)
    - FG-VAKUM-YYYYMMDD-XXX (@ 25 pcs)
    """
    if payload.mika_output_qty <= 0 and payload.vakum_output_qty <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Jumlah produk jadi minimal 1 mika atau 1 vakum."
        )

    today_str = today_wib()
    created_fg_lots = []

    try:
        # Validasi stok adonan
        clean_dough = [c.strip().upper() for c in (payload.dough_lot_codes or []) if c.strip()]
        if not clean_dough:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Daftar lot adonan tidak boleh kosong."
            )

        q_dough = select(LotTracking).where(LotTracking.lot_qr_code.in_(clean_dough))
        res_dough = await db.execute(q_dough)
        dough_lots = res_dough.scalars().all()

        if len(dough_lots) != len(clean_dough):
            found_codes = {dl.lot_qr_code for dl in dough_lots}
            missing = [c for c in clean_dough if c not in found_codes]
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Lot adonan tidak ditemukan: {', '.join(missing)}"
            )

        # Anti-Replay: Tolak jika ada adonan yang sudah berstatus CONSUMED
        consumed_dough = [dl.lot_qr_code for dl in dough_lots if dl.current_status == "CONSUMED"]
        if consumed_dough:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Replay Error: Lot adonan sudah berstatus CONSUMED: {', '.join(consumed_dough)}. Tidak dapat digunakan kembali."
            )

        total_dough_kg = sum(float(dl.quantity or 20.0) for dl in dough_lots)
        required_dough_kg = (payload.mika_output_qty * 2.0) + (payload.vakum_output_qty * 1.0)
        if total_dough_kg < required_dough_kg:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Stok adonan ({total_dough_kg:.1f} kg) tidak cukup untuk pesanan (butuh {required_dough_kg:.1f} kg)."
            )

        # Validasi jenis kulit
        clean_wrap = [c.strip().upper() for c in (payload.wrapper_lot_codes or []) if c.strip()]
        if not clean_wrap:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Daftar lot kulit tidak boleh kosong."
            )

        q_wrap = select(LotTracking).where(LotTracking.lot_qr_code.in_(clean_wrap))
        res_wrap = await db.execute(q_wrap)
        wrap_lots = res_wrap.scalars().all()

        if len(wrap_lots) != len(clean_wrap):
            found_codes = {wl.lot_qr_code for wl in wrap_lots}
            missing = [c for c in clean_wrap if c not in found_codes]
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Lot kulit tidak ditemukan: {', '.join(missing)}"
            )

        # Anti-Replay: Tolak jika ada kulit yang sudah berstatus CONSUMED
        consumed_wrap = [wl.lot_qr_code for wl in wrap_lots if wl.current_status == "CONSUMED"]
        if consumed_wrap:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Replay Error: Lot kulit sudah berstatus CONSUMED: {', '.join(consumed_wrap)}. Tidak dapat digunakan kembali."
            )

        if payload.mika_output_qty > 0:
            has_9cm = any("7CM" not in (wl.lot_type or "").upper() and "7CM" not in (wl.lot_qr_code or "").upper() for wl in wrap_lots)
            if not has_9cm:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Tidak ada lot Kulit 9 cm untuk Dimsum Mika."
                )

        if payload.vakum_output_qty > 0:
            has_7cm = any("7CM" in (wl.lot_type or "").upper() or "7CM" in (wl.lot_qr_code or "").upper() for wl in wrap_lots)
            if not has_7cm:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Tidak ada lot Kulit 7 cm untuk Dimsum Vakum."
                )

        # 1. Tutup Lot Adonan yang dikonsumsi
        for dl in dough_lots:
            dl.current_status = "CONSUMED"
            log = CustodyTransferLog(
                lot_qr_code=dl.lot_qr_code,
                action_type="CONSUMED",
                station_id="ASSEMBLY_STATION",
                target_station=None,
                user_id=payload.operator_id,
                user_role_active=payload.operator_role,
                notes=f"Adonan dipakai cetak batch {today_str}"
            )
            db.add(log)

        # 2. Tutup Lot Kulit yang dikonsumsi
        if payload.wrapper_lot_codes:
            clean_wrap = [c.strip().upper() for c in payload.wrapper_lot_codes if c.strip()]
            q_wrap = select(LotTracking).where(LotTracking.lot_qr_code.in_(clean_wrap))
            res_wrap = await db.execute(q_wrap)
            wrap_lots = res_wrap.scalars().all()
            for wl in wrap_lots:
                wl.current_status = "CONSUMED"
                log = CustodyTransferLog(
                    lot_qr_code=wl.lot_qr_code,
                    action_type="CONSUMED",
                    station_id="ASSEMBLY_STATION",
                    target_station=None,
                    user_id=payload.operator_id,
                    user_role_active=payload.operator_role,
                    notes=f"Kulit dipakai cetak batch {today_str}"
                )
                db.add(log)

        # 3. Terbitkan QR Dimsum Mika (Isi 50 Butir per Mika)
        if payload.mika_output_qty > 0:
            start_mika_idx = await get_next_lot_index(db, "FG-MIKA")
            for i in range(payload.mika_output_qty):
                code = f"FG-MIKA-{today_str}-{str(start_mika_idx + i).zfill(3)}"
                fg_mika = LotTracking(
                    lot_qr_code=code,
                    plan_id=payload.plan_id,
                    lot_type="FG_MIKA",
                    current_status="READY_IN_ASSEMBLY_STATION",
                    current_station="ASSEMBLY_STATION",
                    quantity=50.0,
                    unit="MIKA"
                )
                db.add(fg_mika)

                log = CustodyTransferLog(
                    lot_qr_code=code,
                    action_type="FG_PACKED",
                    station_id="ASSEMBLY_STATION",
                    target_station=None,
                    user_id=payload.operator_id,
                    user_role_active=payload.operator_role,
                    notes=f"Dimsum Mika {code} isi 50 butir siap masuk freezer"
                )
                db.add(log)
                created_fg_lots.append(fg_mika)

        # 4. Terbitkan QR Dimsum Vakum (Isi 25 Butir per Pack)
        if payload.vakum_output_qty > 0:
            start_vakum_idx = await get_next_lot_index(db, "FG-VAKUM")
            for j in range(payload.vakum_output_qty):
                code = f"FG-VAKUM-{today_str}-{str(start_vakum_idx + j).zfill(3)}"
                fg_vakum = LotTracking(
                    lot_qr_code=code,
                    plan_id=payload.plan_id,
                    lot_type="FG_VAKUM",
                    current_status="READY_IN_ASSEMBLY_STATION",
                    current_station="ASSEMBLY_STATION",
                    quantity=25.0,
                    unit="PACK"
                )
                db.add(fg_vakum)

                log = CustodyTransferLog(
                    lot_qr_code=code,
                    action_type="FG_PACKED",
                    station_id="ASSEMBLY_STATION",
                    target_station=None,
                    user_id=payload.operator_id,
                    user_role_active=payload.operator_role,
                    notes=f"Dimsum Vakum {code} isi 25 butir siap masuk freezer"
                )
                db.add(log)
                created_fg_lots.append(fg_vakum)

        await db.commit()
        for lot in created_fg_lots:
            await db.refresh(lot)
        return created_fg_lots

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat menyelesaikan batch: {str(e)}"
        )


# 5. SCAN-OUT / SIMPAN KE GUDANG FREEZER (SIAP JUAL POS)
@router.post("/dispatch-to-freezer", response_model=List[LotResponse])
async def dispatch_to_freezer(
    payload: DispatchFgToFreezerRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_division(["CETAK", "ASSEMBLY", "PACKING"]))
):
    """
    Kirim / konfirmasi fisik produk jadi masuk ke gudang freezer.
    Status berubah menjadi IN_FREEZER_STORAGE dan stok otomatis aktif di POS.
    """
    if not payload.lot_codes:
        raise HTTPException(status_code=400, detail="Daftar lot produk jadi tidak boleh kosong.")

    clean_codes = [c.strip().upper() for c in payload.lot_codes if c.strip()]
    query = select(LotTracking).where(LotTracking.lot_qr_code.in_(clean_codes))
    result = await db.execute(query)
    lots = result.scalars().all()

    if not lots:
        raise HTTPException(status_code=404, detail="Tidak ada lot produk jadi yang ditemukan.")

    updated_lots = []
    try:
        for lot in lots:
            lot.current_status = "IN_FREEZER_STORAGE"
            lot.current_station = "FREEZER_STORAGE"

            log = CustodyTransferLog(
                lot_qr_code=lot.lot_qr_code,
                action_type="SCAN_IN",
                station_id="FREEZER_STORAGE",
                target_station=None,
                user_id=payload.operator_id,
                user_role_active=payload.operator_role,
                notes=f"Produk jadi {lot.lot_qr_code} masuk gudang freezer (siap jual)"
            )
            db.add(log)
            updated_lots.append(lot)

        await db.commit()
        for lot in updated_lots:
            await db.refresh(lot)
        return updated_lots

    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat kirim ke freezer: {str(e)}"
        )


# 5b. SCAN-OUT / DISTRIBUSI LANGSUNG (TANPA FREEZER)
@router.post("/dispatch-direct-distribution", response_model=List[LotResponse])
async def dispatch_direct_distribution(
    payload: DispatchFgToDistributionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_division(["PACKING", "GUDANG", "SALES", "DISTRIBUTION"]))
):
    """
    Kirim produk jadi langsung ke distribusi / kurir / outlet hari ini.
    Status berubah menjadi DISTRIBUTED dan tidak masuk ke stok freezer.
    """
    if not payload.lot_codes:
        raise HTTPException(status_code=400, detail="Daftar lot produk jadi tidak boleh kosong.")

    clean_codes = [c.strip().upper() for c in payload.lot_codes if c.strip()]
    query = select(LotTracking).where(LotTracking.lot_qr_code.in_(clean_codes))
    result = await db.execute(query)
    lots = result.scalars().all()

    if not lots:
        raise HTTPException(status_code=404, detail="Tidak ada lot produk jadi yang ditemukan.")

    updated_lots = []
    try:
        for lot in lots:
            lot.current_status = "DISTRIBUTED"
            lot.current_station = "DISTRIBUTION"

            log = CustodyTransferLog(
                lot_qr_code=lot.lot_qr_code,
                action_type="DIRECT_DISPATCH",
                station_id="DISTRIBUTION",
                target_station=None,
                user_id=payload.operator_id,
                user_role_active=payload.operator_role,
                notes=payload.notes or f"Produk jadi {lot.lot_qr_code} langsung dikirim ke distribusi"
            )
            db.add(log)
            updated_lots.append(lot)

        await db.commit()
        for lot in updated_lots:
            await db.refresh(lot)
        return updated_lots

    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat distribusi langsung: {str(e)}"
        )


# 5c. SCAN 1: VERIFIKASI PACKING / CHECKER (LAPORAN PRODUKSI - BELUM MASUK POS)
@router.post("/verify-packing", response_model=List[LotResponse])
async def verify_packing(
    payload: VerifyPackingRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_division(["PACKING"]))
):
    """
    Scan 1 oleh Checker Packing:
    Mencatat laporan hasil produksi harian. Status menjadi PACKED_VERIFIED.
    PENTING: Tidak menambah/update stok di sistem POS.
    """
    if not payload.lot_codes:
        raise HTTPException(status_code=400, detail="Daftar lot produk jadi tidak boleh kosong.")

    clean_codes = [c.strip().upper() for c in payload.lot_codes if c.strip()]
    query = select(LotTracking).where(LotTracking.lot_qr_code.in_(clean_codes))
    result = await db.execute(query)
    lots = result.scalars().all()

    if not lots:
        raise HTTPException(status_code=404, detail="Tidak ada lot produk jadi yang ditemukan.")

    updated_lots = []
    try:
        for lot in lots:
            lot.current_status = "PACKED_VERIFIED"
            lot.current_station = "PACKING_STATION"

            log = CustodyTransferLog(
                lot_qr_code=lot.lot_qr_code,
                action_type="PACKING_VERIFIED",
                station_id="PACKING_STATION",
                target_station=None,
                user_id=payload.operator_id,
                user_role_active=payload.operator_role,
                notes=payload.notes or "Laporan produksi: Selesai packing (belum masuk POS)"
            )
            db.add(log)
            updated_lots.append(lot)

        await db.commit()
        for lot in updated_lots:
            await db.refresh(lot)
        return updated_lots

    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat verifikasi packing: {str(e)}"
        )


# 5d. SCAN 2: TERIMA STOK PENJUALAN (KONFIRMASI FISIK & MASUK STOK POS)
@router.post("/receive-sales-stock", response_model=List[LotResponse])
async def receive_sales_stock(
    payload: ReceiveSalesStockRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_division(["GUDANG", "SALES"]))
):
    """
    Scan 2 oleh Bagian Penjualan / Toko / Kasir:
    Mengonfirmasi fisik barang yang diterima dari dapur produksi.
    Status menjadi IN_SALES_STOCK dan stok siap jual di POS aktif.
    """
    if not payload.lot_codes:
        raise HTTPException(status_code=400, detail="Daftar lot produk jadi tidak boleh kosong.")

    clean_codes = [c.strip().upper() for c in payload.lot_codes if c.strip()]
    query = select(LotTracking).where(LotTracking.lot_qr_code.in_(clean_codes))
    result = await db.execute(query)
    lots = result.scalars().all()

    if not lots:
        raise HTTPException(status_code=404, detail="Tidak ada lot produk jadi yang ditemukan.")

    updated_lots = []
    try:
        for lot in lots:
            lot.current_status = "IN_SALES_STOCK"
            lot.current_station = "SALES_STATION"

            log = CustodyTransferLog(
                lot_qr_code=lot.lot_qr_code,
                action_type="SALES_CONFIRMED",
                station_id="SALES_STATION",
                target_station=None,
                user_id=payload.operator_id,
                user_role_active=payload.operator_role,
                notes=payload.notes or "Diterima bagian penjualan: Stok aktif di POS"
            )
            db.add(log)
            updated_lots.append(lot)

        await db.commit()
        for lot in updated_lots:
            await db.refresh(lot)
        return updated_lots

    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat menerima stok penjualan: {str(e)}"
        )


# 6. DAFTAR PRODUK JADI AKTIF (FG MIKA & FG VAKUM)
@router.get("/finished-goods", response_model=List[LotResponse])
async def list_finished_goods(
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Mengambil seluruh daftar produk jadi (FG-MIKA & FG-VAKUM),
    baik yang masih di Meja Cetak maupun yang sudah di Gudang Freezer.
    """
    query = select(LotTracking).where(
        LotTracking.lot_type.in_(["FG_MIKA", "FG_VAKUM"])
    ).order_by(desc(LotTracking.created_at))

    result = await db.execute(query)
    return result.scalars().all()


# 7. DAFTAR WADAH BESI / TRAY TRANSFER (CETAK -> PACKING)
@router.get("/trays", response_model=List[LotResponse])
async def list_trays(
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Mengambil seluruh daftar wadah transfer (WADAH_CETAK, WADAH_MIKA, WADAH_VAKUM)."""
    query = select(LotTracking).where(
        LotTracking.lot_type.in_(["WADAH_CETAK", "WADAH_MIKA", "WADAH_VAKUM"])
    ).order_by(desc(LotTracking.created_at))
    result = await db.execute(query)
    return result.scalars().all()


# 8. KIRIM WADAH HASIL CETAK KE PACKING
@router.post("/dispatch-trays", response_model=LotResponse, status_code=status.HTTP_201_CREATED)
async def dispatch_trays(
    payload: DispatchTraysRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_division(["CETAK", "ASSEMBLY"]))
):
    """
    Operator Cetak mengirim N nampan/wadah hasil cetak ke Bagian Packing.
    Adonan dan kulit yang digunakan ditandai CONSUMED.
    Wajib menyertakan bukti foto nampan sebelum kirim.
    Menerbitkan tiket wadah transfer digital unik: WD-MIKA-YYYYMMDD-XXX atau WD-VAK-YYYYMMDD-XXX.
    """
    if payload.tray_count <= 0:
        raise HTTPException(status_code=400, detail="Jumlah wadah harus minimal 1.")

    if not payload.photo_evidence or not payload.photo_evidence.strip():
        raise HTTPException(
            status_code=400,
            detail="Bukti foto laporan jumlah nampan wajib dilampirkan sebelum mengirim ke packing."
        )

    prod_type = (payload.product_type or "FG_MIKA").upper().strip()
    if prod_type not in ["FG_MIKA", "FG_VAKUM"]:
        raise HTTPException(status_code=400, detail="Tipe produk harus FG_MIKA atau FG_VAKUM.")

    is_vakum = (prod_type == "FG_VAKUM")
    lot_type = "WADAH_VAKUM" if is_vakum else "WADAH_MIKA"
    type_label = "Vakum (Kulit 7cm)" if is_vakum else "Mika (Kulit 9cm)"
    prefix = "WD-VAK" if is_vakum else "WD-MIKA"

    today_str = today_wib()
    try:
        # Konsumsi Adonan jika disertakan
        if payload.dough_lot_codes:
            clean_dough = [c.strip().upper() for c in payload.dough_lot_codes if c.strip()]
            q_d = select(LotTracking).where(LotTracking.lot_qr_code.in_(clean_dough))
            res_d = await db.execute(q_d)
            dough_lots = res_d.scalars().all()
            for dl in dough_lots:
                dl.current_status = "CONSUMED"
                log_d = CustodyTransferLog(
                    lot_qr_code=dl.lot_qr_code,
                    action_type="CONSUMED",
                    station_id="ASSEMBLY_STATION",
                    target_station=None,
                    user_id=payload.operator_id,
                    user_role_active=payload.operator_role,
                    notes=f"Adonan dicetak menjadi {payload.tray_count} wadah besi ({type_label})"
                )
                db.add(log_d)

        # Konsumsi Kulit jika disertakan
        if payload.wrapper_lot_codes:
            clean_wrap = [c.strip().upper() for c in payload.wrapper_lot_codes if c.strip()]
            q_w = select(LotTracking).where(LotTracking.lot_qr_code.in_(clean_wrap))
            res_w = await db.execute(q_w)
            wrap_lots = res_w.scalars().all()
            for wl in wrap_lots:
                wl.current_status = "CONSUMED"
                log_w = CustodyTransferLog(
                    lot_qr_code=wl.lot_qr_code,
                    action_type="CONSUMED",
                    station_id="ASSEMBLY_STATION",
                    target_station=None,
                    user_id=payload.operator_id,
                    user_role_active=payload.operator_role,
                    notes=f"Kulit dicetak menjadi {payload.tray_count} wadah besi ({type_label})"
                )
                db.add(log_w)

        # Terbitkan tiket transfer wadah WD-MIKA-... atau WD-VAK-...
        next_idx = await get_next_lot_index(db, prefix)
        tray_code = f"{prefix}-{today_str}-{str(next_idx).zfill(3)}"

        tray_lot = LotTracking(
            lot_qr_code=tray_code,
            plan_id=payload.plan_id,
            lot_type=lot_type,
            current_status="IN_TRANSIT_TO_PACKING",
            current_station="ASSEMBLY_STATION",
            quantity=float(payload.tray_count),
            unit="WADAH",
            photo_evidence=payload.photo_evidence
        )
        db.add(tray_lot)

        log_tray = CustodyTransferLog(
            lot_qr_code=tray_code,
            action_type="TRAYS_DISPATCHED",
            station_id="ASSEMBLY_STATION",
            target_station="PACKING_STATION",
            user_id=payload.operator_id,
            user_role_active=payload.operator_role,
            notes=payload.notes or f"Kirim {payload.tray_count} wadah besi ({type_label}) ke bagian packing [Foto terlampir]"
        )
        db.add(log_tray)

        await db.commit()
        await db.refresh(tray_lot)
        return tray_lot

    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Gagal mengirim wadah cetak: {str(e)}"
        )


# 9. TERIMA WADAH DARI CETAK DI MEJA PACKING
@router.post("/receive-trays", response_model=List[LotResponse])
async def receive_trays(
    payload: ReceiveTraysRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_division(["PACKING"]))
):
    """
    Operator Packing mengonfirmasi penerimaan fisik wadah besi yang dikirim dari Cetak.
    Status berubah menjadi IN_PACKING_STATION.
    """
    if not payload.tray_lot_codes:
        raise HTTPException(status_code=400, detail="Daftar wadah tidak boleh kosong.")

    clean_codes = [c.strip().upper() for c in payload.tray_lot_codes if c.strip()]
    query = select(LotTracking).where(LotTracking.lot_qr_code.in_(clean_codes))
    result = await db.execute(query)
    trays = result.scalars().all()

    if not trays:
        raise HTTPException(status_code=404, detail="Tidak ada lot wadah yang ditemukan.")

    # Validasi Isolasi Line (Security Boundary)
    req_line = payload.line_type.strip().upper() if payload.line_type else None
    if req_line in ["MIKA", "VAKUM"]:
        for tray in trays:
            code = (tray.lot_qr_code or "").upper()
            ltype = (tray.lot_type or "").upper()
            if req_line == "MIKA":
                if code.startswith("WD-VAK-") or ltype == "WADAH_VAKUM":
                    raise HTTPException(
                        status_code=400,
                        detail=f"Akses Ditolak: Wadah {code} adalah nampan Vakum, tidak boleh diterima di Line Dimsum Mika."
                    )
            elif req_line == "VAKUM":
                if code.startswith("WD-MIKA-") or ltype == "WADAH_MIKA":
                    raise HTTPException(
                        status_code=400,
                        detail=f"Akses Ditolak: Wadah {code} adalah nampan Mika, tidak boleh diterima di Line Dimsum Vakum."
                    )

    station_name = "PACKING_MIKA_STATION" if req_line == "MIKA" else ("PACKING_VAKUM_STATION" if req_line == "VAKUM" else "PACKING_STATION")
    updated_trays = []
    try:
        for tray in trays:
            tray.current_status = "IN_PACKING_STATION"
            tray.current_station = station_name

            log = CustodyTransferLog(
                lot_qr_code=tray.lot_qr_code,
                action_type="TRAYS_RECEIVED",
                station_id=station_name,
                target_station=None,
                user_id=payload.operator_id,
                user_role_active=payload.operator_role,
                notes=payload.notes or f"Terima fisik {tray.quantity} wadah besi di {station_name}"
            )
            db.add(log)
            updated_trays.append(tray)

        await db.commit()
        for tray in updated_trays:
            await db.refresh(tray)
        return updated_trays

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat menerima wadah di packing: {str(e)}"
        )


# 10. PACKING MENJADI PRODUK JADI & CETAK LABEL FG (LANGSUNG DARI CETAK)
@router.post("/complete-packing-to-fg", response_model=List[LotResponse], status_code=status.HTTP_201_CREATED)
async def complete_packing_to_fg(
    payload: CompletePackingToFgRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_division(["PACKING", "CETAK", "ASSEMBLY"]))
):
    """
    Operator Packing mengemas hasil cetak menjadi kemasan ritel resmi (Mika & Vakum).
    Bahan adonan & kulit (atau wadah legacy jika ada) ditandai CONSUMED,
    dan label QR Finished Goods diterbitkan langsung:
    - FG-MIKA-YYYYMMDD-XXX (@ 50 pcs)
    - FG-VAKUM-YYYYMMDD-XXX (@ 25 pcs)
    Status langsung PACKED_VERIFIED (tercatat di laporan produksi).
    """
    if payload.mika_output_qty <= 0 and payload.vakum_output_qty <= 0:
        raise HTTPException(status_code=400, detail="Jumlah produk jadi minimal 1 mika atau 1 vakum.")

    today_str = today_wib()
    created_fg_lots = []

    try:
        # A. Jika ada tray_lot_codes (legacy / backward compatibility)
        if payload.tray_lot_codes:
            clean_trays = [c.strip().upper() for c in payload.tray_lot_codes if c.strip()]
            q_trays = select(LotTracking).where(LotTracking.lot_qr_code.in_(clean_trays))
            res_trays = await db.execute(q_trays)
            trays = res_trays.scalars().all()

            for tr in trays:
                tr.current_status = "CONSUMED"
                log_tr = CustodyTransferLog(
                    lot_qr_code=tr.lot_qr_code,
                    action_type="CONSUMED",
                    station_id="PACKING_STATION",
                    target_station=None,
                    user_id=payload.operator_id,
                    user_role_active=payload.operator_role,
                    notes=f"Wadah dikemas: {payload.mika_output_qty} mika, {payload.vakum_output_qty} vakum, reject {payload.reject_pcs} pcs"
                )
                db.add(log_tr)
        else:
            # B. ALUR LANGSUNG CETAK KE PACKING
            # Pengurangan bahan tidak dipaksa auto-FIFO sistem; tim produksi mengelola via media manual /adjust-material-stock.
            # Jika lot adonan atau kulit dikirim secara eksplisit, tandai lot tersebut sebagai CONSUMED:
            if payload.dough_lot_codes:
                clean_dough = [c.strip().upper() for c in payload.dough_lot_codes if c.strip()]
                if clean_dough:
                    q_dough = select(LotTracking).where(LotTracking.lot_qr_code.in_(clean_dough))
                    res_dough = await db.execute(q_dough)
                    for dl in res_dough.scalars().all():
                        dl.current_status = "CONSUMED"
                        db.add(CustodyTransferLog(
                            lot_qr_code=dl.lot_qr_code,
                            action_type="CONSUMED",
                            station_id="PACKING_STATION",
                            target_station=None,
                            user_id=payload.operator_id,
                            user_role_active=payload.operator_role,
                            notes=f"Adonan ditandai terpakai saat packing {payload.mika_output_qty} mika, {payload.vakum_output_qty} vakum"
                        ))

            if payload.wrapper_lot_codes:
                clean_wrap = [c.strip().upper() for c in payload.wrapper_lot_codes if c.strip()]
                if clean_wrap:
                    q_wrap = select(LotTracking).where(LotTracking.lot_qr_code.in_(clean_wrap))
                    res_wrap = await db.execute(q_wrap)
                    for wr in res_wrap.scalars().all():
                        wr.current_status = "CONSUMED"
                        db.add(CustodyTransferLog(
                            lot_qr_code=wr.lot_qr_code,
                            action_type="CONSUMED",
                            station_id="PACKING_STATION",
                            target_station=None,
                            user_id=payload.operator_id,
                            user_role_active=payload.operator_role,
                            notes="Kulit ditandai terpakai saat packing"
                        ))

        # Tutup Saos jika disertakan
        if payload.sauce_lot_codes:
            clean_sauce = [c.strip().upper() for c in payload.sauce_lot_codes if c.strip()]
            q_sauce = select(LotTracking).where(LotTracking.lot_qr_code.in_(clean_sauce))
            res_sauce = await db.execute(q_sauce)
            sauces = res_sauce.scalars().all()
            for sc in sauces:
                sc.current_status = "CONSUMED"
                log_sc = CustodyTransferLog(
                    lot_qr_code=sc.lot_qr_code,
                    action_type="CONSUMED",
                    station_id="PACKING_STATION",
                    target_station=None,
                    user_id=payload.operator_id,
                    user_role_active=payload.operator_role,
                    notes=f"Saos dipacking bersama produk jadi"
                )
                db.add(log_sc)

        # 1. Terbitkan QR Dimsum Mika (50 butir)
        if payload.mika_output_qty > 0:
            start_mika_idx = await get_next_lot_index(db, "FG-MIKA")
            for i in range(payload.mika_output_qty):
                code = f"FG-MIKA-{today_str}-{str(start_mika_idx + i).zfill(3)}"
                fg_mika = LotTracking(
                    lot_qr_code=code,
                    plan_id=payload.plan_id,
                    lot_type="FG_MIKA",
                    current_status="READY_IN_ASSEMBLY_STATION",
                    current_station="PACKING_STATION",
                    quantity=50.0,
                    unit="MIKA"
                )
                db.add(fg_mika)

                log_mika = CustodyTransferLog(
                    lot_qr_code=code,
                    action_type="FG_GENERATED",
                    station_id="PACKING_STATION",
                    target_station=None,
                    user_id=payload.operator_id,
                    user_role_active=payload.operator_role,
                    notes=f"Label Dimsum Mika {code} diterbitkan PIC, menunggu verifikasi packing"
                )
                db.add(log_mika)
                created_fg_lots.append(fg_mika)

        # 2. Terbitkan QR Dimsum Vakum (25 butir)
        if payload.vakum_output_qty > 0:
            start_vakum_idx = await get_next_lot_index(db, "FG-VAKUM")
            for j in range(payload.vakum_output_qty):
                code = f"FG-VAKUM-{today_str}-{str(start_vakum_idx + j).zfill(3)}"
                fg_vakum = LotTracking(
                    lot_qr_code=code,
                    plan_id=payload.plan_id,
                    lot_type="FG_VAKUM",
                    current_status="READY_IN_ASSEMBLY_STATION",
                    current_station="PACKING_STATION",
                    quantity=25.0,
                    unit="PACK"
                )
                db.add(fg_vakum)

                log_vakum = CustodyTransferLog(
                    lot_qr_code=code,
                    action_type="FG_GENERATED",
                    station_id="PACKING_STATION",
                    target_station=None,
                    user_id=payload.operator_id,
                    user_role_active=payload.operator_role,
                    notes=f"Label Dimsum Vakum {code} diterbitkan PIC, menunggu verifikasi packing"
                )
                db.add(log_vakum)
                created_fg_lots.append(fg_vakum)

        await db.commit()
        for lot in created_fg_lots:
            await db.refresh(lot)
        return created_fg_lots

    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat pengemasan produk jadi: {str(e)}"
        )


# 11. MEDIA PENYESUAIAN / PENGURANGAN STOK BAHAN SECARA MANUAL (DENGAN NOTES)
@router.post("/adjust-material-stock", response_model=LotResponse)
async def adjust_material_stock(
    payload: AdjustMaterialStockRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_division(["CETAK", "ASSEMBLY", "PACKING"]))
):
    """
    Media manual bagi tim Cetak / Packing untuk mencatat penggunaan atau pengurangan stok bahan
    (Adonan, Kulit, Saos) dengan menyertakan catatan / notes operasional riil di meja.
    Mendukung:
    - CONSUME_ALL: menandai lot habis terpakai (status CONSUMED).
    - CONSUME_PARTIAL: mengurangi sejumlah kg/bal dari stok lot, dengan catatan penggunaan.
    """
    clean_code = payload.lot_code.strip().upper()
    query = select(LotTracking).where(LotTracking.lot_qr_code == clean_code)
    result = await db.execute(query)
    lot = result.scalar_one_or_none()

    if not lot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Lot bahan {clean_code} tidak ditemukan."
        )

    if lot.current_status == "CONSUMED":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Lot bahan {clean_code} sudah berstatus CONSUMED (habis terpakai)."
        )

    action = payload.action_type.upper().strip()
    qty_used = float(payload.quantity_used or 0.0)
    current_qty = float(lot.quantity or 0.0)

    try:
        if action == "CONSUME_ALL" or (qty_used >= current_qty and qty_used > 0):
            lot.current_status = "CONSUMED"
            is_adonan = clean_code.startswith("AD-") or (lot.lot_type and lot.lot_type.upper() == "ADONAN")
            if is_adonan:
                extra_note = f". {payload.notes}" if payload.notes else ""
                log_notes = f"Stok habis terpakai{extra_note}".strip()
            else:
                log_notes = f"Stok habis terpakai ({current_qty} {lot.unit or 'UNIT'}). {payload.notes or ''}".strip()
            log_action = "CONSUMED"
        elif action == "CONSUME_PARTIAL":
            if qty_used <= 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Jumlah pengurangan harus lebih besar dari 0."
                )
            new_qty = round(max(0.0, current_qty - qty_used), 2)
            lot.quantity = new_qty
            if new_qty <= 0:
                lot.current_status = "CONSUMED"
                log_action = "CONSUMED"
            else:
                log_action = "MATERIAL_USED"
            log_notes = f"Pengurangan stok {qty_used} {lot.unit or 'UNIT'} (Sisa: {new_qty} {lot.unit or 'UNIT'}). {payload.notes or ''}".strip()
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Tipe aksi '{action}' tidak valid. Gunakan CONSUME_ALL atau CONSUME_PARTIAL."
            )

        log = CustodyTransferLog(
            lot_qr_code=lot.lot_qr_code,
            action_type=log_action,
            station_id=lot.current_station or "ASSEMBLY_STATION",
            target_station=None,
            user_id=payload.operator_id,
            user_role_active=payload.operator_role,
            notes=log_notes
        )
        db.add(log)

        await db.commit()
        await db.refresh(lot)
        return lot

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Gagal menyesuaikan stok bahan: {str(e)}"
        )


# 12. METRIK BAHAN 3-KOLOM: DIKIRIM, DIGUNAKAN, SISA (UNTUK BUMBU & KULIT)
@router.post("/record-material-usage", response_model=LotResponse)
async def record_material_usage(
    payload: MaterialUsageRecordRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_division(["CETAK", "ASSEMBLY", "PACKING", "DAGING", "BUMBU", "MEAT_MIXING"]))
):
    """
    Mencatat penggunaan bahan secara riil dengan model 3-Metrik:
    Sisa = Dikirim - Digunakan.
    Mendukung opsi sisa: disimpan di ruangan/meja atau diretur ke Frontdesk.
    """
    clean_code = payload.lot_code.strip().upper()
    query = select(LotTracking).where(LotTracking.lot_qr_code == clean_code)
    result = await db.execute(query)
    lot = result.scalar_one_or_none()

    if not lot:
        raise HTTPException(status_code=404, detail=f"Lot bahan {clean_code} tidak ditemukan.")

    if lot.current_status == "CONSUMED":
        raise HTTPException(status_code=400, detail=f"Lot bahan {clean_code} sudah habis terpakai (CONSUMED).")

    sent = float(payload.quantity_sent)
    used = float(payload.quantity_used)
    if used > sent:
        raise HTTPException(status_code=400, detail="Jumlah digunakan tidak boleh melebihi jumlah dikirim.")

    sisa = round(max(0.0, sent - used), 2)
    action_rem = payload.remaining_action.upper().strip()

    try:
        if sisa <= 0 or used >= sent:
            lot.current_status = "CONSUMED"
            lot.quantity = 0.0
            log_action = "MATERIAL_CONSUMED"
            log_notes = f"Bahan habis terpakai (Digunakan: {used} {lot.unit or 'UNIT'}). {payload.notes or ''}".strip()
        else:
            lot.quantity = sisa
            if action_rem == "RETURN_TO_FRONTDESK":
                lot.current_status = "IN_TRANSIT_TO_FRONTDESK"
                log_action = "MATERIAL_RETURNED"
                log_notes = f"Digunakan: {used} {lot.unit}, Sisa: {sisa} {lot.unit} diretur ke Frontdesk. {payload.notes or ''}".strip()
            else:
                log_action = "MATERIAL_STORED_IN_ROOM"
                log_notes = f"Digunakan: {used} {lot.unit}, Sisa: {sisa} {lot.unit} disimpan di meja. {payload.notes or ''}".strip()

        log = CustodyTransferLog(
            lot_qr_code=lot.lot_qr_code,
            action_type=log_action,
            station_id=lot.current_station or "ASSEMBLY_STATION",
            target_station="FRONTDESK" if action_rem == "RETURN_TO_FRONTDESK" and sisa > 0 else None,
            user_id=payload.operator_id,
            user_role_active=payload.operator_role,
            notes=log_notes
        )
        db.add(log)

        await db.commit()
        await db.refresh(lot)
        return lot

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat mencatat penggunaan bahan: {str(e)}"
        )


# 13. PENJUALAN: MULTI-INPUT VERIFIKASI BERTAHAP (ACC VS ONHOLD)
@router.post("/sales/verify-staged-receipt", response_model=SalesStagedVerificationResponse)
async def verify_staged_sales_receipt(
    payload: SalesStagedVerificationRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_division(["GUDANG", "SALES"]))
):
    """
    Verifikasi fisik bertahap oleh Bagian Penjualan / Toko (bisa diinput berkali-kali):
    - Jika ACC: memindahkan lot kemasan dari dapur ke stok POS & Freezer.
    - Jika ONHOLD: mencatat selisih/cacat kemasan ke antrean audit dan mencatat catatan wajib.
    """
    prod_type = payload.product_id.strip().upper()
    v_status = payload.verification_status.strip().upper()
    actual_qty = int(payload.actual_qty)

    if actual_qty < 0:
        raise HTTPException(status_code=400, detail="Kuantitas fisik tidak boleh bernilai negatif.")

    if v_status not in ["ACC", "ONHOLD"]:
        raise HTTPException(status_code=400, detail="Status verifikasi harus 'ACC' atau 'ONHOLD'.")

    now = datetime.now(timezone.utc)

    try:
        if v_status == "ACC":
            # Cari lot yang siap diverifikasi untuk produk ini
            q_lots = (
                select(LotTracking)
                .where(
                    LotTracking.lot_type == prod_type,
                    LotTracking.current_status.in_(["PACKED_VERIFIED", "READY_IN_ASSEMBLY_STATION", "IN_PACKING_STATION"])
                )
                .order_by(LotTracking.created_at.asc())
                .limit(actual_qty)
            )
            res_lots = await db.execute(q_lots)
            matched_lots = res_lots.scalars().all()

            for lot in matched_lots:
                lot.current_status = "IN_SALES_STOCK"
                lot.current_station = "SALES_STATION"

                log = CustodyTransferLog(
                    lot_qr_code=lot.lot_qr_code,
                    action_type="SALES_STAGED_ACC",
                    station_id="SALES_STATION",
                    target_station="FREEZER",
                    user_id=payload.operator_id,
                    user_role_active=payload.operator_role,
                    notes=payload.notes or "Verifikasi bertahap diterima (Acc): Stok aktif di POS"
                )
                db.add(log)

            msg = f"Berhasil Acc {actual_qty} pack {prod_type} ke stok siap jual POS."

        else: # ONHOLD
            # Catat discrepancy ke discrepancy_logs untuk diaudit PIC
            disc = DiscrepancyLog(
                plan_id=payload.plan_id,
                expected_qty=actual_qty,
                actual_qty=actual_qty,
                variance=0.0,
                unit="PACK",
                notes=f"[ONHOLD AUDIT] {prod_type}: {payload.notes or 'Tertahan untuk pemeriksaan fisik/gramasi'}"
            )
            db.add(disc)

            msg = f"{actual_qty} pack {prod_type} ditandai Onhold untuk audit. Catatan: {payload.notes or 'Pemeriksaan fisik'}"

        await db.commit()

        return SalesStagedVerificationResponse(
            success=True,
            product_id=prod_type,
            verified_qty=actual_qty,
            status=v_status,
            notes=payload.notes,
            timestamp=now,
            message=msg
        )

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat verifikasi penerimaan penjualan: {str(e)}"
        )

