import os
import math
import json
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, delete, or_, func, cast, Date
from datetime import datetime, timezone, timedelta, date
from typing import List, Optional, Dict, Any

# Timezone WIB (UTC+7) untuk lot code date string
WIB = timezone(timedelta(hours=7))

def today_wib() -> str:
    """Tanggal hari ini dalam format YYYYMMDD sesuai timezone WIB (UTC+7)."""
    return datetime.now(WIB).strftime("%Y%m%d")

from ..services.scan_engine import (
    get_effective_scan_mode,
    normalize_status,
    normalize_station_id,
    get_in_transit_status_name,
    STATION_METADATA,
)
from ..database import get_db
from ..auth import require_role, require_division, get_current_user, is_global_supervisor, require_ppic
from ..models import (
    LotTracking,
    CustodyTransferLog,
    MasterRecipe,
    MasterRecipeItem,
    ProductionPlan,
    ProductionBatch,
    DiscrepancyLog,
    ScanSetting,
    ScanEvent,
    SupplierInbound,
    SupplierOutbound,
    DivisionDailyNote
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
    UpdatePlanDraftRequest,
    ProductionPlanResponse,
    ProductionBatchResponse,
    AddProductionBatchPreviewRequest,
    AddProductionBatchPreviewResponse,
    AddProductionBatchRequest,
    ConfirmStockInRequest,
    VoidLotsRequest,
    SupplierInboundCreate,
    SupplierInboundResponse,
    SupplierOutboundCreate,
    SupplierOutboundResponse,
    DivisionDailyNoteCreate,
    DivisionDailyNoteResponse,
    GenerateDailyLotsAllRequest,
    GeneratePackagingBatchRequest
)

router = APIRouter(prefix="/production1", tags=["Produksi 1 & Barcode Traceability"])


# ============================================================================
# 0. PRODUCTION PLAN & BATCHES: Buat, Auto-Save Draft & Siklus Hidup Rencana
# ============================================================================

async def serialize_plan_with_batches(db: AsyncSession, plan: ProductionPlan) -> dict:
    if not plan:
        return None

    # Cari seluruh batch yang terdaftar untuk plan ini
    b_res = await db.execute(
        select(ProductionBatch)
        .where(ProductionBatch.plan_id == plan.plan_id)
        .order_by(ProductionBatch.batch_no.asc())
    )
    batches_raw = b_res.scalars().all()

    # Cek untuk setiap batch apakah is_voidable
    batch_list = []
    for b in batches_raw:
        counts = {}
        if b.counts_json:
            try:
                counts = json.loads(b.counts_json)
            except Exception:
                counts = {}

        is_voidable = (b.status == "ACTIVE")
        if is_voidable:
            # Cek apakah ada lot di batch ini yang sudah di-scan
            lot_codes_res = await db.execute(
                select(LotTracking.lot_qr_code).where(
                    LotTracking.plan_id == plan.plan_id,
                    LotTracking.batch_no == b.batch_no
                )
            )
            codes = lot_codes_res.scalars().all()
            if codes:
                scan_log_res = await db.execute(
                    select(func.count(CustodyTransferLog.log_id)).where(
                        CustodyTransferLog.lot_qr_code.in_(codes),
                        CustodyTransferLog.action_type.notin_(["QR_PRINTED", "LOT_VOIDED"])
                    )
                )
                scanned_cnt = scan_log_res.scalar() or 0
                if scanned_cnt > 0:
                    is_voidable = False
                else:
                    status_check = await db.execute(
                        select(LotTracking.lot_qr_code).where(
                            LotTracking.plan_id == plan.plan_id,
                            LotTracking.batch_no == b.batch_no,
                            LotTracking.current_status.notin_([
                                "READY_IN_MEAT_MIXING_STATION",
                                "PRINTED_PENDING_STOCK_IN",
                                "VOID"
                            ])
                        )
                    )
                    if status_check.scalars().first():
                        is_voidable = False

        batch_list.append({
            "batch_id": b.batch_id,
            "plan_id": b.plan_id,
            "batch_no": b.batch_no,
            "plan_date": b.plan_date,
            "status": b.status,
            "created_by": b.created_by,
            "reason": b.reason,
            "counts": counts,
            "created_at": b.created_at,
            "is_voidable": is_voidable
        })

    # Tentukan status rencana: jika sudah ada batch aktif, statusnya PUBLISHED
    has_active_batches = any(b["status"] == "ACTIVE" for b in batch_list)
    computed_status = "PUBLISHED" if has_active_batches else (plan.status if plan.status in ["DRAFT", "PUBLISHED"] else "DRAFT")

    return {
        "plan_id": plan.plan_id,
        "plan_date": plan.plan_date,
        "target_gilingan": plan.target_gilingan,
        "target_mika_pax": plan.target_mika_pax,
        "target_vakum_pax": plan.target_vakum_pax,
        "status": computed_status,
        "created_by": plan.created_by,
        "version": plan.version or 1,
        "order_mika": plan.order_mika or 0,
        "order_vakum": plan.order_vakum or 0,
        "bumper_mika": plan.bumper_mika or 0,
        "bumper_vakum": plan.bumper_vakum or 0,
        "manual_gilingan": plan.manual_gilingan,
        "manual_bumbu": plan.manual_bumbu,
        "target_kulit_bal": plan.target_kulit_bal or 0,
        "target_kulit_9cm": plan.target_kulit_9cm or 0,
        "target_kulit_7cm": plan.target_kulit_7cm or 0,
        "target_saos_bal": plan.target_saos_bal or 0,
        "target_saos_mika": plan.target_saos_mika or 0,
        "target_saos_vakum": plan.target_saos_vakum or 0,
        "published_at": plan.published_at,
        "published_by": plan.published_by,
        "updated_at": plan.updated_at,
        "batches": batch_list
    }


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

    existing = await db.execute(select(ProductionPlan).where(ProductionPlan.plan_id == payload.plan_id))
    plan = existing.scalar_one_or_none()
    if plan:
        plan.target_gilingan = payload.target_gilingan
        plan.target_mika_pax = payload.target_mika_pax
        plan.target_vakum_pax = payload.target_vakum_pax
        plan.version = (plan.version or 1) + 1
        plan.updated_at = datetime.utcnow()
        await db.commit()
        await db.refresh(plan)
        return await serialize_plan_with_batches(db, plan)

    try:
        plan = ProductionPlan(
            plan_id=payload.plan_id,
            plan_date=payload.plan_date,
            target_gilingan=payload.target_gilingan,
            target_mika_pax=payload.target_mika_pax,
            target_vakum_pax=payload.target_vakum_pax,
            status="DRAFT",
            version=1,
            created_by=payload.created_by
        )
        db.add(plan)
        await db.commit()
        await db.refresh(plan)
        return await serialize_plan_with_batches(db, plan)
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Gagal membuat production plan: {str(e)}"
        )


@router.put("/plans/draft", response_model=ProductionPlanResponse)
async def update_plan_draft(
    payload: UpdatePlanDraftRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_ppic())
):
    today = datetime.now(WIB).date()
    today_str = today.strftime("%Y%m%d")
    plan_id = payload.plan_id or f"PLAN-{today_str}-01"

    existing = await db.execute(select(ProductionPlan).where(ProductionPlan.plan_id == plan_id))
    plan = existing.scalar_one_or_none()

    if not plan:
        plan = ProductionPlan(
            plan_id=plan_id,
            plan_date=today,
            status="DRAFT",
            version=1,
            created_by=payload.operator_id or current_user.get("operator_id") or "PPIC-AYU"
        )
        db.add(plan)
        await db.commit()
        await db.refresh(plan)

    # Cek apakah sudah PUBLISHED (tidak boleh diedit sebagai draft jika sudah terbit)
    b_res = await db.execute(
        select(ProductionBatch).where(ProductionBatch.plan_id == plan.plan_id, ProductionBatch.status == "ACTIVE")
    )
    if b_res.scalars().first() or plan.status == "PUBLISHED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Rencana hari ini sudah diterbitkan dan dikunci. Gunakan 'Tambah Produksi' untuk menambah kuantitas."
        )

    # Optimistic concurrency check
    if payload.version is not None and plan.version != payload.version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Rencana telah diubah oleh operator lain. Data telah dimuat ulang."
        )

    plan.order_mika = max(0, payload.order_mika or 0)
    plan.order_vakum = max(0, payload.order_vakum or 0)
    plan.bumper_mika = max(0, payload.bumper_mika or 0)
    plan.bumper_vakum = max(0, payload.bumper_vakum or 0)
    plan.manual_gilingan = payload.manual_gilingan
    plan.manual_bumbu = payload.manual_bumbu
    plan.target_mika_pax = plan.order_mika + plan.bumper_mika
    plan.target_vakum_pax = plan.order_vakum + plan.bumper_vakum
    if payload.target_gilingan is not None:
        plan.target_gilingan = payload.target_gilingan
    plan.target_kulit_bal = max(0, payload.target_kulit or 0)
    plan.target_kulit_9cm = max(0, payload.target_kulit_9cm or 0)
    plan.target_kulit_7cm = max(0, payload.target_kulit_7cm or 0)
    plan.target_saos_bal = max(0, payload.target_saos or 0)
    plan.target_saos_mika = max(0, payload.target_saos_mika or 0)
    plan.target_saos_vakum = max(0, payload.target_saos_vakum or 0)
    plan.version = (plan.version or 1) + 1
    plan.updated_at = datetime.utcnow()

    await db.commit()
    await db.refresh(plan)

    return await serialize_plan_with_batches(db, plan)


@router.get("/plans", response_model=List[ProductionPlanResponse])
async def list_production_plans(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Ambil semua rencana produksi, diurutkan dari terbaru."""
    from sqlalchemy import desc as sa_desc
    result = await db.execute(select(ProductionPlan).order_by(sa_desc(ProductionPlan.plan_date)))
    plans = result.scalars().all()
    out = []
    for p in plans:
        out.append(await serialize_plan_with_batches(db, p))
    return out


@router.get("/plans/active", response_model=Optional[ProductionPlanResponse])
async def get_active_plan(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Ambil rencana produksi aktif hari ini (WIB). Jika belum ada, otomatis buat default draft untuk hari ini."""
    today = datetime.now(WIB).date()
    result = await db.execute(
        select(ProductionPlan)
        .where(ProductionPlan.plan_date == today)
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
            target_gilingan=0,
            target_mika_pax=0,
            target_vakum_pax=0,
            status="DRAFT",
            version=1,
            created_by="SYSTEM"
        )
        db.add(plan)
        await db.commit()
        await db.refresh(plan)

    return await serialize_plan_with_batches(db, plan)


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
    current_user: dict = Depends(require_ppic())
):

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
    current_user: dict = Depends(require_ppic())
):
    """
    Menghasilkan wadah adonan langsung dalam 1 alur terpadu di ruangan Giling & Mixing.
    QR untuk gilingan dan adonan adalah QR yang sama (AD-YYYYMMDD-XXX).
    Diterbitkan dalam bentuk lembar QR rekap utuh oleh PPIC (Ayu).
    """

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

    mode = await get_effective_scan_mode(db, "MEAT_STATION")
    norm = normalize_status(lot.current_status)
    if mode == "2X":
        if norm == "DICETAK":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Belum scan in. Lakukan Scan In terlebih dahulu."
            )
        if norm != "DI_STOK" and lot.current_status != "READY_IN_MEAT_STATION":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Lot {clean_code} belum siap dikirim. Pastikan sudah discan masuk stok."
            )
    else:
        if norm not in ["DICETAK", "DI_STOK"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Lot {clean_code} tidak dapat dikirim (status: {lot.current_status})."
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

    mode = await get_effective_scan_mode(db, "MEAT_STATION")
    norm = normalize_status(lot.current_status)
    if mode == "2X":
        if norm == "DICETAK":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Belum scan in. Lakukan Scan In terlebih dahulu."
            )
        if norm != "DI_STOK" and lot.current_status not in ["READY_IN_MEAT_MIXING_STATION", "READY_IN_SEASONING_STATION"]:
            raise HTTPException(
                status_code=400,
                detail=f"Lot berstatus '{lot.current_status}'. Hanya lot berstatus READY yang dapat di-Scan OUT ke Assembly."
            )
    else:
        if norm not in ["DICETAK", "DI_STOK"] and lot.current_status not in ["READY_IN_MEAT_MIXING_STATION", "READY_IN_SEASONING_STATION"]:
            raise HTTPException(
                status_code=400,
                detail=f"Lot berstatus '{lot.current_status}'. Tidak dapat di-Scan OUT ke Assembly."
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
    current_user: dict = Depends(require_ppic())
):

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
                current_status="PRINTED_PENDING_STOCK_IN",
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

    mode = await get_effective_scan_mode(db, "WRAPPER_STATION")
    norm = normalize_status(lot.current_status)
    if mode == "2X":
        if norm == "DICETAK":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Belum scan in. Lakukan Scan In terlebih dahulu."
            )
        if norm != "DI_STOK" and lot.current_status != "READY_IN_WRAPPER_STATION":
            raise HTTPException(
                status_code=400,
                detail=f"Lot {clean_code} tidak dapat dikirim (status: {lot.current_status})."
            )
    else:
        if norm not in ["DICETAK", "DI_STOK"] and lot.current_status not in ["READY_IN_WRAPPER_STATION", "PRINTED_PENDING_STOCK_IN"]:
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
    current_user: dict = Depends(require_ppic())
):

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
                current_status="PRINTED_PENDING_STOCK_IN",
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

    mode = await get_effective_scan_mode(db, "SAUCE_STATION")
    norm = normalize_status(lot.current_status)
    if mode == "2X":
        if norm == "DICETAK":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Belum scan in. Lakukan Scan In terlebih dahulu."
            )
        if norm != "DI_STOK" and lot.current_status != "READY_IN_SAUCE_STATION":
            raise HTTPException(
                status_code=400,
                detail=f"Lot {clean_code} tidak dapat dikirim (status: {lot.current_status})."
            )
    else:
        if norm not in ["DICETAK", "DI_STOK"] and lot.current_status not in ["READY_IN_SAUCE_STATION", "PRINTED_PENDING_STOCK_IN"]:
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

    user_role = (current_user.get("role") or "").upper()
    user_div = (current_user.get("division") or "").upper()
    user_st = (current_user.get("default_station") or "").upper()
    sub = (current_user.get("sub") or "").lower()
    is_admin_supervisor = (
        user_role == "SUPERVISOR" or
        "PPIC" in user_div or
        "SUPERVISOR" in user_div or
        sub in ["supervisor", "ppic", "api_client"]
    )

    # Batasi data riwayat yang dimuat sesuai wewenang divisi & stasiun pengguna (Non-Supervisor)
    if not is_admin_supervisor:
        if user_st == "MEAT" or "DAGING" in user_div or "GILING" in user_div:
            query = query.where(
                (CustodyTransferLog.station_id.in_(["MEAT_STATION", "MEAT_MIXING_STATION", "GRINDING_STATION"])) |
                (CustodyTransferLog.target_station.in_(["MEAT_STATION", "MEAT_MIXING_STATION", "GRINDING_STATION"])) |
                (CustodyTransferLog.lot_qr_code.like("DG-%")) |
                (CustodyTransferLog.lot_qr_code.like("AD-%"))
            ).where(
                ~CustodyTransferLog.lot_qr_code.like("SA-%"),
                ~CustodyTransferLog.lot_qr_code.like("SS-%"),
                ~CustodyTransferLog.lot_qr_code.like("KL-%"),
                ~CustodyTransferLog.lot_qr_code.like("BM-%"),
                ~CustodyTransferLog.lot_qr_code.like("BS-%"),
                ~CustodyTransferLog.lot_qr_code.like("FG-%"),
                ~CustodyTransferLog.lot_qr_code.like("TR-%")
            )
        elif user_st == "SEASONING" or "BUMBU" in user_div:
            query = query.where(
                (CustodyTransferLog.station_id == "SEASONING_STATION") |
                (CustodyTransferLog.target_station == "SEASONING_STATION") |
                (CustodyTransferLog.lot_qr_code.like("BM-%")) |
                (CustodyTransferLog.lot_qr_code.like("BS-%"))
            ).where(
                ~CustodyTransferLog.lot_qr_code.like("SA-%"),
                ~CustodyTransferLog.lot_qr_code.like("SS-%"),
                ~CustodyTransferLog.lot_qr_code.like("KL-%"),
                ~CustodyTransferLog.lot_qr_code.like("DG-%"),
                ~CustodyTransferLog.lot_qr_code.like("AD-%"),
                ~CustodyTransferLog.lot_qr_code.like("FG-%"),
                ~CustodyTransferLog.lot_qr_code.like("TR-%")
            )
        elif user_st == "WRAPPER" or "KULIT" in user_div:
            query = query.where(
                (CustodyTransferLog.station_id.in_(["WRAPPER_STATION", "SKIN_STATION"])) |
                (CustodyTransferLog.target_station.in_(["WRAPPER_STATION", "SKIN_STATION"])) |
                (CustodyTransferLog.lot_qr_code.like("KL-%"))
            ).where(
                ~CustodyTransferLog.lot_qr_code.like("SA-%"),
                ~CustodyTransferLog.lot_qr_code.like("SS-%"),
                ~CustodyTransferLog.lot_qr_code.like("BM-%"),
                ~CustodyTransferLog.lot_qr_code.like("BS-%"),
                ~CustodyTransferLog.lot_qr_code.like("DG-%"),
                ~CustodyTransferLog.lot_qr_code.like("AD-%"),
                ~CustodyTransferLog.lot_qr_code.like("FG-%"),
                ~CustodyTransferLog.lot_qr_code.like("TR-%")
            )
        elif user_st == "SAUCE" or "SAOS" in user_div:
            query = query.where(
                (CustodyTransferLog.station_id == "SAUCE_STATION") |
                (CustodyTransferLog.target_station == "SAUCE_STATION") |
                (CustodyTransferLog.lot_qr_code.like("SA-%")) |
                (CustodyTransferLog.lot_qr_code.like("SS-%"))
            ).where(
                ~CustodyTransferLog.lot_qr_code.like("KL-%"),
                ~CustodyTransferLog.lot_qr_code.like("BM-%"),
                ~CustodyTransferLog.lot_qr_code.like("BS-%"),
                ~CustodyTransferLog.lot_qr_code.like("DG-%"),
                ~CustodyTransferLog.lot_qr_code.like("AD-%"),
                ~CustodyTransferLog.lot_qr_code.like("FG-%"),
                ~CustodyTransferLog.lot_qr_code.like("TR-%")
            )
        elif user_st == "ASSEMBLY" or "CETAK" in user_div:
            query = query.where(
                (CustodyTransferLog.station_id == "ASSEMBLY_STATION") |
                (CustodyTransferLog.target_station == "ASSEMBLY_STATION") |
                (CustodyTransferLog.lot_qr_code.like("TR-%")) |
                (CustodyTransferLog.lot_qr_code.like("AD-%")) |
                (CustodyTransferLog.lot_qr_code.like("KL-%"))
            ).where(
                ~CustodyTransferLog.lot_qr_code.like("SA-%"),
                ~CustodyTransferLog.lot_qr_code.like("SS-%"),
                ~CustodyTransferLog.lot_qr_code.like("BM-%"),
                ~CustodyTransferLog.lot_qr_code.like("BS-%"),
                ~CustodyTransferLog.lot_qr_code.like("DG-%"),
                # Jangan tampilkan QR_PRINTED bahan baku di stasiun cetak (itu dicetak di hulu)
                ~((CustodyTransferLog.action_type == "QR_PRINTED") & ~CustodyTransferLog.lot_qr_code.like("TR-%"))
            )
        elif user_st == "PACKING" or "PACKING" in user_div:
            query = query.where(
                (CustodyTransferLog.station_id.in_(["PACKING_STATION", "FINISHING_STATION", "FREEZER_STATION"])) |
                (CustodyTransferLog.target_station.in_(["PACKING_STATION", "FINISHING_STATION", "FREEZER_STATION"])) |
                (CustodyTransferLog.lot_qr_code.like("FG-%")) |
                (CustodyTransferLog.lot_qr_code.like("TR-%")) |
                (CustodyTransferLog.lot_qr_code.like("SA-%")) |
                (CustodyTransferLog.lot_qr_code.like("SS-%"))
            ).where(
                ~CustodyTransferLog.lot_qr_code.like("DG-%"),
                ~CustodyTransferLog.lot_qr_code.like("AD-%"),
                ~CustodyTransferLog.lot_qr_code.like("BM-%"),
                ~CustodyTransferLog.lot_qr_code.like("BS-%")
            )

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
    current_user: dict = Depends(require_ppic())
):

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
                current_status="PRINTED_PENDING_STOCK_IN",
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
        mode = await get_effective_scan_mode(db, "SEASONING_STATION")
        if payload.lot_codes:
            query = select(LotTracking).where(LotTracking.lot_qr_code.in_(payload.lot_codes))
            result = await db.execute(query)
            all_lots = result.scalars().all()

            if not all_lots:
                raise HTTPException(status_code=404, detail="Kode lot bumbu tidak ditemukan di database.")

            if mode == "2X":
                unscanned = [l for l in all_lots if normalize_status(l.current_status) == "DICETAK"]
                if unscanned:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Belum scan in. Lakukan Scan In terlebih dahulu."
                    )
                ready_lots = [l for l in all_lots if normalize_status(l.current_status) == "DI_STOK"]
            else:
                ready_lots = [l for l in all_lots if normalize_status(l.current_status) in ["DI_STOK", "DICETAK"]]

            already_transit = [l for l in all_lots if normalize_status(l.current_status) == "KELUAR"]

            # Jika semua lot sudah dalam transit, kembalikan data (idempotent)
            if not ready_lots and already_transit:
                return all_lots

            if not ready_lots:
                statuses = ", ".join([f"{l.lot_qr_code}: {l.current_status}" for l in all_lots])
                raise HTTPException(status_code=400, detail=f"Tidak ada bumbu yang berstatus siap kirim ({statuses}).")

            lots_to_update = ready_lots
        else:
            allowed_statuses = ["READY_IN_SEASONING_STATION"] if mode == "2X" else ["READY_IN_SEASONING_STATION", "PRINTED_PENDING_STOCK_IN"]
            query = select(LotTracking).where(
                LotTracking.lot_type == "BUMBU",
                LotTracking.current_status.in_(allowed_statuses)
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
    current_user: dict = Depends(require_ppic())
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
        await db.execute(delete(ProductionBatch))
        await db.execute(delete(ProductionPlan))
        await db.execute(delete(ScanSetting))
        await db.execute(delete(SupplierOutbound))
        await db.execute(delete(SupplierInbound))
        await db.execute(delete(DivisionDailyNote))

        # 2. Re-seed ProductionPlan default hari ini (WIB)
        today = datetime.now(WIB).date()
        today_str = today.strftime("%Y%m%d")
        plan_id = f"PLAN-{today_str}-01"
        new_plan = ProductionPlan(
            plan_id=plan_id,
            plan_date=today,
            target_gilingan=0,
            target_mika_pax=0,
            target_vakum_pax=0,
            status="DRAFT",
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


# ============================================================================
# 15. SENTRALISASI PPIC & CETAK BARCODE (AYU)
# ============================================================================

def parse_date_param(d_str: Optional[str]) -> date:
    if not d_str:
        return datetime.now(WIB).date()
    try:
        return datetime.strptime(d_str, "%Y-%m-%d").date()
    except ValueError:
        try:
            return datetime.strptime(d_str, "%Y%m%d").date()
        except ValueError:
            return datetime.now(WIB).date()


@router.post("/generate-daily-lots-all", status_code=status.HTTP_201_CREATED)
async def generate_daily_lots_all(
    payload: GenerateDailyLotsAllRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_ppic())
):
    """
    Pusat Penerbitan Barcode Harian PPIC (Ayu):
    Menerbitkan seluruh barcode yang dibutuhkan pabrik hari ini (Adonan, Bumbu, Kulit, Saos)
    secara batch atomik (Batch 1) sebelum shift produksi dimulai.
    Dilengkapi pemeriksaan idempotency dan optimistic locking.
    """
    if current_user and not is_global_supervisor(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akses ditolak: Hanya PPIC (Ayu) yang berhak menerbitkan barcode dan mengelola rencana."
        )

    today_date = datetime.now(WIB).date()
    today_str = today_date.strftime("%Y%m%d")

    # 1. Tentukan plan_id
    plan_id = payload.plan_id
    if not plan_id:
        plan_query = select(ProductionPlan).where(ProductionPlan.plan_date == today_date).limit(1)
        plan_res = await db.execute(plan_query)
        active_plan = plan_res.scalar_one_or_none()
        if active_plan:
            plan_id = active_plan.plan_id
        else:
            plan_id = f"PLAN-{today_str}-01"

    # 2. Pemeriksaan Idempotency Key (Jika key sama dikirim ulang, kembalikan hasil batch sebelumnya)
    if payload.idempotency_key:
        existing_batch_res = await db.execute(
            select(ProductionBatch).where(ProductionBatch.idempotency_key == payload.idempotency_key)
        )
        existing_b = existing_batch_res.scalar_one_or_none()
        if existing_b:
            lots_res = await db.execute(
                select(LotTracking).where(
                    LotTracking.plan_id == existing_b.plan_id,
                    LotTracking.batch_no == existing_b.batch_no
                )
            )
            existing_lots = lots_res.scalars().all()
            counts = {}
            if existing_b.counts_json:
                try:
                    counts = json.loads(existing_b.counts_json)
                except Exception:
                    counts = {}
            return {
                "status": "success",
                "is_idempotent_replay": True,
                "message": f"Permintaan terulang (idempotent). Mengembalikan Batch {existing_b.batch_no} yang telah diterbitkan sebelumnya.",
                "plan_id": existing_b.plan_id,
                "batch_no": existing_b.batch_no,
                "counts": counts,
                "lots": [
                    {
                        "lot_qr_code": l.lot_qr_code,
                        "plan_id": l.plan_id,
                        "batch_no": l.batch_no,
                        "lot_type": l.lot_type,
                        "current_status": l.current_status,
                        "current_station": l.current_station,
                        "quantity": float(l.quantity),
                        "unit": l.unit
                    }
                    for l in existing_lots
                ]
            }

    # 3. Pemeriksaan apakah Batch 1 sudah pernah diterbitkan untuk rencana ini
    batch1_check = await db.execute(
        select(ProductionBatch).where(
            ProductionBatch.plan_id == plan_id,
            ProductionBatch.batch_no == 1
        )
    )
    existing_b1 = batch1_check.scalar_one_or_none()
    if existing_b1 and existing_b1.status == "ACTIVE":
        time_str = existing_b1.created_at.strftime("%H:%M") if existing_b1.created_at else "08:00"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Batch 1 sudah diterbitkan oleh {existing_b1.created_by} pada pukul {time_str} WIB. Rencana telah dikunci. Gunakan 'Tambah Produksi' untuk menerbitkan batch baru."
        )

    # 4. Optimistic Concurrency Control (Versi Rencana)
    plan_res = await db.execute(select(ProductionPlan).where(ProductionPlan.plan_id == plan_id))
    plan = plan_res.scalar_one_or_none()
    if plan and payload.version is not None and plan.version != payload.version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Rencana telah diubah oleh operator lain. Data telah disinkronkan ulang."
        )

    try:
        # Kuantitas lot murni berdasarkan input PPIC
        target_gilingan = max(0, payload.gilingan_count) if (payload.gilingan_count is not None and payload.gilingan_count >= 0) else 0
        sack_count = max(0, payload.sack_count) if (payload.sack_count is not None and payload.sack_count >= 0) else 0
        wrapper_bal = max(0, payload.wrapper_bal) if (payload.wrapper_bal is not None and payload.wrapper_bal >= 0) else 0
        sauce_bal = max(0, payload.sauce_bal) if (payload.sauce_bal is not None and payload.sauce_bal >= 0) else 0

        created_lots = []
        op_id = payload.operator_id or current_user.get("operator_id") or "PPIC-AYU"
        op_role = payload.operator_role or current_user.get("role") or "STAKEHOLDER"

        # 1. Generate Wadah Adonan (AD-YYYYMMDD-XXX)
        if target_gilingan > 0:
            ad_start = await get_next_lot_index(db, "AD")
            for i in range(target_gilingan):
                code = f"AD-{today_str}-{str(ad_start + i).zfill(3)}"
                lot = LotTracking(
                    lot_qr_code=code,
                    plan_id=plan_id,
                    batch_no=1,
                    lot_type="ADONAN",
                    current_status="READY_IN_MEAT_MIXING_STATION",
                    current_station="MEAT_MIXING_STATION",
                    quantity=15.0,
                    unit="KG"
                )
                db.add(lot)
                log = CustodyTransferLog(
                    lot_qr_code=code,
                    action_type="QR_PRINTED",
                    station_id="MEAT_MIXING_STATION",
                    user_id=op_id,
                    user_role_active=op_role,
                    notes=f"Cetak Batch 1 PPIC ({i+1}/{target_gilingan} adonan)"
                )
                db.add(log)
                created_lots.append(lot)

        # 2. Generate Karung Bumbu Sparta (BM-YYYYMMDD-XXX)
        if sack_count > 0:
            bm_start = await get_next_lot_index(db, "BM")
            for i in range(sack_count):
                code = f"BM-{today_str}-{str(bm_start + i).zfill(3)}"
                lot = LotTracking(
                    lot_qr_code=code,
                    plan_id=plan_id,
                    batch_no=1,
                    lot_type="BUMBU",
                    current_status="PRINTED_PENDING_STOCK_IN",
                    current_station="SEASONING_STATION",
                    quantity=1.0,
                    unit="KARUNG"
                )
                db.add(lot)
                log = CustodyTransferLog(
                    lot_qr_code=code,
                    action_type="QR_PRINTED",
                    station_id="SEASONING_STATION",
                    user_id=op_id,
                    user_role_active=op_role,
                    notes=f"Cetak Batch 1 PPIC ({i+1}/{sack_count} karung bumbu)"
                )
                db.add(log)
                created_lots.append(lot)

        # 3. Generate Bal Kulit (KL-YYYYMMDD-XXX)
        wrapper_9cm = max(0, payload.wrapper_9cm_bal) if (payload.wrapper_9cm_bal is not None and payload.wrapper_9cm_bal >= 0) else 0
        wrapper_7cm = max(0, payload.wrapper_7cm_bal) if (payload.wrapper_7cm_bal is not None and payload.wrapper_7cm_bal >= 0) else 0
        has_specific_wrapper = (payload.wrapper_9cm_bal is not None or payload.wrapper_7cm_bal is not None)

        if has_specific_wrapper:
            total_wrapper = wrapper_9cm + wrapper_7cm
            if total_wrapper > 0:
                kl_start = await get_next_lot_index(db, "KL")
                curr_idx = 0
                for _ in range(wrapper_9cm):
                    code = f"KL-{today_str}-{str(kl_start + curr_idx).zfill(3)}"
                    curr_idx += 1
                    lot = LotTracking(
                        lot_qr_code=code,
                        plan_id=plan_id,
                        batch_no=1,
                        lot_type="KULIT_9CM",
                        current_status="PRINTED_PENDING_STOCK_IN",
                        current_station="WRAPPER_STATION",
                        quantity=1.0,
                        unit="BAL"
                    )
                    db.add(lot)
                    log = CustodyTransferLog(
                        lot_qr_code=code,
                        action_type="QR_PRINTED",
                        station_id="WRAPPER_STATION",
                        user_id=op_id,
                        user_role_active=op_role,
                        notes="Cetak Batch 1 PPIC (Kulit 9cm Mika)"
                    )
                    db.add(log)
                    created_lots.append(lot)
                for _ in range(wrapper_7cm):
                    code = f"KL-{today_str}-{str(kl_start + curr_idx).zfill(3)}"
                    curr_idx += 1
                    lot = LotTracking(
                        lot_qr_code=code,
                        plan_id=plan_id,
                        batch_no=1,
                        lot_type="KULIT_7CM",
                        current_status="PRINTED_PENDING_STOCK_IN",
                        current_station="WRAPPER_STATION",
                        quantity=1.0,
                        unit="BAL"
                    )
                    db.add(lot)
                    log = CustodyTransferLog(
                        lot_qr_code=code,
                        action_type="QR_PRINTED",
                        station_id="WRAPPER_STATION",
                        user_id=op_id,
                        user_role_active=op_role,
                        notes="Cetak Batch 1 PPIC (Kulit 7cm Vakum)"
                    )
                    db.add(log)
                    created_lots.append(lot)
        elif wrapper_bal > 0:
            kl_start = await get_next_lot_index(db, "KL")
            for i in range(wrapper_bal):
                code = f"KL-{today_str}-{str(kl_start + i).zfill(3)}"
                w_type = "KULIT_9CM" if (i % 2 == 0) else "KULIT_7CM"
                lot = LotTracking(
                    lot_qr_code=code,
                    plan_id=plan_id,
                    batch_no=1,
                    lot_type=w_type,
                    current_status="PRINTED_PENDING_STOCK_IN",
                    current_station="WRAPPER_STATION",
                    quantity=1.0,
                    unit="BAL"
                )
                db.add(lot)
                log = CustodyTransferLog(
                    lot_qr_code=code,
                    action_type="QR_PRINTED",
                    station_id="WRAPPER_STATION",
                    user_id=op_id,
                    user_role_active=op_role,
                    notes=f"Cetak Batch 1 PPIC ({i+1}/{wrapper_bal} bal kulit {w_type})"
                )
                db.add(log)
                created_lots.append(lot)

        # 4. Generate Bal Saos (SA-YYYYMMDD-XXX)
        sauce_mika = max(0, payload.sauce_mika_bal) if (payload.sauce_mika_bal is not None and payload.sauce_mika_bal >= 0) else 0
        sauce_vakum = max(0, payload.sauce_vakum_bal) if (payload.sauce_vakum_bal is not None and payload.sauce_vakum_bal >= 0) else 0
        has_specific_sauce = (payload.sauce_mika_bal is not None or payload.sauce_vakum_bal is not None)

        if has_specific_sauce:
            total_sauce = sauce_mika + sauce_vakum
            if total_sauce > 0:
                sa_start = await get_next_lot_index(db, "SA")
                curr_idx = 0
                for _ in range(sauce_mika):
                    code = f"SA-{today_str}-{str(sa_start + curr_idx).zfill(3)}"
                    curr_idx += 1
                    lot = LotTracking(
                        lot_qr_code=code,
                        plan_id=plan_id,
                        batch_no=1,
                        lot_type="SAOS_MIKA",
                        current_status="PRINTED_PENDING_STOCK_IN",
                        current_station="SAUCE_STATION",
                        quantity=1.0,
                        unit="BAL"
                    )
                    db.add(lot)
                    log = CustodyTransferLog(
                        lot_qr_code=code,
                        action_type="QR_PRINTED",
                        station_id="SAUCE_STATION",
                        user_id=op_id,
                        user_role_active=op_role,
                        notes="Cetak Batch 1 PPIC (Saos Mika)"
                    )
                    db.add(log)
                    created_lots.append(lot)
                for _ in range(sauce_vakum):
                    code = f"SA-{today_str}-{str(sa_start + curr_idx).zfill(3)}"
                    curr_idx += 1
                    lot = LotTracking(
                        lot_qr_code=code,
                        plan_id=plan_id,
                        batch_no=1,
                        lot_type="SAOS_VAKUM",
                        current_status="PRINTED_PENDING_STOCK_IN",
                        current_station="SAUCE_STATION",
                        quantity=1.0,
                        unit="BAL"
                    )
                    db.add(lot)
                    log = CustodyTransferLog(
                        lot_qr_code=code,
                        action_type="QR_PRINTED",
                        station_id="SAUCE_STATION",
                        user_id=op_id,
                        user_role_active=op_role,
                        notes="Cetak Batch 1 PPIC (Saos Vakum)"
                    )
                    db.add(log)
                    created_lots.append(lot)
        elif sauce_bal > 0:
            sa_start = await get_next_lot_index(db, "SA")
            for i in range(sauce_bal):
                code = f"SA-{today_str}-{str(sa_start + i).zfill(3)}"
                lot = LotTracking(
                    lot_qr_code=code,
                    plan_id=plan_id,
                    batch_no=1,
                    lot_type="SAOS",
                    current_status="PRINTED_PENDING_STOCK_IN",
                    current_station="SAUCE_STATION",
                    quantity=1.0,
                    unit="BAL"
                )
                db.add(lot)
                log = CustodyTransferLog(
                    lot_qr_code=code,
                    action_type="QR_PRINTED",
                    station_id="SAUCE_STATION",
                    user_id=op_id,
                    user_role_active=op_role,
                    notes=f"Cetak Batch 1 PPIC ({i+1}/{sauce_bal} bal saos)"
                )
                db.add(log)
                created_lots.append(lot)

        wrapper_count = (wrapper_9cm + wrapper_7cm) if has_specific_wrapper else wrapper_bal
        sauce_count = (sauce_mika + sauce_vakum) if has_specific_sauce else sauce_bal

        batch1_counts = {
            "adonan": target_gilingan,
            "bumbu": sack_count,
            "kulit": wrapper_count,
            "saos": sauce_count,
            "total": len(created_lots)
        }

        # Simpan Record Batch 1 (Update jika Batch 1 sebelumnya berstatus VOID, atau Insert baru)
        if existing_b1:
            existing_b1.status = "ACTIVE"
            existing_b1.plan_date = today_date
            existing_b1.created_by = op_id
            existing_b1.created_at = datetime.utcnow()
            existing_b1.reason = payload.reason or "Penerbitan Batch 1 Awal Shift"
            existing_b1.idempotency_key = payload.idempotency_key
            existing_b1.counts_json = json.dumps(batch1_counts)
            batch1 = existing_b1
        else:
            batch1 = ProductionBatch(
                plan_id=plan_id,
                batch_no=1,
                plan_date=today_date,
                status="ACTIVE",
                created_by=op_id,
                reason=payload.reason or "Penerbitan Batch 1 Awal Shift",
                idempotency_key=payload.idempotency_key,
                counts_json=json.dumps(batch1_counts)
            )
            db.add(batch1)
        await db.flush()

        for lot in created_lots:
            lot.batch_id = batch1.batch_id

        # Update status rencana menjadi PUBLISHED & kunci rencana
        if not plan:
            plan = ProductionPlan(
                plan_id=plan_id,
                plan_date=today_date,
                target_gilingan=target_gilingan,
                target_mika_pax=0,
                target_vakum_pax=0,
                status="PUBLISHED",
                version=2,
                created_by=op_id
            )
            db.add(plan)
        else:
            plan.status = "PUBLISHED"
            plan.published_at = datetime.utcnow()
            plan.published_by = op_id
            plan.version = (plan.version or 1) + 1
            plan.updated_at = datetime.utcnow()

        await db.commit()
        for lot in created_lots:
            await db.refresh(lot)

        return {
            "status": "success",
            "message": f"Berhasil menerbitkan Batch 1 sebanyak {len(created_lots)} barcode produksi.",
            "plan_id": plan_id,
            "batch_no": 1,
            "counts": batch1_counts,
            "lots": [
                {
                    "lot_qr_code": l.lot_qr_code,
                    "plan_id": l.plan_id,
                    "batch_no": l.batch_no,
                    "lot_type": l.lot_type,
                    "current_status": l.current_status,
                    "current_station": l.current_station,
                    "quantity": float(l.quantity),
                    "unit": l.unit
                }
                for l in created_lots
            ]
        }
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Gagal menerbitkan barcode massal PPIC: {str(e)}"
        )


@router.post("/generate-packaging-lots-batch", response_model=List[LotResponse], status_code=status.HTTP_201_CREATED)
async def generate_packaging_lots_batch(
    payload: GeneratePackagingBatchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_ppic())
):
    """
    Penerbitan QR Label Kemasan (Finished Goods) per Batch di Halaman Rencana (PPIC).
    Mendukung pencetakan bertahap per roll (misal 50 atau 100 label stiker thermal 2-line).
    """
    prod_type = payload.product_type.strip().upper()
    if prod_type not in ["FG_MIKA", "FG_VAKUM"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tipe produk kemasan harus 'FG_MIKA' atau 'FG_VAKUM'."
        )

    if payload.batch_qty <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Jumlah batch cetak harus minimal 1."
        )

    today_date = datetime.now(WIB).date()
    today_str = today_wib()

    plan_id = payload.plan_id
    if not plan_id:
        plan_res = await db.execute(select(ProductionPlan).where(ProductionPlan.plan_date == today_date).limit(1))
        active_plan = plan_res.scalar_one_or_none()
        plan_id = active_plan.plan_id if active_plan else f"PLAN-{today_str}-01"

    prefix = "FG-MIKA" if prod_type == "FG_MIKA" else "FG-VAKUM"
    start_idx = await get_next_lot_index(db, prefix)

    qty_per_pack = 50.0 if prod_type == "FG_MIKA" else 25.0
    unit_label = "MIKA" if prod_type == "FG_MIKA" else "PACK"
    label_desc = "Dimsum Mika (50 pcs)" if prod_type == "FG_MIKA" else "Dimsum Vakum (25 pcs)"

    created_lots = []
    try:
        for i in range(payload.batch_qty):
            code = f"{prefix}-{today_str}-{str(start_idx + i).zfill(3)}"
            lot = LotTracking(
                lot_qr_code=code,
                plan_id=plan_id,
                lot_type=prod_type,
                current_status="READY_IN_ASSEMBLY_STATION",
                current_station="ASSEMBLY_STATION",
                quantity=qty_per_pack,
                unit=unit_label
            )
            db.add(lot)

            log = CustodyTransferLog(
                lot_qr_code=code,
                action_type="FG_PREPRINTED",
                station_id="PLAN",
                target_station="ASSEMBLY_STATION",
                user_id=payload.operator_id or current_user.get("operator_id") or "PPIC-AYU",
                user_role_active=current_user.get("role") or "PPIC",
                notes=f"Cetak batch kemasan {label_desc} ({payload.batch_qty} label stiker 2-line)"
            )
            db.add(log)
            created_lots.append(lot)

        await db.commit()

        for l in created_lots:
            await db.refresh(l)

        return created_lots
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Gagal menerbitkan batch kemasan: {str(e)}"
        )


@router.post("/plans/batches/preview", response_model=AddProductionBatchPreviewResponse)
async def preview_additional_batch(
    payload: AddProductionBatchPreviewRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_ppic())
):
    """
    Kalkulasi Pratinjau Kebutuhan QR Tambahan (Batch 2+):
    Menghitung total kumulatif target dikurangi lot aktif yang sudah terbit hari ini.
    Menjamin karung/bal yang masih cukup tidak dicetak ulang (selisih minimal 0).
    """
    today = datetime.now(WIB).date()
    today_str = today.strftime("%Y%m%d")
    plan_id = payload.plan_id or f"PLAN-{today_str}-01"

    plan_res = await db.execute(select(ProductionPlan).where(ProductionPlan.plan_id == plan_id))
    plan = plan_res.scalar_one_or_none()
    if not plan or plan.status != "PUBLISHED":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Rencana produksi hari ini belum diterbitkan (masih DRAFT). Terbitkan Batch 1 terlebih dahulu."
        )

    add_mika = max(0, payload.add_mika_packs or 0)
    add_vakum = max(0, payload.add_vakum_packs or 0)

    # Ambil seluruh lot aktif (tidak VOID) untuk rencana hari ini
    active_lots_res = await db.execute(
        select(LotTracking).where(
            LotTracking.plan_id == plan.plan_id,
            LotTracking.current_status != "VOID"
        )
    )
    active_lots = active_lots_res.scalars().all()

    already_adonan = sum(1 for l in active_lots if l.lot_type == "ADONAN")
    already_bumbu = sum(1 for l in active_lots if l.lot_type == "BUMBU")
    already_kulit = sum(1 for l in active_lots if (l.lot_type or "").startswith("KULIT"))
    already_saos = sum(1 for l in active_lots if (l.lot_type or "").startswith("SAOS"))

    # Target kumulatif baru (pack)
    cum_mika = (plan.order_mika or 0) + (plan.bumper_mika or 0) + add_mika
    cum_vakum = (plan.order_vakum or 0) + (plan.bumper_vakum or 0) + add_vakum
    total_pcs = (cum_mika * 50) + (cum_vakum * 25)

    if payload.manual_add_gilingan is not None:
        delta_adonan = max(0, payload.manual_add_gilingan)
        cum_gilingan = already_adonan + delta_adonan
    else:
        cum_gilingan = math.ceil((total_pcs * 17) / 19000) if total_pcs > 0 else 0
        delta_adonan = max(0, cum_gilingan - already_adonan)

    needed_bumbu = math.ceil(cum_gilingan / 20) if cum_gilingan > 0 else 0
    needed_kulit = cum_gilingan
    needed_saos = cum_gilingan

    # Selisih (delta minimum 0 agar tidak mencetak ulang bahan yang masih cukup)
    if payload.manual_add_bumbu is not None:
        delta_bumbu = max(0, payload.manual_add_bumbu)
    else:
        delta_bumbu = max(0, needed_bumbu - already_bumbu)

    if payload.manual_add_kulit is not None:
        delta_kulit = max(0, payload.manual_add_kulit)
    else:
        delta_kulit = max(0, needed_kulit - already_kulit)

    if payload.manual_add_saos is not None:
        delta_saos = max(0, payload.manual_add_saos)
    else:
        delta_saos = max(0, needed_saos - already_saos)

    max_b_res = await db.execute(
        select(func.max(ProductionBatch.batch_no)).where(ProductionBatch.plan_id == plan.plan_id)
    )
    max_b = max_b_res.scalar() or 1
    next_batch_no = max_b + 1

    total_delta = delta_adonan + delta_bumbu + delta_kulit + delta_saos

    return {
        "next_batch_no": next_batch_no,
        "delta": {
            "adonan": delta_adonan,
            "bumbu": delta_bumbu,
            "kulit": delta_kulit,
            "saos": delta_saos,
            "total": total_delta
        },
        "cumulative_targets": {
            "total_mika": cum_mika,
            "total_vakum": cum_vakum,
            "target_gilingan": cum_gilingan,
            "target_bumbu": needed_bumbu,
            "target_kulit": needed_kulit,
            "target_saos": needed_saos
        },
        "already_issued_active": {
            "adonan": already_adonan,
            "bumbu": already_bumbu,
            "kulit": already_kulit,
            "saos": already_saos,
            "total": len(active_lots)
        },
        "message": f"Pratinjau Batch {next_batch_no}: +{delta_adonan} wadah adonan, +{delta_bumbu} karung bumbu, +{delta_kulit} bal kulit, +{delta_saos} bal saos."
    }


@router.post("/plans/batches/add", status_code=status.HTTP_201_CREATED)
async def add_production_batch(
    payload: AddProductionBatchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_ppic())
):
    """
    Menerbitkan Batch Produksi Tambahan (Batch 2, 3, dst.):
    Menerbitkan lot selisih kebutuhan QR secara atomik dan terikat pada nomor batch baru.
    Dilengkapi perlindungan idempotency key dan penyesuaian target kumulatif.
    """
    today_date = datetime.now(WIB).date()
    today_str = today_date.strftime("%Y%m%d")
    plan_id = payload.plan_id or f"PLAN-{today_str}-01"

    plan_res = await db.execute(select(ProductionPlan).where(ProductionPlan.plan_id == plan_id))
    plan = plan_res.scalar_one_or_none()
    if not plan or plan.status != "PUBLISHED":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Rencana hari ini belum diterbitkan. Terbitkan Batch 1 terlebih dahulu."
        )

    # 1. Pemeriksaan Idempotency Key
    if payload.idempotency_key:
        existing_b_res = await db.execute(
            select(ProductionBatch).where(ProductionBatch.idempotency_key == payload.idempotency_key)
        )
        existing_b = existing_b_res.scalar_one_or_none()
        if existing_b:
            lots_res = await db.execute(
                select(LotTracking).where(
                    LotTracking.plan_id == existing_b.plan_id,
                    LotTracking.batch_no == existing_b.batch_no
                )
            )
            existing_lots = lots_res.scalars().all()
            counts = {}
            if existing_b.counts_json:
                try:
                    counts = json.loads(existing_b.counts_json)
                except Exception:
                    counts = {}
            return {
                "status": "success",
                "is_idempotent_replay": True,
                "message": f"Permintaan terulang (idempotent). Mengembalikan Batch {existing_b.batch_no} yang telah diterbitkan.",
                "plan_id": existing_b.plan_id,
                "batch_no": existing_b.batch_no,
                "counts": counts,
                "lots": [
                    {
                        "lot_qr_code": l.lot_qr_code,
                        "plan_id": l.plan_id,
                        "batch_no": l.batch_no,
                        "lot_type": l.lot_type,
                        "current_status": l.current_status,
                        "current_station": l.current_station,
                        "quantity": float(l.quantity),
                        "unit": l.unit
                    }
                    for l in existing_lots
                ]
            }

    # 2. Hitung delta yang harus diterbitkan
    if payload.confirmed_delta_gilingan is not None:
        delta_adonan = max(0, payload.confirmed_delta_gilingan)
        delta_bumbu = max(0, payload.confirmed_delta_bumbu or 0)
        delta_kulit = max(0, payload.confirmed_delta_kulit or 0)
        delta_saos = max(0, payload.confirmed_delta_saos or 0)
    else:
        prev_req = AddProductionBatchPreviewRequest(
            plan_id=plan_id,
            add_mika_packs=payload.add_mika_packs,
            add_vakum_packs=payload.add_vakum_packs
        )
        prev = await preview_additional_batch(prev_req, db, current_user)
        delta_adonan = prev["delta"]["adonan"]
        delta_bumbu = prev["delta"]["bumbu"]
        delta_kulit = prev["delta"]["kulit"]
        delta_saos = prev["delta"]["saos"]

    total_delta = delta_adonan + delta_bumbu + delta_kulit + delta_saos
    if total_delta == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Kebutuhan bahan sudah tercukupi dari batch sebelumnya (0 QR baru yang perlu diterbitkan)."
        )

    max_b_res = await db.execute(
        select(func.max(ProductionBatch.batch_no)).where(ProductionBatch.plan_id == plan.plan_id)
    )
    max_b = max_b_res.scalar() or 1
    next_batch_no = max_b + 1

    op_id = payload.operator_id or current_user.get("operator_id") or "PPIC-AYU"
    op_role = payload.operator_role or current_user.get("role") or "STAKEHOLDER"

    created_lots = []

    try:
        # Generate Wadah Adonan
        if delta_adonan > 0:
            ad_start = await get_next_lot_index(db, "AD")
            for i in range(delta_adonan):
                code = f"AD-{today_str}-{str(ad_start + i).zfill(3)}"
                lot = LotTracking(
                    lot_qr_code=code,
                    plan_id=plan_id,
                    batch_no=next_batch_no,
                    lot_type="ADONAN",
                    current_status="READY_IN_MEAT_MIXING_STATION",
                    current_station="MEAT_MIXING_STATION",
                    quantity=15.0,
                    unit="KG"
                )
                db.add(lot)
                log = CustodyTransferLog(
                    lot_qr_code=code,
                    action_type="QR_PRINTED",
                    station_id="MEAT_MIXING_STATION",
                    user_id=op_id,
                    user_role_active=op_role,
                    notes=f"Cetak Batch {next_batch_no} PPIC ({i+1}/{delta_adonan} adonan)"
                )
                db.add(log)
                created_lots.append(lot)

        # Generate Karung Bumbu
        if delta_bumbu > 0:
            bm_start = await get_next_lot_index(db, "BM")
            for i in range(delta_bumbu):
                code = f"BM-{today_str}-{str(bm_start + i).zfill(3)}"
                lot = LotTracking(
                    lot_qr_code=code,
                    plan_id=plan_id,
                    batch_no=next_batch_no,
                    lot_type="BUMBU",
                    current_status="PRINTED_PENDING_STOCK_IN",
                    current_station="SEASONING_STATION",
                    quantity=1.0,
                    unit="KARUNG"
                )
                db.add(lot)
                log = CustodyTransferLog(
                    lot_qr_code=code,
                    action_type="QR_PRINTED",
                    station_id="SEASONING_STATION",
                    user_id=op_id,
                    user_role_active=op_role,
                    notes=f"Cetak Batch {next_batch_no} PPIC ({i+1}/{delta_bumbu} karung bumbu)"
                )
                db.add(log)
                created_lots.append(lot)

        # Generate Bal Kulit
        if delta_kulit > 0:
            kl_start = await get_next_lot_index(db, "KL")
            for i in range(delta_kulit):
                code = f"KL-{today_str}-{str(kl_start + i).zfill(3)}"
                w_type = "KULIT_9CM" if (i % 2 == 0) else "KULIT_7CM"
                lot = LotTracking(
                    lot_qr_code=code,
                    plan_id=plan_id,
                    batch_no=next_batch_no,
                    lot_type=w_type,
                    current_status="PRINTED_PENDING_STOCK_IN",
                    current_station="WRAPPER_STATION",
                    quantity=1.0,
                    unit="BAL"
                )
                db.add(lot)
                log = CustodyTransferLog(
                    lot_qr_code=code,
                    action_type="QR_PRINTED",
                    station_id="WRAPPER_STATION",
                    user_id=op_id,
                    user_role_active=op_role,
                    notes=f"Cetak Batch {next_batch_no} PPIC ({i+1}/{delta_kulit} bal kulit {w_type})"
                )
                db.add(log)
                created_lots.append(lot)

        # Generate Bal Saos
        if delta_saos > 0:
            sa_start = await get_next_lot_index(db, "SA")
            for i in range(delta_saos):
                code = f"SA-{today_str}-{str(sa_start + i).zfill(3)}"
                s_type = "SAOS_MIKA" if (i % 2 == 0) else "SAOS_VAKUM"
                lot = LotTracking(
                    lot_qr_code=code,
                    plan_id=plan_id,
                    batch_no=next_batch_no,
                    lot_type=s_type,
                    current_status="PRINTED_PENDING_STOCK_IN",
                    current_station="SAUCE_STATION",
                    quantity=1.0,
                    unit="BAL"
                )
                db.add(lot)
                log = CustodyTransferLog(
                    lot_qr_code=code,
                    action_type="QR_PRINTED",
                    station_id="SAUCE_STATION",
                    user_id=op_id,
                    user_role_active=op_role,
                    notes=f"Cetak Batch {next_batch_no} PPIC ({i+1}/{delta_saos} bal saos)"
                )
                db.add(log)
                created_lots.append(lot)

        batch_counts = {
            "adonan": delta_adonan,
            "bumbu": delta_bumbu,
            "kulit": delta_kulit,
            "saos": delta_saos,
            "total": len(created_lots)
        }

        batch_record = ProductionBatch(
            plan_id=plan.plan_id,
            batch_no=next_batch_no,
            plan_date=today_date,
            status="ACTIVE",
            created_by=op_id,
            reason=payload.reason or "Penambahan Produksi",
            idempotency_key=payload.idempotency_key,
            counts_json=json.dumps(batch_counts)
        )
        db.add(batch_record)
        await db.flush()

        for lot in created_lots:
            lot.batch_id = batch_record.batch_id

        # Update target kumulatif pada plan
        plan.order_mika = (plan.order_mika or 0) + (payload.add_mika_packs or 0)
        plan.order_vakum = (plan.order_vakum or 0) + (payload.add_vakum_packs or 0)
        plan.target_mika_pax = (plan.target_mika_pax or 0) + (payload.add_mika_packs or 0)
        plan.target_vakum_pax = (plan.target_vakum_pax or 0) + (payload.add_vakum_packs or 0)
        plan.target_gilingan = (plan.target_gilingan or 0) + delta_adonan
        plan.version = (plan.version or 1) + 1
        plan.updated_at = datetime.utcnow()

        await db.commit()
        for lot in created_lots:
            await db.refresh(lot)

        return {
            "status": "success",
            "message": f"Berhasil menerbitkan Batch {next_batch_no} sebanyak {len(created_lots)} QR.",
            "plan_id": plan_id,
            "batch_no": next_batch_no,
            "counts": batch_counts,
            "lots": [
                {
                    "lot_qr_code": l.lot_qr_code,
                    "plan_id": l.plan_id,
                    "batch_no": l.batch_no,
                    "lot_type": l.lot_type,
                    "current_status": l.current_status,
                    "current_station": l.current_station,
                    "quantity": float(l.quantity),
                    "unit": l.unit
                }
                for l in created_lots
            ]
        }
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Gagal menerbitkan batch tambahan: {str(e)}"
        )


@router.post("/plans/batches/{batch_no}/void")
async def void_production_batch(
    batch_no: int,
    plan_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_ppic())
):
    """
    Batalkan Satu Batch Penerbitan:
    Hanya dapat dibatalkan jika TIDAK ADA lot dalam batch tersebut yang sudah di-scan di stasiun.
    Jika sudah ada yang di-scan, menolak dengan 409 Conflict dan menyarankan batch koreksi.
    Lot yang dibatalkan diberi status VOID.
    """
    today_date = datetime.now(WIB).date()
    today_str = today_date.strftime("%Y%m%d")
    target_plan_id = plan_id or f"PLAN-{today_str}-01"

    b_res = await db.execute(
        select(ProductionBatch).where(
            ProductionBatch.plan_id == target_plan_id,
            ProductionBatch.batch_no == batch_no
        )
    )
    batch = b_res.scalar_one_or_none()
    if not batch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Batch {batch_no} tidak ditemukan untuk rencana {target_plan_id}."
        )

    if batch.status == "VOID":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Batch {batch_no} sudah dibatalkan (VOID) sebelumnya."
        )

    # Cek lot di batch ini
    lots_res = await db.execute(
        select(LotTracking).where(
            LotTracking.plan_id == target_plan_id,
            LotTracking.batch_no == batch_no
        )
    )
    batch_lots = lots_res.scalars().all()
    codes = [l.lot_qr_code for l in batch_lots]

    if codes:
        # Cek apakah sudah ada log scan selain QR_PRINTED
        scanned_logs = await db.execute(
            select(CustodyTransferLog).where(
                CustodyTransferLog.lot_qr_code.in_(codes),
                CustodyTransferLog.action_type.notin_(["QR_PRINTED", "LOT_VOIDED"])
            )
        )
        first_scanned = scanned_logs.scalars().first()
        if first_scanned:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Batch {batch_no} tidak dapat dibatalkan karena ada lot yang sudah di-scan di stasiun ({first_scanned.station_id} - {first_scanned.action_type}). Silakan buat batch koreksi."
            )

        for l in batch_lots:
            if l.current_status not in ["READY_IN_MEAT_MIXING_STATION", "PRINTED_PENDING_STOCK_IN", "VOID"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Batch {batch_no} tidak dapat dibatalkan karena lot {l.lot_qr_code} sudah berada dalam status '{l.current_status}'."
                )

    op_id = current_user.get("operator_id") or "PPIC-AYU"
    op_role = current_user.get("role") or "STAKEHOLDER"

    # Batalkan batch dan seluruh lot-nya
    batch.status = "VOID"
    for l in batch_lots:
        l.current_status = "VOID"
        db.add(
            CustodyTransferLog(
                lot_qr_code=l.lot_qr_code,
                action_type="LOT_VOIDED",
                station_id=l.current_station,
                user_id=op_id,
                user_role_active=op_role,
                notes=f"Pembatalan Batch {batch_no} oleh PPIC"
            )
        )

    plan_res = await db.execute(select(ProductionPlan).where(ProductionPlan.plan_id == target_plan_id))
    plan = plan_res.scalar_one_or_none()
    if plan:
        plan.version = (plan.version or 1) + 1
        plan.updated_at = datetime.utcnow()
        # Jika tidak ada batch aktif yang tersisa, kembalikan status rencana menjadi DRAFT
        active_b_res = await db.execute(
            select(ProductionBatch).where(
                ProductionBatch.plan_id == target_plan_id,
                ProductionBatch.status == "ACTIVE"
            )
        )
        if not active_b_res.scalars().all():
            plan.status = "DRAFT"

    await db.commit()

    return {
        "status": "success",
        "message": f"Batch {batch_no} berhasil dibatalkan. {len(batch_lots)} barcode berstatus VOID.",
        "batch_no": batch_no,
        "voided_lots_count": len(batch_lots)
    }


# ============================================================================
# 16. PENERIMAAN BARANG SUPPLIER LUAR (INBOUND LOGISTIK PPIC)
# ============================================================================

@router.post("/supplier-inbound", response_model=SupplierInboundResponse, status_code=status.HTTP_201_CREATED)
async def create_supplier_inbound(
    payload: SupplierInboundCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_ppic())
):
    """
    Mencatat penerimaan barang supplier luar oleh PPIC (Kulit, Daging, Bumbu kemasan, Saos, Dus, Mika, Vakum, dll).
    Menyediakan media pencatatan dinamis tanpa otomatisasi pemotongan stok berbelit-belit.
    """
    if current_user and not is_global_supervisor(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akses ditolak: Hanya PPIC (Ayu) yang berhak mencatat penerimaan barang supplier luar."
        )

    try:
        inbound_date = payload.inbound_date or datetime.now(WIB).date()
        inbound = SupplierInbound(
            inbound_date=inbound_date,
            category=payload.category.upper(),
            item_name=payload.item_name,
            quantity=payload.quantity,
            unit=payload.unit.upper(),
            supplier_name=payload.supplier_name,
            document_no=payload.document_no,
            notes=payload.notes,
            received_by=payload.received_by or current_user.get("operator_id") or "PPIC-AYU",
            has_qr=True if payload.has_qr is None else bool(payload.has_qr)
        )
        db.add(inbound)
        await db.commit()
        await db.refresh(inbound)
        return inbound
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Gagal mencatat penerimaan barang supplier luar: {str(e)}"
        )


@router.get("/supplier-inbound", response_model=List[SupplierInboundResponse])
async def list_supplier_inbound(
    inbound_date: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    category: Optional[str] = None,
    all_time: Optional[bool] = False,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Daftar penerimaan barang supplier luar untuk kontrol harian logistik PPIC.
    Mendukung filter tanggal tunggal (inbound_date), rentang tanggal (start_date & end_date), atau all_time=True.
    """
    if all_time:
        query = select(SupplierInbound)
    elif start_date and end_date:
        d_start = parse_date_param(start_date)
        d_end = parse_date_param(end_date)
        if d_start > d_end:
            d_start, d_end = d_end, d_start
        query = select(SupplierInbound).where(SupplierInbound.inbound_date.between(d_start, d_end))
    elif inbound_date:
        target_date = parse_date_param(inbound_date)
        query = select(SupplierInbound).where(SupplierInbound.inbound_date == target_date)
    elif start_date:
        target_date = parse_date_param(start_date)
        query = select(SupplierInbound).where(SupplierInbound.inbound_date == target_date)
    elif end_date:
        target_date = parse_date_param(end_date)
        query = select(SupplierInbound).where(SupplierInbound.inbound_date == target_date)
    else:
        # Default jika tanpa parameter: muat hari ini dan kemarin agar pergantian hari (00:00) tidak mengosongkan riwayat
        today_wib = datetime.now(WIB).date()
        yesterday_wib = today_wib - timedelta(days=1)
        query = select(SupplierInbound).where(SupplierInbound.inbound_date.between(yesterday_wib, today_wib))

    if category:
        query = query.where(SupplierInbound.category == category.upper())
    query = query.order_by(desc(SupplierInbound.inbound_date), desc(SupplierInbound.created_at))
    result = await db.execute(query)
    return result.scalars().all()


# ============================================================================
# 16b. PENGELUARAN BARANG LOGISTIK PPIC (BARANG KELUAR / OUTBOUND KE DIVISI)
# ============================================================================

@router.post("/supplier-outbound", response_model=SupplierOutboundResponse, status_code=status.HTTP_201_CREATED)
async def create_supplier_outbound(
    payload: SupplierOutboundCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_ppic())
):
    """
    Mencatat pengeluaran stok barang logistik oleh PPIC dan otomatis menerbitkan
    QR tracking lot berstatus IN_TRANSIT jika dikirim ke divisi tertentu (misal: Cetak, Giling, dll).
    """
    if current_user and not is_global_supervisor(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akses ditolak: Hanya PPIC (Ayu) yang berhak mencatat pengeluaran barang logistik."
        )

    try:
        outbound_date = payload.outbound_date or datetime.now(WIB).date()
        today_code = outbound_date.strftime("%Y%m%d")
        cat = payload.category.upper()
        cat_prefix = "KL" if cat == "KULIT" else "DG" if cat == "DAGING" else "BM" if cat == "BUMBU" else "SA" if cat == "SAOS" else "KM" if cat == "KEMASAN" else "SP"

        # Validasi sisa stok barang masuk ketat (mencegah bug pengeluaran saat stok habis)
        if payload.inbound_id:
            stmt_in = select(SupplierInbound).where(SupplierInbound.inbound_id == payload.inbound_id)
            res_in = await db.execute(stmt_in)
            inbound_rec = res_in.scalar_one_or_none()
            if not inbound_rec:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Data barang masuk dengan ID {payload.inbound_id} tidak ditemukan."
                )

            stmt_out_sum = select(func.coalesce(func.sum(SupplierOutbound.quantity), 0)).where(
                SupplierOutbound.inbound_id == payload.inbound_id
            )
            res_out_sum = await db.execute(stmt_out_sum)
            already_out = float(res_out_sum.scalar() or 0)
            total_in = float(inbound_rec.quantity or 0)
            remaining_stock = total_in - already_out

            if remaining_stock <= 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Stok untuk '{inbound_rec.item_name}' sudah habis (0 {inbound_rec.unit}). Tidak dapat mengeluarkan stok lagi."
                )

            if float(payload.quantity) > remaining_stock:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Jumlah pengeluaran ({payload.quantity} {inbound_rec.unit}) melebihi sisa stok yang tersedia ({remaining_stock} {inbound_rec.unit})."
                )

        # Internal reference code untuk audit trail
        if payload.lot_qr_code and payload.lot_qr_code.strip():
            lot_code = payload.lot_qr_code.strip().upper()
        else:
            stmt_cnt = select(func.count(SupplierOutbound.outbound_id)).where(SupplierOutbound.outbound_date == outbound_date)
            res_cnt = await db.execute(stmt_cnt)
            seq = (res_cnt.scalar() or 0) + 1
            lot_code = f"OUT-{cat_prefix}-{today_code}-{str(seq).zfill(3)}"

        # Simpan record outbound
        outbound = SupplierOutbound(
            inbound_id=payload.inbound_id,
            outbound_date=outbound_date,
            lot_qr_code=lot_code,
            category=cat,
            item_name=payload.item_name,
            quantity=payload.quantity,
            unit=payload.unit.upper(),
            target_station=payload.target_station,
            target_station_name=payload.target_station_name,
            dispatched_by=payload.dispatched_by or current_user.get("operator_id") or "PPIC-AYU",
            notes=payload.notes,
            created_at=datetime.utcnow()
        )
        db.add(outbound)
        await db.commit()
        await db.refresh(outbound)
        return outbound

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Gagal mencatat pengeluaran barang logistik: {str(e)}"
        )


@router.get("/supplier-outbound", response_model=List[SupplierOutboundResponse])
async def list_supplier_outbound(
    outbound_date: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    category: Optional[str] = None,
    all_time: Optional[bool] = False,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Daftar pengeluaran barang logistik PPIC ke divisi produksi maupun keluar umum.
    """
    if all_time:
        query = select(SupplierOutbound)
    elif start_date and end_date:
        d_start = parse_date_param(start_date)
        d_end = parse_date_param(end_date)
        if d_start > d_end:
            d_start, d_end = d_end, d_start
        query = select(SupplierOutbound).where(SupplierOutbound.outbound_date.between(d_start, d_end))
    elif outbound_date:
        target_date = parse_date_param(outbound_date)
        query = select(SupplierOutbound).where(SupplierOutbound.outbound_date == target_date)
    elif start_date:
        target_date = parse_date_param(start_date)
        query = select(SupplierOutbound).where(SupplierOutbound.outbound_date == target_date)
    elif end_date:
        target_date = parse_date_param(end_date)
        query = select(SupplierOutbound).where(SupplierOutbound.outbound_date == target_date)
    else:
        # Default jika tanpa parameter: muat hari ini dan kemarin
        today_wib = datetime.now(WIB).date()
        yesterday_wib = today_wib - timedelta(days=1)
        query = select(SupplierOutbound).where(SupplierOutbound.outbound_date.between(yesterday_wib, today_wib))

    if category:
        query = query.where(SupplierOutbound.category == category.upper())
    query = query.order_by(desc(SupplierOutbound.outbound_date), desc(SupplierOutbound.created_at))
    result = await db.execute(query)
    return result.scalars().all()


# ============================================================================
# 17. CATATAN REKAP & REJECT DIVISI HARIAN (PPIC KONTROL)
# ============================================================================

@router.post("/daily-notes", response_model=DivisionDailyNoteResponse)
async def save_division_daily_note(
    payload: DivisionDailyNoteCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_ppic())
):
    """
    Menyimpan atau memperbarui rekap reject dan catatan operasional divisi untuk hari ini.
    """
    if current_user and not is_global_supervisor(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akses ditolak: Hanya PPIC (Ayu) yang berhak mencatat rekap reject dan kendala divisi."
        )

    try:
        today = datetime.now(WIB).date()
        query = select(DivisionDailyNote).where(
            DivisionDailyNote.record_date == today,
            DivisionDailyNote.division == payload.division.upper()
        )
        result = await db.execute(query)
        existing = result.scalar_one_or_none()

        if existing:
            existing.reject_count = payload.reject_count
            existing.reject_notes = payload.reject_notes
            existing.operational_notes = payload.operational_notes
            existing.created_by = payload.created_by or current_user.get("operator_id") or "PPIC-AYU"
            existing.updated_at = datetime.utcnow()
            await db.commit()
            await db.refresh(existing)
            return existing
        else:
            note = DivisionDailyNote(
                record_date=today,
                division=payload.division.upper(),
                reject_count=payload.reject_count,
                reject_notes=payload.reject_notes,
                operational_notes=payload.operational_notes,
                created_by=payload.created_by or current_user.get("operator_id") or "PPIC-AYU"
            )
            db.add(note)
            await db.commit()
            await db.refresh(note)
            return note
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Gagal menyimpan catatan harian divisi: {str(e)}"
        )


@router.get("/daily-notes", response_model=List[DivisionDailyNoteResponse])
async def list_division_daily_notes(
    record_date: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    target_date = parse_date_param(record_date)
    query = select(DivisionDailyNote).where(DivisionDailyNote.record_date == target_date)
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/daily-recap")
async def get_daily_recap(
    recap_date: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Data Rekapitulasi Menyeluruh Harian & Rentang Tanggal untuk Ayu (PPIC):
    Mengagregasi produksi per divisi, pergerakan lot, reject, dan catatan operasional.
    """
    # Deteksi mode rentang tanggal jika start_date dan end_date keduanya diberikan
    if start_date and end_date:
        d_start = parse_date_param(start_date)
        d_end = parse_date_param(end_date)
        if d_start > d_end:
            d_start, d_end = d_end, d_start

        # 1. Catatan divisi dalam rentang
        notes_query = select(DivisionDailyNote).where(
            DivisionDailyNote.record_date.between(d_start, d_end)
        ).order_by(DivisionDailyNote.record_date)
        notes_res = await db.execute(notes_query)
        all_notes = notes_res.scalars().all()

        notes_by_div: Dict[str, List[DivisionDailyNote]] = {
            "DAGING": [],
            "BUMBU": [],
            "KULIT": [],
            "SAOS": [],
            "CETAK": [],
            "PACKING": []
        }
        for n in all_notes:
            div_name = (n.division or "").upper()
            if div_name in notes_by_div:
                notes_by_div[div_name].append(n)
            else:
                notes_by_div[div_name] = [n]

        # 2. Barang masuk supplier dalam rentang
        inbound_query = select(SupplierInbound).where(
            SupplierInbound.inbound_date.between(d_start, d_end)
        ).order_by(desc(SupplierInbound.inbound_date), desc(SupplierInbound.created_at))
        inbound_res = await db.execute(inbound_query)
        inbound_list = inbound_res.scalars().all()

        # 3. Rencana produksi dalam rentang
        plan_query = select(ProductionPlan).where(
            ProductionPlan.plan_date.between(d_start, d_end)
        ).order_by(ProductionPlan.plan_date)
        plan_res = await db.execute(plan_query)
        plans = plan_res.scalars().all()
        target_gilingan_sum = sum(p.target_gilingan or 0 for p in plans)

        # 4. Lot tracking dalam rentang
        start_dt = datetime.combine(d_start, datetime.min.time())
        end_dt = datetime.combine(d_end, datetime.max.time())
        d_start_iso = d_start.isoformat()
        d_end_iso = d_end.isoformat()

        num_days = (d_end - d_start).days
        if 0 <= num_days <= 60:
            date_tokens = [(d_start + timedelta(days=i)).strftime("%Y%m%d") for i in range(num_days + 1)]
            lot_code_conds = [LotTracking.lot_qr_code.like(f"%-{tok}-%") for tok in date_tokens]
            lots_query = select(LotTracking).where(
                or_(
                    LotTracking.created_at.between(start_dt, end_dt),
                    func.date(LotTracking.created_at).between(d_start_iso, d_end_iso),
                    *lot_code_conds
                )
            )
        else:
            lots_query = select(LotTracking).where(
                or_(
                    LotTracking.created_at.between(start_dt, end_dt),
                    func.date(LotTracking.created_at).between(d_start_iso, d_end_iso)
                )
            )
        lots_res = await db.execute(lots_query)
        all_range_lots = lots_res.scalars().all()

        division_lots: Dict[str, List[LotTracking]] = {
            "DAGING": [],
            "BUMBU": [],
            "KULIT": [],
            "SAOS": [],
            "PROD_2": []
        }
        for lot in all_range_lots:
            l_type = (lot.lot_type or "").upper()
            if l_type in ["ADONAN", "DAGING"]:
                division_lots["DAGING"].append(lot)
            elif l_type == "BUMBU":
                division_lots["BUMBU"].append(lot)
            elif "KULIT" in l_type:
                division_lots["KULIT"].append(lot)
            elif l_type == "SAOS":
                division_lots["SAOS"].append(lot)
            else:
                division_lots["PROD_2"].append(lot)

        def summarize_range_div(div_key: str, default_unit: str) -> Dict[str, Any]:
            lots = division_lots.get(div_key, [])
            div_notes = notes_by_div.get(div_key, [])
            total_qty = sum(float(l.quantity or 0) for l in lots)
            
            printed_count = sum(1 for l in lots if "PRINTED" in (l.current_status or ""))
            transit_count = sum(1 for l in lots if "TRANSIT" in (l.current_status or ""))
            ready_or_delivered = sum(1 for l in lots if "READY" in (l.current_status or "") or "STOCK" in (l.current_status or "") or "IN_" in (l.current_status or ""))
            consumed_count = sum(1 for l in lots if l.current_status == "CONSUMED")
            void_count = sum(1 for l in lots if (l.current_status or "").upper() == "VOID")

            pending_lots = [
                {
                    "lot_qr_code": l.lot_qr_code,
                    "quantity": float(l.quantity or 0),
                    "unit": l.unit or default_unit,
                    "current_status": l.current_status
                }
                for l in lots if "PRINTED" in (l.current_status or "")
            ]

            completed_qty = sum(
                float(l.quantity or 0)
                for l in lots
                if "READY" in (l.current_status or "") or "STOCK" in (l.current_status or "") or "IN_" in (l.current_status or "") or l.current_status == "CONSUMED"
            )

            rej_sum = sum(n.reject_count or 0 for n in div_notes)
            rej_notes_items = [n for n in div_notes if n.reject_notes and n.reject_notes.strip()]
            if len(rej_notes_items) == 1:
                agg_rej_notes = rej_notes_items[0].reject_notes.strip()
            elif len(rej_notes_items) > 1:
                agg_rej_notes = "; ".join(f"[{n.record_date.isoformat()}] {n.reject_notes.strip()}" for n in rej_notes_items)
            else:
                agg_rej_notes = ""

            op_notes_items = [n for n in div_notes if n.operational_notes and n.operational_notes.strip()]
            if len(op_notes_items) == 1:
                agg_op_notes = op_notes_items[0].operational_notes.strip()
            elif len(op_notes_items) > 1:
                agg_op_notes = "; ".join(f"[{n.record_date.isoformat()}] {n.operational_notes.strip()}" for n in op_notes_items)
            else:
                agg_op_notes = ""

            last_updated_by = div_notes[-1].created_by if div_notes else "PPIC-AYU"

            return {
                "division": div_key,
                "total_lots": len(lots),
                "total_quantity": total_qty,
                "completed_quantity": completed_qty,
                "unit": default_unit,
                "printed_count": printed_count,
                "transit_count": transit_count,
                "completed_count": ready_or_delivered + consumed_count,
                "void_count": void_count,
                "pending_lots": pending_lots,
                "reject_count": rej_sum,
                "reject_notes": agg_rej_notes,
                "operational_notes": agg_op_notes,
                "updated_by": last_updated_by
            }

        def calculate_range_cetak() -> Dict[str, Any]:
            div_notes = notes_by_div.get("CETAK", [])
            rej_sum = sum(n.reject_count or 0 for n in div_notes)
            rej_notes_items = [n for n in div_notes if n.reject_notes and n.reject_notes.strip()]
            agg_rej_notes = "; ".join(f"[{n.record_date.isoformat()}] {n.reject_notes.strip()}" for n in rej_notes_items) if len(rej_notes_items) > 1 else (rej_notes_items[0].reject_notes.strip() if rej_notes_items else "")
            op_notes_items = [n for n in div_notes if n.operational_notes and n.operational_notes.strip()]
            agg_op_notes = "; ".join(f"[{n.record_date.isoformat()}] {n.operational_notes.strip()}" for n in op_notes_items) if len(op_notes_items) > 1 else (op_notes_items[0].operational_notes.strip() if op_notes_items else "")
            last_updated_by = div_notes[-1].created_by if div_notes else "PPIC-AYU"

            adonan_received_lots = [
                l for l in all_range_lots
                if (l.lot_type in ["ADONAN", "DAGING"] or l.lot_qr_code.startswith("AD-") or l.lot_qr_code.startswith("DG-"))
                and (l.current_status in ["IN_ASSEMBLY_STATION", "READY_IN_ASSEMBLY_STATION", "CONSUMED"]
                     or (l.current_station or "").upper() in ["ASSEMBLY_STATION", "CETAK"])
            ]
            adonan_received_count = len(adonan_received_lots)
            adonan_received_qty = sum(float(l.quantity or 0) for l in adonan_received_lots)
            adonan_transit_count = sum(
                1 for l in all_range_lots
                if (l.lot_type in ["ADONAN", "DAGING"] or l.lot_qr_code.startswith("AD-"))
                and "TRANSIT" in (l.current_status or "")
            )

            kulit_received_lots = [
                l for l in all_range_lots
                if ("KULIT" in (l.lot_type or "").upper() or l.lot_qr_code.startswith("KL-"))
                and (l.current_status in ["IN_ASSEMBLY_STATION", "READY_IN_ASSEMBLY_STATION", "CONSUMED"]
                     or (l.current_station or "").upper() in ["ASSEMBLY_STATION", "CETAK"])
            ]
            kulit_received_count = len(kulit_received_lots)
            kulit_received_qty = sum(float(l.quantity or 0) for l in kulit_received_lots)
            kulit_transit_count = sum(
                1 for l in all_range_lots
                if ("KULIT" in (l.lot_type or "").upper() or l.lot_qr_code.startswith("KL-"))
                and "TRANSIT" in (l.current_status or "")
            )

            fg_lots = [
                l for l in all_range_lots
                if (l.lot_type in ["FG_MIKA", "FG_VAKUM"] or l.lot_qr_code.startswith("FG-"))
                and (l.current_status or "").upper() != "VOID"
            ]
            fg_mika_cnt = sum(1 for l in fg_lots if "MIKA" in (l.lot_type or l.lot_qr_code or ""))
            fg_vakum_cnt = sum(1 for l in fg_lots if "VAKUM" in (l.lot_type or l.lot_qr_code or ""))
            total_pcs = (fg_mika_cnt * 50) + (fg_vakum_cnt * 25)

            return {
                "division": "CETAK",
                "name": "Meja Cetak",
                "unit": "PCS",
                "adonan_received_count": adonan_received_count,
                "adonan_received_qty": adonan_received_qty,
                "adonan_transit_count": adonan_transit_count,
                "kulit_received_count": kulit_received_count,
                "kulit_received_qty": kulit_received_qty,
                "kulit_transit_count": kulit_transit_count,
                "total_pcs_cetak": total_pcs,
                "total_quantity": total_pcs,
                "total_lots": adonan_received_count + kulit_received_count,
                "completed_quantity": total_pcs,
                "printed_count": 0,
                "transit_count": adonan_transit_count + kulit_transit_count,
                "completed_count": adonan_received_count + kulit_received_count,
                "void_count": 0,
                "pending_lots": [],
                "reject_count": rej_sum,
                "reject_notes": agg_rej_notes,
                "operational_notes": agg_op_notes,
                "updated_by": last_updated_by
            }

        def calculate_range_packing() -> Dict[str, Any]:
            div_notes = notes_by_div.get("PACKING", [])
            rej_sum = sum(n.reject_count or 0 for n in div_notes)
            rej_notes_items = [n for n in div_notes if n.reject_notes and n.reject_notes.strip()]
            agg_rej_notes = "; ".join(f"[{n.record_date.isoformat()}] {n.reject_notes.strip()}" for n in rej_notes_items) if len(rej_notes_items) > 1 else (rej_notes_items[0].reject_notes.strip() if rej_notes_items else "")
            op_notes_items = [n for n in div_notes if n.operational_notes and n.operational_notes.strip()]
            agg_op_notes = "; ".join(f"[{n.record_date.isoformat()}] {n.operational_notes.strip()}" for n in op_notes_items) if len(op_notes_items) > 1 else (op_notes_items[0].operational_notes.strip() if op_notes_items else "")
            last_updated_by = div_notes[-1].created_by if div_notes else "PPIC-AYU"

            target_mika_val = sum(int(p.target_mika_pax or ((p.order_mika or 0) + (p.bumper_mika or 0))) for p in plans)
            target_vakum_val = sum(int(p.target_vakum_pax or ((p.order_vakum or 0) + (p.bumper_vakum or 0))) for p in plans)
            target_total = target_mika_val + target_vakum_val

            fg_lots = [
                l for l in all_range_lots
                if (l.lot_type in ["FG_MIKA", "FG_VAKUM"] or l.lot_qr_code.startswith("FG-"))
                and (l.current_status or "").upper() != "VOID"
            ]

            packed_lots = [
                l for l in fg_lots
                if l.current_status in ["PACKED_VERIFIED", "IN_FREEZER_STORAGE", "IN_SALES_STOCK"]
            ]
            scan_mika = sum(1 for l in packed_lots if "MIKA" in (l.lot_type or l.lot_qr_code or ""))
            scan_vakum = sum(1 for l in packed_lots if "VAKUM" in (l.lot_type or l.lot_qr_code or ""))
            scan_total = scan_mika + scan_vakum

            sales_lots = [
                l for l in fg_lots
                if l.current_status == "IN_SALES_STOCK"
            ]
            sales_mika = sum(1 for l in sales_lots if "MIKA" in (l.lot_type or l.lot_qr_code or ""))
            sales_vakum = sum(1 for l in sales_lots if "VAKUM" in (l.lot_type or l.lot_qr_code or ""))
            sales_total = sales_mika + sales_vakum

            selisih = scan_total - sales_total

            return {
                "division": "PACKING",
                "name": "Meja Packing",
                "unit": "PACK",
                "target_pack": target_total,
                "target_mika": target_mika_val,
                "target_vakum": target_vakum_val,
                "scan_pack": scan_total,
                "scan_mika": scan_mika,
                "scan_vakum": scan_vakum,
                "sales_pack": sales_total,
                "sales_mika": sales_mika,
                "sales_vakum": sales_vakum,
                "selisih_sales": selisih,
                "total_lots": scan_total,
                "total_quantity": scan_total,
                "completed_quantity": scan_total,
                "printed_count": max(0, target_total - scan_total),
                "transit_count": 0,
                "completed_count": scan_total,
                "void_count": 0,
                "pending_lots": [],
                "reject_count": rej_sum,
                "reject_notes": agg_rej_notes,
                "operational_notes": agg_op_notes,
                "updated_by": last_updated_by
            }

        divisions_dict = {
            "DAGING": summarize_range_div("DAGING", "Gilingan"),
            "BUMBU": summarize_range_div("BUMBU", "Karung"),
            "KULIT": summarize_range_div("KULIT", "BAL"),
            "SAOS": summarize_range_div("SAOS", "BAL"),
            "CETAK": calculate_range_cetak(),
            "PACKING": calculate_range_packing()
        }
        total_reject = sum(d["reject_count"] for d in divisions_dict.values())
        total_lots_count = sum(d["total_lots"] for d in divisions_dict.values())
        total_void_count = sum(d.get("void_count", 0) for d in divisions_dict.values())
        total_pending_count = sum(d.get("printed_count", 0) for d in divisions_dict.values())

        inbound_serialized = [
            {
                "inbound_id": item.inbound_id,
                "inbound_date": item.inbound_date.isoformat() if hasattr(item.inbound_date, 'isoformat') else str(item.inbound_date),
                "category": item.category,
                "item_name": item.item_name,
                "quantity": float(item.quantity),
                "unit": item.unit,
                "supplier_name": item.supplier_name,
                "document_no": item.document_no,
                "notes": item.notes,
                "received_by": item.received_by
            }
            for item in inbound_list
        ]

        lots_serialized = [
            {
                "lot_qr_code": l.lot_qr_code,
                "plan_id": l.plan_id,
                "lot_type": l.lot_type,
                "current_status": l.current_status,
                "current_station": l.current_station,
                "quantity": float(l.quantity or 0),
                "unit": l.unit
            }
            for l in all_range_lots
        ]

        date_display = d_start.isoformat() if d_start == d_end else f"{d_start.isoformat()} s/d {d_end.isoformat()}"
        plan_id_str = plans[0].plan_id if len(plans) == 1 else f"PLAN-{d_start.strftime('%Y%m%d')}-RANGE"

        return {
            "is_range": True,
            "date": date_display,
            "recap_date": d_start.isoformat(),
            "start_date": d_start.isoformat(),
            "end_date": d_end.isoformat(),
            "plan_id": plan_id_str,
            "target_gilingan": target_gilingan_sum,
            "total_reject": total_reject,
            "total_lots": total_lots_count,
            "total_void": total_void_count,
            "total_pending": total_pending_count,
            "divisions": divisions_dict,
            "supplier_inbound": inbound_serialized,
            "inbound": inbound_serialized,
            "lots": lots_serialized
        }

    # Mode tanggal tunggal (100% backward compatibility)
    single_date_input = recap_date or start_date or end_date
    target_date = parse_date_param(single_date_input)
    today_code_str = target_date.strftime("%Y%m%d")

    # 1. Ambil catatan divisi hari ini
    notes_query = select(DivisionDailyNote).where(DivisionDailyNote.record_date == target_date)
    notes_res = await db.execute(notes_query)
    notes_map = {n.division: n for n in notes_res.scalars().all()}

    # 2. Ambil barang masuk supplier hari ini
    inbound_query = select(SupplierInbound).where(SupplierInbound.inbound_date == target_date).order_by(desc(SupplierInbound.created_at))
    inbound_res = await db.execute(inbound_query)
    inbound_list = inbound_res.scalars().all()

    # 3. Ambil seluruh lot yang beredar hari ini (berdasarkan pola tanggal di kode lot atau created_at)
    target_dt_start = datetime.combine(target_date, datetime.min.time())
    target_dt_end = datetime.combine(target_date, datetime.max.time())
    lots_query = select(LotTracking).where(
        or_(
            LotTracking.lot_qr_code.like(f"%-{today_code_str}-%"),
            LotTracking.created_at.between(target_dt_start, target_dt_end),
            func.date(LotTracking.created_at) == target_date.isoformat()
        )
    )
    lots_res = await db.execute(lots_query)
    all_today_lots = lots_res.scalars().all()

    # Kelompokkan lot per divisi
    division_lots_single: Dict[str, List[LotTracking]] = {
        "DAGING": [],
        "BUMBU": [],
        "KULIT": [],
        "SAOS": [],
        "PROD_2": []
    }

    for lot in all_today_lots:
        l_type = (lot.lot_type or "").upper()
        if l_type in ["ADONAN", "DAGING"]:
            division_lots_single["DAGING"].append(lot)
        elif l_type == "BUMBU":
            division_lots_single["BUMBU"].append(lot)
        elif "KULIT" in l_type:
            division_lots_single["KULIT"].append(lot)
        elif l_type == "SAOS":
            division_lots_single["SAOS"].append(lot)
        else:
            division_lots_single["PROD_2"].append(lot)

    def summarize_div(div_key: str, default_unit: str) -> Dict[str, Any]:
        lots = division_lots_single.get(div_key, [])
        note = notes_map.get(div_key)
        total_qty = sum(float(l.quantity or 0) for l in lots)
        
        # Hitung status
        printed_count = sum(1 for l in lots if "PRINTED" in (l.current_status or ""))
        transit_count = sum(1 for l in lots if "TRANSIT" in (l.current_status or ""))
        ready_or_delivered = sum(1 for l in lots if "READY" in (l.current_status or "") or "STOCK" in (l.current_status or "") or "IN_" in (l.current_status or ""))
        consumed_count = sum(1 for l in lots if l.current_status == "CONSUMED")
        void_count = sum(1 for l in lots if (l.current_status or "").upper() == "VOID")

        pending_lots = [
            {
                "lot_qr_code": l.lot_qr_code,
                "quantity": float(l.quantity or 0),
                "unit": l.unit or default_unit,
                "current_status": l.current_status
            }
            for l in lots if "PRINTED" in (l.current_status or "")
        ]

        completed_qty = sum(
            float(l.quantity or 0)
            for l in lots
            if "READY" in (l.current_status or "") or "STOCK" in (l.current_status or "") or "IN_" in (l.current_status or "") or l.current_status == "CONSUMED"
        )

        return {
            "division": div_key,
            "total_lots": len(lots),
            "total_quantity": total_qty,
            "completed_quantity": completed_qty,
            "unit": default_unit,
            "printed_count": printed_count,
            "transit_count": transit_count,
            "completed_count": ready_or_delivered + consumed_count,
            "void_count": void_count,
            "pending_lots": pending_lots,
            "reject_count": note.reject_count if note else 0,
            "reject_notes": note.reject_notes if note else "",
            "operational_notes": note.operational_notes if note else "",
            "updated_by": note.created_by if note else "PPIC-AYU"
        }

    # Ambil data rencana produksi hari ini
    plan_query = select(ProductionPlan).where(ProductionPlan.plan_date == target_date).limit(1)
    plan_res = await db.execute(plan_query)
    plan = plan_res.scalar_one_or_none()

    # Hitung metrik Meja Cetak (Single Date)
    adonan_received_single = [
        l for l in all_today_lots
        if (l.lot_type in ["ADONAN", "DAGING"] or l.lot_qr_code.startswith("AD-") or l.lot_qr_code.startswith("DG-"))
        and (l.current_status in ["IN_ASSEMBLY_STATION", "READY_IN_ASSEMBLY_STATION", "CONSUMED"]
             or (l.current_station or "").upper() in ["ASSEMBLY_STATION", "CETAK"])
    ]
    adonan_cnt_single = len(adonan_received_single)
    adonan_qty_single = sum(float(l.quantity or 0) for l in adonan_received_single)
    adonan_transit_single = sum(
        1 for l in all_today_lots
        if (l.lot_type in ["ADONAN", "DAGING"] or l.lot_qr_code.startswith("AD-"))
        and "TRANSIT" in (l.current_status or "")
    )

    kulit_received_single = [
        l for l in all_today_lots
        if ("KULIT" in (l.lot_type or "").upper() or l.lot_qr_code.startswith("KL-"))
        and (l.current_status in ["IN_ASSEMBLY_STATION", "READY_IN_ASSEMBLY_STATION", "CONSUMED"]
             or (l.current_station or "").upper() in ["ASSEMBLY_STATION", "CETAK"])
    ]
    kulit_cnt_single = len(kulit_received_single)
    kulit_qty_single = sum(float(l.quantity or 0) for l in kulit_received_single)
    kulit_transit_single = sum(
        1 for l in all_today_lots
        if ("KULIT" in (l.lot_type or "").upper() or l.lot_qr_code.startswith("KL-"))
        and "TRANSIT" in (l.current_status or "")
    )

    fg_today_lots = [
        l for l in all_today_lots
        if (l.lot_type in ["FG_MIKA", "FG_VAKUM"] or l.lot_qr_code.startswith("FG-"))
        and (l.current_status or "").upper() != "VOID"
    ]
    fg_mika_cnt_single = sum(1 for l in fg_today_lots if "MIKA" in (l.lot_type or l.lot_qr_code or ""))
    fg_vakum_cnt_single = sum(1 for l in fg_today_lots if "VAKUM" in (l.lot_type or l.lot_qr_code or ""))
    total_pcs_single = (fg_mika_cnt_single * 50) + (fg_vakum_cnt_single * 25)

    # Hitung metrik Meja Packing (Single Date)
    target_mika_single = int(plan.target_mika_pax or ((plan.order_mika or 0) + (plan.bumper_mika or 0))) if plan else 0
    target_vakum_single = int(plan.target_vakum_pax or ((plan.order_vakum or 0) + (plan.bumper_vakum or 0))) if plan else 0
    target_total_single = target_mika_single + target_vakum_single

    packed_verified_single = [
        l for l in fg_today_lots
        if l.current_status in ["PACKED_VERIFIED", "IN_FREEZER_STORAGE", "IN_SALES_STOCK"]
    ]
    scan_mika_single = sum(1 for l in packed_verified_single if "MIKA" in (l.lot_type or l.lot_qr_code or ""))
    scan_vakum_single = sum(1 for l in packed_verified_single if "VAKUM" in (l.lot_type or l.lot_qr_code or ""))
    scan_total_single = scan_mika_single + scan_vakum_single

    sales_acc_single = [
        l for l in fg_today_lots
        if l.current_status == "IN_SALES_STOCK"
    ]
    sales_mika_single = sum(1 for l in sales_acc_single if "MIKA" in (l.lot_type or l.lot_qr_code or ""))
    sales_vakum_single = sum(1 for l in sales_acc_single if "VAKUM" in (l.lot_type or l.lot_qr_code or ""))
    sales_total_single = sales_mika_single + sales_vakum_single

    selisih_single = scan_total_single - sales_total_single

    divisions_single_dict = {
        "DAGING": summarize_div("DAGING", "Gilingan"),
        "BUMBU": summarize_div("BUMBU", "Karung"),
        "KULIT": summarize_div("KULIT", "BAL"),
        "SAOS": summarize_div("SAOS", "BAL"),
        "CETAK": {
            "division": "CETAK",
            "name": "Meja Cetak",
            "unit": "PCS",
            "adonan_received_count": adonan_cnt_single,
            "adonan_received_qty": adonan_qty_single,
            "adonan_transit_count": adonan_transit_single,
            "kulit_received_count": kulit_cnt_single,
            "kulit_received_qty": kulit_qty_single,
            "kulit_transit_count": kulit_transit_single,
            "total_pcs_cetak": total_pcs_single,
            "total_quantity": total_pcs_single,
            "total_lots": adonan_cnt_single + kulit_cnt_single,
            "completed_quantity": total_pcs_single,
            "printed_count": 0,
            "transit_count": adonan_transit_single + kulit_transit_single,
            "completed_count": adonan_cnt_single + kulit_cnt_single,
            "void_count": 0,
            "pending_lots": [],
            "reject_count": notes_map["CETAK"].reject_count if "CETAK" in notes_map else 0,
            "reject_notes": notes_map["CETAK"].reject_notes if "CETAK" in notes_map else "",
            "operational_notes": notes_map["CETAK"].operational_notes if "CETAK" in notes_map else "",
            "updated_by": notes_map["CETAK"].created_by if "CETAK" in notes_map else "PPIC-AYU"
        },
        "PACKING": {
            "division": "PACKING",
            "name": "Meja Packing",
            "unit": "PACK",
            "target_pack": target_total_single,
            "target_mika": target_mika_single,
            "target_vakum": target_vakum_single,
            "scan_pack": scan_total_single,
            "scan_mika": scan_mika_single,
            "scan_vakum": scan_vakum_single,
            "sales_pack": sales_total_single,
            "sales_mika": sales_mika_single,
            "sales_vakum": sales_vakum_single,
            "selisih_sales": selisih_single,
            "total_lots": scan_total_single,
            "total_quantity": scan_total_single,
            "completed_quantity": scan_total_single,
            "printed_count": max(0, target_total_single - scan_total_single),
            "transit_count": 0,
            "completed_count": scan_total_single,
            "void_count": 0,
            "pending_lots": [],
            "reject_count": notes_map["PACKING"].reject_count if "PACKING" in notes_map else 0,
            "reject_notes": notes_map["PACKING"].reject_notes if "PACKING" in notes_map else "",
            "operational_notes": notes_map["PACKING"].operational_notes if "PACKING" in notes_map else "",
            "updated_by": notes_map["PACKING"].created_by if "PACKING" in notes_map else "PPIC-AYU"
        }
    }
    total_reject_single = sum(d["reject_count"] for d in divisions_single_dict.values())
    total_lots_single = sum(d["total_lots"] for d in divisions_single_dict.values())
    total_void_single = sum(d.get("void_count", 0) for d in divisions_single_dict.values())
    total_pending_single = sum(d.get("printed_count", 0) for d in divisions_single_dict.values())

    inbound_single_serialized = [
        {
            "inbound_id": item.inbound_id,
            "inbound_date": item.inbound_date.isoformat() if hasattr(item.inbound_date, 'isoformat') else str(item.inbound_date),
            "category": item.category,
            "item_name": item.item_name,
            "quantity": float(item.quantity),
            "unit": item.unit,
            "supplier_name": item.supplier_name,
            "document_no": item.document_no,
            "notes": item.notes,
            "received_by": item.received_by
        }
        for item in inbound_list
    ]

    lots_single_serialized = [
        {
            "lot_qr_code": l.lot_qr_code,
            "plan_id": l.plan_id,
            "lot_type": l.lot_type,
            "current_status": l.current_status,
            "current_station": l.current_station,
            "quantity": float(l.quantity or 0),
            "unit": l.unit
        }
        for l in all_today_lots
    ]

    return {
        "is_range": False,
        "date": target_date.isoformat(),
        "recap_date": target_date.isoformat(),
        "start_date": target_date.isoformat(),
        "end_date": target_date.isoformat(),
        "plan_id": plan.plan_id if plan else f"PLAN-{today_code_str}-01",
        "target_gilingan": plan.target_gilingan if plan else 0,
        "total_reject": total_reject_single,
        "total_lots": total_lots_single,
        "total_void": total_void_single,
        "total_pending": total_pending_single,
        "divisions": divisions_single_dict,
        "supplier_inbound": inbound_single_serialized,
        "inbound": inbound_single_serialized,
        "lots": lots_single_serialized
    }





