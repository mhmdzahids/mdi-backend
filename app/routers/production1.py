import os
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, delete
from datetime import datetime, timezone, timedelta
from typing import List, Optional

# Timezone WIB (UTC+7) untuk lot code date string
WIB = timezone(timedelta(hours=7))

def today_wib() -> str:
    """Tanggal hari ini dalam format YYYYMMDD sesuai timezone WIB (UTC+7)."""
    return datetime.now(WIB).strftime("%Y%m%d")

from ..database import get_db
from ..auth import require_role, require_division, get_current_user, is_global_supervisor
from ..models import (
    LotTracking,
    CustodyTransferLog,
    MasterRecipe,
    MasterRecipeItem,
    ProductionPlan,
    DiscrepancyLog,
    ScanSetting,
    ScanEvent
)
from ..schemas import (
    GenerateByGilinganRequest,
    ScanOutMeatLotRequest,
    ScanInMeatLotRequest,
    PullScanInMeatLotRequest,
    CompleteMixDoughRequest,
    ScanOutDoughLotRequest,
    CreateWrapperLotRequest,
    ScanOutWrapperLotRequest,
    CreateSauceLotRequest,
    ScanOutSauceLotRequest,
    GenerateSeasoningPouchesRequest,
    GenerateSeasoningSacksRequest,
    ScanOutSeasoningBatchRequest,
    ScanInSeasoningBatchRequest,
    ProduceDoughMixedRequest,
    ProduceBatchUnifiedRequest,
    LotResponse,
    CustodyLogResponse,
    RecipeSchema,
    CreateProductionPlanRequest,
    ProductionPlanResponse,
    ConfirmStockInRequest,
    VoidLotsRequest
)

router = APIRouter(prefix="/production1", tags=["Produksi 1 & Barcode Traceability"])


# ============================================================================
# 0. PRODUCTION PLAN: Buat & Ambil Rencana Produksi Harian
# ============================================================================

@router.post("/plans", response_model=ProductionPlanResponse, status_code=status.HTTP_201_CREATED)
async def create_production_plan(
    payload: CreateProductionPlanRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_role(["STAKEHOLDER"]))
):
    if not is_global_supervisor(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akses ditolak: Hanya Supervisor Global atau PPIC yang berhak mengubah rencana produksi pabrik."
        )

    # Cek apakah plan sudah ada; jika sudah ada, update targetnya
    existing = await db.execute(select(ProductionPlan).where(ProductionPlan.plan_id == payload.plan_id))
    plan = existing.scalar_one_or_none()
    if plan:
        plan.target_gilingan = payload.target_gilingan
        plan.target_mika_pax = payload.target_mika_pax
        plan.target_vakum_pax = payload.target_vakum_pax
        await db.commit()
        await db.refresh(plan)
        return plan

    try:
        plan = ProductionPlan(
            plan_id=payload.plan_id,
            plan_date=payload.plan_date,
            target_gilingan=payload.target_gilingan,
            target_mika_pax=payload.target_mika_pax,
            target_vakum_pax=payload.target_vakum_pax,
            status="ACTIVE",
            created_by=payload.created_by
        )
        db.add(plan)
        await db.commit()
        await db.refresh(plan)
        return plan
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Gagal membuat production plan: {str(e)}"
        )


@router.get("/plans", response_model=List[ProductionPlanResponse])
async def list_production_plans(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Ambil semua rencana produksi, diurutkan dari terbaru."""
    from sqlalchemy import desc as sa_desc
    result = await db.execute(select(ProductionPlan).order_by(sa_desc(ProductionPlan.plan_date)))
    return result.scalars().all()


@router.get("/plans/active", response_model=Optional[ProductionPlanResponse])
async def get_active_plan(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Ambil rencana produksi aktif hari ini (WIB). Jika belum ada, otomatis buat default untuk hari ini."""
    today = datetime.now(WIB).date()
    result = await db.execute(
        select(ProductionPlan)
        .where(ProductionPlan.plan_date == today, ProductionPlan.status == "ACTIVE")
        .order_by(desc(ProductionPlan.plan_id))
        .limit(1)
    )
    plan = result.scalar_one_or_none()

    if not plan:
        today_str = today.strftime("%Y%m%d")
        plan_id = f"PLAN-{today_str}-01"
        plan = ProductionPlan(
            plan_id=plan_id,
            plan_date=today,
            target_gilingan=40,
            target_mika_pax=0,
            target_vakum_pax=0,
            status="ACTIVE",
            created_by="SYSTEM"
        )
        db.add(plan)
        await db.commit()
        await db.refresh(plan)

    return plan


# Helper untuk mencari index urutan berikutnya berdasarkan prefix dan tanggal hari ini
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

# Helper untuk auto-generate kode lot tunggal
async def generate_lot_code(db: AsyncSession, prefix: str) -> str:
    today_str = today_wib()
    next_idx = await get_next_lot_index(db, prefix)
    return f"{prefix}-{today_str}-{str(next_idx).zfill(3)}"


# 1. DIVISI DAGING: Generate Batch Berdasarkan Jumlah Gilingan (Konversi Otomatis dari Master Recipe)
@router.post("/meat/generate-by-gilingan", response_model=List[LotResponse], status_code=status.HTTP_201_CREATED)
async def generate_meat_lots_by_gilingan(
    payload: GenerateByGilinganRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_division(["DAGING"]))
):
    if current_user.get("role") != "STAKEHOLDER":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akses ditolak: Hanya PIC atau Supervisor yang berhak menerbitkan batch gilingan."
        )

    try:
        # Strictly Avoid Hardcoding: Ambil gramasi daging dari Master Recipe aktif
        recipe_query = select(MasterRecipe).where(MasterRecipe.is_active == True).limit(1)
        recipe_result = await db.execute(recipe_query)
        recipe = recipe_result.scalar_one_or_none()

        meat_input_gram = recipe.meat_input_gram if recipe else 15000.00
        kg_per_gilingan = float(meat_input_gram) / 1000.0

        today_str = today_wib()
        start_idx = await get_next_lot_index(db, "DG")
        created_lots = []
        for i in range(payload.gilingan_count):
            lot_code = f"DG-{today_str}-{str(start_idx + i).zfill(3)}"

            new_lot = LotTracking(
                lot_qr_code=lot_code,
                plan_id=payload.plan_id,
                lot_type="DAGING",
                current_status="PRINTED_PENDING_STOCK_IN",
                current_station="MEAT_STATION",
                quantity=kg_per_gilingan,
                unit="KG"
            )
            db.add(new_lot)

            # Audit Trail: Catat pencetakan label QR oleh PIC
            log = CustodyTransferLog(
                lot_qr_code=lot_code,
                action_type="QR_PRINTED",
                station_id="MEAT_STATION",
                user_id=payload.operator_id,
                user_role_active=payload.operator_role,
                notes=f"Cetak QR Wadah Daging {lot_code} (15 kg)"
            )
            db.add(log)
            created_lots.append(new_lot)

        # Atomic commit seluruh batch
        await db.commit()
        for lot in created_lots:
            await db.refresh(lot)

        return created_lots

    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat generate lot daging: {str(e)}"
        )

# 1b. GILING & MIXING TERPADU: Terbitkan Wadah Adonan Langsung (QR Tunggal AD-...)
@router.post("/meat-mixing/produce-batch", response_model=List[LotResponse], status_code=status.HTTP_201_CREATED)
async def produce_batch_unified(
    payload: ProduceBatchUnifiedRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_division(["DAGING", "BUMBU", "MEAT_MIXING", "PROD_1", "MIXING"]))
):
    """
    Menghasilkan wadah adonan langsung dalam 1 alur terpadu di ruangan Giling & Mixing.
    QR untuk gilingan dan adonan adalah QR yang sama (AD-YYYYMMDD-XXX).
    Diterbitkan dalam bentuk lembar QR rekap utuh untuk dibawa troli ke Frontdesk.
    """
    if current_user.get("role") != "STAKEHOLDER":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akses ditolak: Hanya PIC atau Supervisor yang berhak menerbitkan batch adonan."
        )

    try:
        today_str = today_wib()
        start_idx = await get_next_lot_index(db, "AD")
        created_lots = []

        for i in range(payload.gilingan_count):
            lot_code = f"AD-{today_str}-{str(start_idx + i).zfill(3)}"

            dough_lot = LotTracking(
                lot_qr_code=lot_code,
                plan_id=payload.plan_id,
                lot_type="ADONAN",
                current_status="READY_IN_MEAT_MIXING_STATION",
                current_station="MEAT_MIXING_STATION",
                quantity=payload.dough_yield_kg,
                unit="KG"
            )
            db.add(dough_lot)

            log = CustodyTransferLog(
                lot_qr_code=lot_code,
                action_type="QR_PRINTED",
                station_id="MEAT_MIXING_STATION",
                user_id=payload.operator_id,
                user_role_active=payload.operator_role,
                notes=f"Label disiapkan PIC ({i + 1}/{payload.gilingan_count} gilingan adonan). {payload.notes or ''}".strip()
            )
            db.add(log)
            created_lots.append(dough_lot)

        await db.commit()
        for lot in created_lots:
            await db.refresh(lot)

        return created_lots

    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat proses giling & mixing batch: {str(e)}"
        )


# 1c. VOID / BATALKAN LABEL QR SISA (Tidak Terealisasi / Sisa Rencana)
@router.post("/lots/void", response_model=List[LotResponse])
async def void_lots(
    payload: VoidLotsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Membatalkan (Void) label QR yang tidak jadi diproduksi (misal sisa rencana harian).
    Mencegah stok siluman di sistem dan menjaga akurasi data produksi.
    """
    clean_codes = [c.strip().upper() for c in payload.lot_codes if c.strip()]
    if not clean_codes:
        raise HTTPException(status_code=400, detail="Daftar kode lot kosong.")

    query = select(LotTracking).where(LotTracking.lot_qr_code.in_(clean_codes))
    result = await db.execute(query)
    lots = result.scalars().all()

    if not lots:
        raise HTTPException(status_code=404, detail="Tidak ada lot yang ditemukan untuk dibatalkan.")

    voided_lots = []
    for lot in lots:
        non_voidable = ["IN_ASSEMBLY_STATION", "CONSUMED", "CLOSED"]
        if lot.current_status in non_voidable:
            continue

        lot.current_status = "VOID"

        log = CustodyTransferLog(
            lot_qr_code=lot.lot_qr_code,
            action_type="LOT_VOIDED",
            station_id=lot.current_station or "MEAT_MIXING_STATION",
            user_id=payload.operator_id or current_user.get("sub", "PIC"),
            user_role_active=payload.operator_role or current_user.get("role", "STAKEHOLDER"),
            notes=f"Label QR dibatalkan PIC: {payload.reason or 'Sisa rencana tidak terealisasi'}"
        )
        db.add(log)
        voided_lots.append(lot)

    await db.commit()
    for lot in voided_lots:
        await db.refresh(lot)

    return voided_lots



# 2. DIVISI DAGING: DUAL-SCAN Step 1 - Scan-OUT Pengiriman ke Divisi Bumbu
@router.post("/meat/scan-out", response_model=LotResponse)
async def scan_out_meat_lot(
    payload: ScanOutMeatLotRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_division(["DAGING"]))
):
    clean_code = payload.lot_code.strip().upper()

    # Validasi Kategori Barcode (Gatekeeping)
    if not clean_code.startswith("DG-"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Validasi gagal: Barcode '{clean_code}' bukan QR Daging (harus berawalan DG-)."
        )

    query = select(LotTracking).where(LotTracking.lot_qr_code == clean_code)
    result = await db.execute(query)
    lot = result.scalar_one_or_none()

    if not lot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Lot Daging '{clean_code}' tidak ditemukan di sistem."
        )

    # Idempotent: jika sudah status in-transit
    if lot.current_status == "IN_TRANSIT_TO_SEASONING":
        return lot

    if lot.current_status == "IN_SEASONING_STATION":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Lot Daging '{clean_code}' sudah diterima di Stasiun Bumbu."
        )

    if lot.current_status == "CONSUMED":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Lot Daging '{clean_code}' sudah selesai di-mix (CONSUMED)."
        )

    if lot.current_status != "READY_IN_MEAT_STATION":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Lot {clean_code} belum siap dikirim. Pastikan sudah discan masuk stok."
        )

    try:
        lot.current_status = "IN_TRANSIT_TO_SEASONING"

        log = CustodyTransferLog(
            lot_qr_code=lot.lot_qr_code,
            action_type="SCAN_OUT",
            station_id="MEAT_STATION",
            target_station=payload.target_station,
            user_id=payload.operator_id,
            user_role_active=payload.operator_role,
            notes="Kirim Daging ke Bagian Mixer"
        )
        db.add(log)

        await db.commit()
        await db.refresh(lot)
        return lot

    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat eksekusi Scan-OUT daging: {str(e)}"
        )


# 3. DIVISI BUMBU: DUAL-SCAN Step 2 - Scan-IN Penerimaan Wadah Daging dari Daging
@router.post("/seasoning/scan-in", response_model=LotResponse)
async def scan_in_meat_lot(
    payload: ScanInMeatLotRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_division(["BUMBU"]))
):
    clean_code = payload.lot_code.strip().upper()

    # Validasi Kategori Scan (Wajib awalan DG-)
    if not clean_code.startswith("DG-"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Validasi gagal: Barcode '{clean_code}' bukan QR Daging (harus berawalan DG-)."
        )

    query = select(LotTracking).where(LotTracking.lot_qr_code == clean_code)
    result = await db.execute(query)
    lot = result.scalar_one_or_none()

    if not lot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Lot Daging '{clean_code}' tidak ditemukan di sistem."
        )

    if lot.current_status == "CONSUMED":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Lot Daging '{clean_code}' sudah berstatus CONSUMED (sudah selesai dicampur)."
        )

    if lot.current_status == "IN_SEASONING_STATION":
        return lot # Idempotent: sudah di stasiun bumbu

    # DUAL-SCAN GATEKEEPING ENFORCEMENT
    notes = "Scan-IN diterima di Divisi Bumbu"
    if lot.current_status == "READY_IN_MEAT_STATION":
        if payload.allow_supervisor_bypass:
            expected_pin = os.getenv("SUPERVISOR_PIN", "8888")
            if not payload.supervisor_pin or str(payload.supervisor_pin).strip() != expected_pin:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Otorisasi Supervisor gagal: PIN Supervisor tidak valid atau tidak disertakan."
                )
            notes = f"SUPERVISOR BYPASS: {payload.bypass_reason or 'Otorisasi darurat melewati Scan-OUT Daging'}"
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Validasi Dual-Scan Gagal: Wadah '{clean_code}' belum di-Scan OUT oleh Divisi Daging. "
                    f"Minta operator daging melakukan Scan OUT terlebih dahulu, atau gunakan otorisasi supervisor bypass."
                )
            )
    elif lot.current_status != "IN_TRANSIT_TO_SEASONING":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Lot berstatus '{lot.current_status}'. Hanya lot IN_TRANSIT_TO_SEASONING yang dapat di-Scan IN ke Stasiun Bumbu."
        )

    # Rollback Safety (Atomic Transaction)
    try:
        lot.current_status = "IN_SEASONING_STATION"
        lot.current_station = "SEASONING_STATION"

        log = CustodyTransferLog(
            lot_qr_code=lot.lot_qr_code,
            action_type="SCAN_IN",
            station_id="SEASONING_STATION",
            user_id=payload.operator_id,
            user_role_active=payload.operator_role,
            notes=notes
        )
        db.add(log)

        await db.commit()
        await db.refresh(lot)
        return lot

    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat eksekusi Scan-IN bumbu: {str(e)}"
        )


# 4. DIVISI BUMBU: Selesaikan Mixing & Terbitkan QR Adonan (Siap di-Scan OUT ke Assembly)
@router.post("/seasoning/complete-mix", response_model=LotResponse, status_code=status.HTTP_201_CREATED)
async def complete_mix_dough(
    payload: CompleteMixDoughRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_division(["BUMBU"]))
):
    clean_parent = payload.parent_meat_lot_code.strip().upper()
    query = select(LotTracking).where(LotTracking.lot_qr_code == clean_parent)
    result = await db.execute(query)
    meat_lot = result.scalar_one_or_none()

    if not meat_lot:
        raise HTTPException(status_code=404, detail=f"Lot Daging {clean_parent} tidak ditemukan.")

    if meat_lot.current_status != "IN_SEASONING_STATION":
        raise HTTPException(
            status_code=400,
            detail=f"Lot Daging harus berstatus IN_SEASONING_STATION sebelum di-mixing. Status saat ini: '{meat_lot.current_status}'."
        )

    try:
        # 1. Update status daging jadi CONSUMED
        meat_lot.current_status = "CONSUMED"

        # 2. Buat QR Adonan baru dengan status READY_IN_SEASONING_STATION (siap di-Scan OUT ke Assembly)
        dough_lot_code = await generate_lot_code(db, "AD")
        dough_lot = LotTracking(
            lot_qr_code=dough_lot_code,
            plan_id=meat_lot.plan_id,
            lot_type="ADONAN",
            current_status="READY_IN_SEASONING_STATION",
            current_station="SEASONING_STATION",
            quantity=payload.dough_yield_kg,
            unit="KG"
        )
        db.add(dough_lot)

        # 3. Log custody
        log = CustodyTransferLog(
            lot_qr_code=dough_lot_code,
            action_type="MIXING_COMPLETED",
            station_id="SEASONING_STATION",
            user_id=payload.operator_id,
            user_role_active=payload.operator_role,
            notes=f"Adonan dimixing dari parent lot {clean_parent}. Hasil timbang: {payload.dough_yield_kg} kg."
        )
        db.add(log)

        await db.commit()
        await db.refresh(dough_lot)
        return dough_lot

    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat menyelesaikan mixing adonan: {str(e)}"
        )


# 5. DIVISI BUMBU: DUAL-SCAN - Scan-OUT Adonan ke Assembly (Produksi 2)
@router.post("/seasoning/scan-out-dough", response_model=LotResponse)
async def scan_out_dough_lot(
    payload: ScanOutDoughLotRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_division(["BUMBU", "DAGING"]))
):
    clean_code = payload.lot_code.strip().upper()

    if not clean_code.startswith("AD-"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Validasi gagal: Barcode '{clean_code}' bukan QR Adonan (harus berawalan AD-)."
        )

    query = select(LotTracking).where(LotTracking.lot_qr_code == clean_code)
    result = await db.execute(query)
    lot = result.scalar_one_or_none()

    if not lot:
        raise HTTPException(status_code=404, detail=f"Lot Adonan '{clean_code}' tidak ditemukan.")

    if lot.current_status == "IN_TRANSIT_TO_ASSEMBLY":
        return lot

    if lot.current_status not in ["READY_IN_MEAT_MIXING_STATION", "READY_IN_SEASONING_STATION"]:
        raise HTTPException(
            status_code=400,
            detail=f"Lot berstatus '{lot.current_status}'. Hanya lot berstatus READY yang dapat di-Scan OUT ke Assembly."
        )

    try:
        lot.current_status = "IN_TRANSIT_TO_ASSEMBLY"

        log = CustodyTransferLog(
            lot_qr_code=lot.lot_qr_code,
            action_type="SCAN_OUT",
            station_id=lot.current_station or "MEAT_MIXING_STATION",
            target_station=payload.target_station,
            user_id=payload.operator_id,
            user_role_active=payload.operator_role,
            notes="Scan-OUT Adonan ke Stasiun Assembly (Produksi 2)"
        )
        db.add(log)

        await db.commit()
        await db.refresh(lot)
        return lot

    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat Scan-OUT adonan: {str(e)}"
        )


# 6. DIVISI KULIT: Terbitkan Bal Kulit (1 QR per 1 Bal Fisik @ 15 kg)
@router.post("/wrapper/generate", response_model=List[LotResponse], status_code=status.HTTP_201_CREATED)
async def create_wrapper_lot(
    payload: CreateWrapperLotRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_division(["KULIT"]))
):
    if current_user.get("role") != "STAKEHOLDER":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akses ditolak: Hanya PIC atau Supervisor yang berhak menerbitkan bal kulit."
        )

    try:
        w_type = payload.wrapper_type.upper()
        if "7CM" in w_type or "VAKUM" in w_type:
            type_name = "Kulit Dimsum 7 cm (Vakum)"
            w_lot_type = "KULIT_7CM"
        else:
            type_name = "Kulit Dimsum 9 cm (Mika)"
            w_lot_type = "KULIT_9CM"

        bal_count = max(1, int(payload.bal_qty))
        weight_per_bal = float(payload.weight_kg) if payload.weight_kg and payload.weight_kg > 0 else 15.0

        today_str = today_wib()
        start_idx = await get_next_lot_index(db, "KL")
        created_lots = []

        for i in range(bal_count):
            lot_code = f"KL-{today_str}-{str(start_idx + i).zfill(3)}"
            wrapper_lot = LotTracking(
                lot_qr_code=lot_code,
                plan_id=payload.plan_id,
                lot_type=w_lot_type,
                current_status="READY_IN_WRAPPER_STATION",
                current_station="WRAPPER_STATION",
                quantity=1.0,
                unit="BAL"
            )
            db.add(wrapper_lot)

            log_note = payload.notes if payload.notes else (
                f"Cetak QR Bal Kulit Supplier Luar {lot_code} (1 Bal / 15 kg, {type_name})"
                if (payload.source_type or "").upper() == "EXTERNAL"
                else f"Cetak QR Bal Kulit {lot_code} (1 Bal / 15 kg, {type_name})"
            )
            log = CustodyTransferLog(
                lot_qr_code=lot_code,
                action_type="QR_PRINTED",
                station_id="WRAPPER_STATION",
                user_id=payload.operator_id,
                user_role_active=payload.operator_role,
                notes=log_note
            )
            db.add(log)
            created_lots.append(wrapper_lot)

        await db.commit()
        for lot in created_lots:
            await db.refresh(lot)
        return created_lots

    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat generate bal kulit: {str(e)}"
        )


# 7. DIVISI KULIT: DUAL-SCAN - Scan-OUT Pack Kulit ke Assembly (Produksi 2)
@router.post("/wrapper/scan-out", response_model=LotResponse)
async def scan_out_wrapper_lot(
    payload: ScanOutWrapperLotRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_division(["KULIT"]))
):
    clean_code = payload.lot_code.strip().upper()

    if not clean_code.startswith("KL-"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Validasi gagal: Barcode '{clean_code}' bukan QR Kulit (harus berawalan KL-)."
        )

    query = select(LotTracking).where(LotTracking.lot_qr_code == clean_code)
    result = await db.execute(query)
    lot = result.scalar_one_or_none()

    if not lot:
        raise HTTPException(status_code=404, detail=f"Lot Kulit '{clean_code}' tidak ditemukan.")

    if lot.current_status == "IN_TRANSIT_TO_ASSEMBLY":
        return lot

    if lot.current_status not in ["READY_IN_WRAPPER_STATION", "PRINTED_PENDING_STOCK_IN"]:
        raise HTTPException(
            status_code=400,
            detail=f"Lot {clean_code} tidak dapat dikirim (status: {lot.current_status})."
        )

    try:
        lot.current_status = "IN_TRANSIT_TO_ASSEMBLY"

        log = CustodyTransferLog(
            lot_qr_code=lot.lot_qr_code,
            action_type="SCAN_OUT",
            station_id="WRAPPER_STATION",
            target_station=payload.target_station,
            user_id=payload.operator_id,
            user_role_active=payload.operator_role,
            notes="Kirim Kulit ke Bagian Cetak"
        )
        db.add(log)

        await db.commit()
        await db.refresh(lot)
        return lot

    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat Scan-OUT kulit: {str(e)}"
        )


# 7b. DIVISI SAOS: Terbitkan Bal Saos (1 QR per 1 Bal Fisik)
# Dimsum Mini: 1 Bal = 200 plastik saos (es mambo)
# Dimsum Reguler: 1 Bal = 50 pouch saos
@router.post("/sauce/generate", response_model=List[LotResponse], status_code=status.HTTP_201_CREATED)
async def create_sauce_lot(
    payload: CreateSauceLotRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_division(["SAOS"]))
):
    if current_user.get("role") != "STAKEHOLDER":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akses ditolak: Hanya PIC atau Supervisor yang berhak menerbitkan pack saos."
        )

    try:
        s_type = payload.sauce_type.upper()
        is_mini = "MINI" in s_type
        type_name = "Saos Dimsum Mini (200 Plastik)" if is_mini else "Saos Dimsum Reguler (50 Pouch)"
        items_per_bal = 200 if is_mini else 50
        unit_name = "PLASTIK" if is_mini else "POUCH"

        bal_count = max(1, int(payload.bal_qty))
        today_str = today_wib()
        start_idx = await get_next_lot_index(db, "SA")
        created_lots = []

        for i in range(bal_count):
            lot_code = f"SA-{today_str}-{str(start_idx + i).zfill(3)}"
            sauce_lot = LotTracking(
                lot_qr_code=lot_code,
                plan_id=payload.plan_id,
                lot_type="SAOS",
                current_status="READY_IN_SAUCE_STATION",
                current_station="SAUCE_STATION",
                quantity=1.0,
                unit="BAL"
            )
            db.add(sauce_lot)

            log = CustodyTransferLog(
                lot_qr_code=lot_code,
                action_type="QR_PRINTED",
                station_id="SAUCE_STATION",
                user_id=payload.operator_id,
                user_role_active=payload.operator_role,
                notes=f"Cetak QR Bal Saos {lot_code} (1 Bal / {items_per_bal} {unit_name}, {type_name})"
            )
            db.add(log)
            created_lots.append(sauce_lot)

        await db.commit()
        for lot in created_lots:
            await db.refresh(lot)
        return created_lots

    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat generate bal saos: {str(e)}"
        )


# 7c. DIVISI SAOS: DUAL-SCAN - Scan-OUT Bal Saos ke Bagian Finishing
@router.post("/sauce/scan-out", response_model=LotResponse)
async def scan_out_sauce_lot(
    payload: ScanOutSauceLotRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_division(["SAOS"]))
):
    clean_code = payload.lot_code.strip().upper()

    if not clean_code.startswith("SA-"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Validasi gagal: Barcode '{clean_code}' bukan QR Saos (harus berawalan SA-)."
        )

    query = select(LotTracking).where(LotTracking.lot_qr_code == clean_code)
    result = await db.execute(query)
    lot = result.scalar_one_or_none()

    if not lot:
        raise HTTPException(status_code=404, detail=f"Lot Saos '{clean_code}' tidak ditemukan.")

    if lot.current_status == "IN_TRANSIT_TO_FINISHING":
        return lot

    if lot.current_status not in ["READY_IN_SAUCE_STATION", "PRINTED_PENDING_STOCK_IN"]:
        raise HTTPException(
            status_code=400,
            detail=f"Lot {clean_code} tidak dapat dikirim (status: {lot.current_status})."
        )

    try:
        lot.current_status = "IN_TRANSIT_TO_FINISHING"

        log = CustodyTransferLog(
            lot_qr_code=lot.lot_qr_code,
            action_type="SCAN_OUT",
            station_id="SAUCE_STATION",
            target_station=payload.target_station or "FINISHING_STATION",
            user_id=payload.operator_id,
            user_role_active=payload.operator_role,
            notes="Kirim Saos ke Bagian Finishing"
        )
        db.add(log)

        await db.commit()
        await db.refresh(lot)
        return lot

    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat Scan-OUT saos: {str(e)}"
        )


# 8. MONITORING IN-TRANSIT: Seluruh lot yang sedang dalam perpindahan antar-stasiun
@router.get("/in-transit", response_model=List[LotResponse])
async def list_in_transit_lots(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    query = select(LotTracking).where(LotTracking.current_status.like("IN_TRANSIT_%")).order_by(desc(LotTracking.created_at))
    result = await db.execute(query)
    return result.scalars().all()


# Query Daftar Lot Aktif
@router.get("/lots", response_model=List[LotResponse])
async def list_lots(
    lot_type: str = None,
    status: str = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    query = select(LotTracking).order_by(desc(LotTracking.created_at))
    if lot_type:
        query = query.where(LotTracking.lot_type == lot_type)
    if status:
        query = query.where(LotTracking.current_status == status)

    result = await db.execute(query)
    return result.scalars().all()


# Query Audit Trail
@router.get("/custody-logs", response_model=List[CustodyLogResponse])
async def list_custody_logs(
    limit: int = 500,
    station: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    query = select(CustodyTransferLog)
    if station and station.upper() != "ALL":
        clean_st = station.upper().strip()
        query = query.where(
            (CustodyTransferLog.station_id == clean_st) |
            (CustodyTransferLog.target_station == clean_st)
        )
    query = query.order_by(desc(CustodyTransferLog.timestamp)).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()


# 9. DIVISI BUMBU SPARTA: Sistem Karung Sak (1 Karung = N Porsi Gilingan, Default 20 Gilingan / Karung)
@router.post("/seasoning/generate-sacks", response_model=List[LotResponse], status_code=status.HTTP_201_CREATED)
@router.post("/seasoning/generate-pouches", response_model=List[LotResponse], status_code=status.HTTP_201_CREATED)
async def generate_seasoning_sacks(
    payload: GenerateSeasoningPouchesRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_division(["BUMBU"]))
):
    if current_user.get("role") != "STAKEHOLDER":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akses ditolak: Hanya PIC atau Supervisor yang berhak menerbitkan karung bumbu."
        )

    try:
        portions_per_sack = payload.portions_per_sack or payload.pouches_per_bag or 20
        # Jika client mengirim sack_count gunakan itu, jika mengirim pouch_count konversi ke jumlah karung
        if payload.sack_count and payload.sack_count > 0:
            sack_count = payload.sack_count
        elif payload.pouch_count and payload.pouch_count > 0:
            sack_count = max(1, (payload.pouch_count + portions_per_sack - 1) // portions_per_sack)
        else:
            sack_count = 1

        today_str = today_wib()
        start_idx = await get_next_lot_index(db, "BM")
        created_lots = []
        for i in range(sack_count):
            lot_code = f"BM-{today_str}-{str(start_idx + i).zfill(3)}"
            new_lot = LotTracking(
                lot_qr_code=lot_code,
                plan_id=payload.plan_id,
                lot_type="BUMBU",
                current_status="READY_IN_SEASONING_STATION",
                current_station="SEASONING_STATION",
                quantity=float(portions_per_sack),
                unit="PORSI"
            )
            db.add(new_lot)

            log = CustodyTransferLog(
                lot_qr_code=lot_code,
                action_type="QR_PRINTED",
                station_id="SEASONING_STATION",
                user_id=payload.operator_id,
                user_role_active=payload.operator_role,
                notes=f"Cetak QR Karung Bumbu {lot_code} ({portions_per_sack} kresek)"
            )
            db.add(log)
            created_lots.append(new_lot)

        await db.commit()
        for lot in created_lots:
            await db.refresh(lot)
        return created_lots

    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat generate karung bumbu sparta: {str(e)}"
        )


# 9b. KONFIRMASI PENEMPELAN QR & STOCK-IN FISIK OLEH PEGAWAI
@router.post("/lot/confirm-stock-in", response_model=List[LotResponse])
async def confirm_stock_in(payload: ConfirmStockInRequest, db: AsyncSession = Depends(get_db)):
    """
    Pegawai menempelkan label QR ke produk fisik, lalu men-scan QR untuk
    mengonfirmasi bahwa produk fisik sudah selesai dan resmi masuk ke stok stasiun (Stock-In).
    Mengubah status dari PRINTED_PENDING_STOCK_IN -> READY_IN_<STATION>.
    """
    try:
        if not payload.lot_codes:
            raise HTTPException(status_code=400, detail="Daftar kode lot tidak boleh kosong.")

        clean_codes = [c.strip().upper() for c in payload.lot_codes if c.strip()]
        query = select(LotTracking).where(LotTracking.lot_qr_code.in_(clean_codes))
        result = await db.execute(query)
        all_lots = result.scalars().all()

        if not all_lots:
            raise HTTPException(status_code=404, detail="Kode lot tidak ditemukan di database.")

        updated_lots = []
        for lot in all_lots:
            # Jika sudah pernah di-stock in (idempoten)
            if lot.current_status != "PRINTED_PENDING_STOCK_IN":
                updated_lots.append(lot)
                continue

            # Petakan target status dan stasiun berdasarkan lot_type
            if lot.lot_type == "BUMBU":
                target_status = "READY_IN_SEASONING_STATION"
                station = "SEASONING_STATION"
                item_desc = f"Karung Bumbu Sparta ({int(lot.quantity)} porsi)"
            elif lot.lot_type == "DAGING":
                target_status = "READY_IN_MEAT_STATION"
                station = "MEAT_STATION"
                item_desc = "Wadah Daging Giling (15 kg)"
                if payload.actual_weight_kg and payload.actual_weight_kg > 0:
                    lot.quantity = payload.actual_weight_kg
            elif lot.lot_type == "KULIT":
                target_status = "READY_IN_WRAPPER_STATION"
                station = "WRAPPER_STATION"
                item_desc = "Bal Kulit Dimsum (15 kg)"
                if payload.actual_weight_kg and payload.actual_weight_kg > 0:
                    lot.quantity = payload.actual_weight_kg / 15.0
            elif lot.lot_type == "SAOS":
                target_status = "READY_IN_SAUCE_STATION"
                station = "SAUCE_STATION"
                item_desc = "Bal Saos Dimsum"
            elif lot.lot_type == "ADONAN":
                target_status = "READY_IN_MEAT_MIXING_STATION"
                station = "MEAT_MIXING_STATION"
                item_desc = f"Wadah Adonan ({lot.quantity} kg)"
            else:
                target_status = f"READY_IN_{lot.current_station}"
                station = lot.current_station
                item_desc = f"Lot {lot.lot_type}"

            lot.current_status = target_status
            lot.current_station = station

            log = CustodyTransferLog(
                lot_qr_code=lot.lot_qr_code,
                action_type="STOCK_IN",
                station_id=station,
                target_station=None,
                user_id=payload.operator_id,
                user_role_active=payload.operator_role,
                notes=payload.notes or f"Pegawai konfirmasi fisik {item_desc} {lot.lot_qr_code} selesai & label tertempel."
            )
            db.add(log)
            updated_lots.append(lot)

        await db.commit()
        for lot in updated_lots:
            await db.refresh(lot)

        return updated_lots

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat konfirmasi stock-in: {str(e)}"
        )


# 10. DIVISI BUMBU SPARTA: Batch Scan-OUT ke Ruang Giling & Mixer
@router.post("/seasoning/scan-out-batch", response_model=List[LotResponse])
async def scan_out_seasoning_batch(
    payload: ScanOutSeasoningBatchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_division(["BUMBU"]))
):
    try:
        if payload.lot_codes:
            query = select(LotTracking).where(LotTracking.lot_qr_code.in_(payload.lot_codes))
            result = await db.execute(query)
            all_lots = result.scalars().all()

            if not all_lots:
                raise HTTPException(status_code=404, detail="Kode lot bumbu tidak ditemukan di database.")

            # Filter lot yang siap dikirim
            ready_lots = [l for l in all_lots if l.current_status in ["READY_IN_SEASONING_STATION", "PRINTED_PENDING_STOCK_IN"]]
            already_transit = [l for l in all_lots if l.current_status == "IN_TRANSIT_TO_MEAT_MIXING"]

            # Jika semua lot sudah dalam transit, kembalikan data (idempotent)
            if not ready_lots and already_transit:
                return all_lots

            if not ready_lots:
                statuses = ", ".join([f"{l.lot_qr_code}: {l.current_status}" for l in all_lots])
                raise HTTPException(status_code=400, detail=f"Tidak ada bumbu yang berstatus siap kirim ({statuses}).")

            lots_to_update = ready_lots
        else:
            query = select(LotTracking).where(
                LotTracking.lot_type == "BUMBU",
                LotTracking.current_status.in_(["READY_IN_SEASONING_STATION", "PRINTED_PENDING_STOCK_IN"])
            )
            result = await db.execute(query)
            lots_to_update = result.scalars().all()

            if not lots_to_update:
                raise HTTPException(status_code=400, detail="Tidak ada bumbu Sparta siap kirim yang dapat dikirim.")

        for lot in lots_to_update:
            lot.current_status = "IN_TRANSIT_TO_MEAT_MIXING"
            log = CustodyTransferLog(
                lot_qr_code=lot.lot_qr_code,
                action_type="SCAN_OUT",
                station_id="SEASONING_STATION",
                target_station=payload.target_station,
                user_id=payload.operator_id,
                user_role_active=payload.operator_role,
                notes="Kirim Karung Bumbu ke Bagian Mixer"
            )
            db.add(log)

        await db.commit()
        for lot in lots_to_update:
            await db.refresh(lot)
        return lots_to_update

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat Scan-OUT bumbu sparta: {str(e)}"
        )


# 11. DIVISI GILING & MIXER: Batch Scan-IN Terima Bumbu Sparta
@router.post("/meat/scan-in-bumbu-batch", response_model=List[LotResponse])
async def scan_in_seasoning_batch(
    payload: ScanInSeasoningBatchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_division(["DAGING"]))
):
    try:
        if payload.lot_codes:
            query = select(LotTracking).where(LotTracking.lot_qr_code.in_(payload.lot_codes))
            result = await db.execute(query)
            all_lots = result.scalars().all()

            if not all_lots:
                raise HTTPException(status_code=404, detail="Kode lot bumbu tidak ditemukan di database.")

            # Filter yang sedang transit
            transit_lots = [l for l in all_lots if l.current_status == "IN_TRANSIT_TO_MEAT_MIXING"]
            already_received = [l for l in all_lots if l.current_status == "IN_MEAT_MIXING_STATION"]

            # Jika semua lot sudah diterima sebelumnya, kembalikan tanpa error (idempotent)
            if not transit_lots and already_received:
                return all_lots

            if not transit_lots:
                statuses = ", ".join([f"{l.lot_qr_code}: {l.current_status}" for l in all_lots])
                raise HTTPException(status_code=400, detail=f"Hanya bumbu berstatus IN_TRANSIT_TO_MEAT_MIXING yang dapat diterima ({statuses}).")

            lots_to_update = transit_lots
        else:
            query = select(LotTracking).where(
                LotTracking.lot_type == "BUMBU",
                LotTracking.current_status == "IN_TRANSIT_TO_MEAT_MIXING"
            )
            result = await db.execute(query)
            lots_to_update = result.scalars().all()

            if not lots_to_update:
                raise HTTPException(status_code=400, detail="Tidak ada bumbu Sparta berstatus IN_TRANSIT_TO_MEAT_MIXING untuk diterima.")

        for lot in lots_to_update:
            lot.current_status = "IN_MEAT_MIXING_STATION"
            lot.current_station = "MEAT_MIXING_STATION"
            log = CustodyTransferLog(
                lot_qr_code=lot.lot_qr_code,
                action_type="SCAN_IN",
                station_id="MEAT_MIXING_STATION",
                target_station=None,
                user_id=payload.operator_id,
                user_role_active=payload.operator_role,
                notes="Terima Karung Bumbu di Bagian Mixer"
            )
            db.add(log)

        await db.commit()
        for lot in lots_to_update:
            await db.refresh(lot)
        return lots_to_update

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat Scan-IN bumbu sparta: {str(e)}"
        )


# 12. DIVISI GILING & MIXER: Eksekusi Mixing 15kg Daging + 1 Plastik Sparta -> Terbit AD-...
@router.post("/meat/produce-dough", response_model=List[LotResponse], status_code=status.HTTP_201_CREATED)
async def produce_dough_mixed(
    payload: ProduceDoughMixedRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_division(["DAGING"]))
):
    try:
        # 1. Update wadah daging yang digunakan jadi CONSUMED
        consumed_meat_codes = []
        if payload.meat_lot_codes:
            meat_query = select(LotTracking).where(LotTracking.lot_qr_code.in_(payload.meat_lot_codes))
            meat_result = await db.execute(meat_query)
            for m in meat_result.scalars().all():
                m.current_status = "CONSUMED"
                consumed_meat_codes.append(m.lot_qr_code)
        else:
            meat_query = select(LotTracking).where(
                LotTracking.lot_type == "DAGING",
                LotTracking.current_status == "READY_IN_MEAT_STATION"
            ).order_by(LotTracking.created_at).limit(payload.gilingan_count)
            meat_result = await db.execute(meat_query)
            for m in meat_result.scalars().all():
                m.current_status = "CONSUMED"
                consumed_meat_codes.append(m.lot_qr_code)

        # 2. Update bumbu sparta: Kurangi porsi dari Karung Bumbu di Meja Mixer secara FIFO
        consumed_bumbu_descriptions = []
        portions_needed = payload.gilingan_count

        if payload.bumbu_lot_codes:
            bumbu_query = select(LotTracking).where(
                LotTracking.lot_qr_code.in_(payload.bumbu_lot_codes)
            ).order_by(LotTracking.created_at)
        else:
            bumbu_query = select(LotTracking).where(
                LotTracking.lot_type == "BUMBU",
                LotTracking.current_station == "MEAT_MIXING_STATION",
                LotTracking.current_status.in_(["IN_MEAT_MIXING_STATION", "READY_IN_MEAT_MIXING_STATION"]),
                LotTracking.quantity > 0
            ).order_by(LotTracking.created_at)

        bumbu_result = await db.execute(bumbu_query)
        available_sacks = bumbu_result.scalars().all()

        for sack in available_sacks:
            if portions_needed <= 0:
                break
            current_qty = float(sack.quantity)
            if current_qty <= 0:
                continue

            deduct = min(current_qty, float(portions_needed))
            new_qty = current_qty - deduct
            sack.quantity = new_qty
            portions_needed -= int(deduct)
            consumed_bumbu_descriptions.append(f"{sack.lot_qr_code} (-{int(deduct)} porsi)")

            if new_qty <= 0.001:
                sack.current_status = "CONSUMED"
                sack_note = f"Karung Bumbu {sack.lot_qr_code} habis terpakai (0 porsi tersisa)."
            else:
                sack_note = f"Karung Bumbu {sack.lot_qr_code} terpakai {int(deduct)} porsi (sisa {int(new_qty)} porsi)."

            log = CustodyTransferLog(
                lot_qr_code=sack.lot_qr_code,
                action_type="BUMBU_PORTION_CONSUMED",
                station_id="MEAT_MIXING_STATION",
                target_station=None,
                user_id=payload.operator_id,
                user_role_active=payload.operator_role,
                notes=sack_note
            )
            db.add(log)

        created_dough_lots = []
        today_str = today_wib()
        start_idx = await get_next_lot_index(db, "AD")
        for i in range(payload.gilingan_count):
            dough_lot_code = f"AD-{today_str}-{str(start_idx + i).zfill(3)}"
            meat_ref = consumed_meat_codes[i] if i < len(consumed_meat_codes) else "MANUAL-DAGING"
            bumbu_ref = ", ".join(consumed_bumbu_descriptions) if consumed_bumbu_descriptions else "KARUNG-BUMBU-MIXER"

            new_dough = LotTracking(
                lot_qr_code=dough_lot_code,
                plan_id=payload.plan_id,
                lot_type="ADONAN",
                current_status="PRINTED_PENDING_STOCK_IN",
                current_station="MEAT_MIXING_STATION",
                quantity=payload.dough_yield_kg,
                unit="KG"
            )
            db.add(new_dough)

            log = CustodyTransferLog(
                lot_qr_code=dough_lot_code,
                action_type="DOUGH_MIXED",
                station_id="MEAT_MIXING_STATION",
                user_id=payload.operator_id,
                user_role_active=payload.operator_role,
                notes=f"Mixing selesai: 15kg Daging + Bumbu {bumbu_ref} -> {payload.dough_yield_kg}kg Adonan"
            )
            db.add(log)
            created_dough_lots.append(new_dough)

        await db.commit()
        for dough in created_dough_lots:
            await db.refresh(dough)
        return created_dough_lots

    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error saat produce dough mixed: {str(e)}"
        )


# ============================================================================
# DEV ONLY: Wipe Database & Reset State Lingkungan Pengujian
# ============================================================================
@router.post("/dev/wipe-database", status_code=status.HTTP_200_OK)
async def dev_wipe_database(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    [DEV ONLY] Menghapus seluruh data operasional:
    - Seluruh LotTracking
    - Seluruh CustodyTransferLog
    - Seluruh ScanEvent
    - Seluruh DiscrepancyLog
    - Mereset ProductionPlan ke target default hari ini
    - Mereset ScanSetting ke default GLOBAL '1X'

    Hanya dapat dieksekusi oleh PPIC / Supervisor Global.
    """
    if not is_global_supervisor(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akses ditolak: Operasi wipe database hanya diizinkan untuk PPIC / Supervisor."
        )

    try:
        # 1. Hapus tabel data operasional transaksi secara berurutan sesuai relasi FK
        await db.execute(delete(CustodyTransferLog))
        await db.execute(delete(ScanEvent))
        await db.execute(delete(DiscrepancyLog))
        await db.execute(delete(LotTracking))
        await db.execute(delete(ProductionPlan))
        await db.execute(delete(ScanSetting))

        # 2. Re-seed ProductionPlan default hari ini (WIB)
        today = datetime.now(WIB).date()
        today_str = today.strftime("%Y%m%d")
        plan_id = f"PLAN-{today_str}-01"
        new_plan = ProductionPlan(
            plan_id=plan_id,
            plan_date=today,
            target_gilingan=40,
            target_mika_pax=0,
            target_vakum_pax=0,
            status="ACTIVE",
            created_by=current_user.get("operator_id") or "SYSTEM"
        )
        db.add(new_plan)

        # 3. Re-seed ScanSetting GLOBAL='1X'
        db.add(ScanSetting(
            station_id="GLOBAL",
            scan_mode="1X",
            updated_by=current_user.get("operator_id") or "SYSTEM"
        ))

        await db.commit()

        return {
            "status": "success",
            "message": "Database berhasil di-wipe dan direset ke kondisi awal.",
            "plan_id": plan_id,
            "date": today.isoformat()
        }
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Gagal melakukan wipe database: {str(e)}"
        )




