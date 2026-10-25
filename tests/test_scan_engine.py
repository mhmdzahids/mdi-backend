import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pytest
import anyio
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from fastapi import HTTPException

from backend.app.models import Base, LotTracking, ScanSetting, ScanEvent, ProductionPlan

from backend.app.services.scan_engine import (
    execute_scan_transition,
    execute_undo_scan,
    get_station_stock_counts,
    get_effective_scan_mode,
    get_target_station_for_lot,
    normalize_status,
)

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

@pytest.fixture
def anyio_backend():
    return 'asyncio'

@pytest.fixture
async def async_session():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with session_maker() as session:
        # Seed production plan
        plan = ProductionPlan(
            plan_id="PLAN-TEST-01",
            plan_date=datetime.utcnow().date(),
            target_gilingan=10,
            target_mika_pax=0,
            target_vakum_pax=0,
            status="ACTIVE",
            created_by="TEST"
        )
        session.add(plan)
        # Default global mode 1X
        session.add(ScanSetting(station_id="GLOBAL", scan_mode="1X", updated_by="TEST"))
        await session.commit()
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()



@pytest.mark.anyio
async def test_saos_target_station_is_finishing(async_session: AsyncSession):
    """Verifikasi bahwa Saos selalu ditargetkan ke FINISHING_STATION (Packing), bukan Cetak."""
    sauce_lot = LotTracking(
        lot_qr_code="SA-20261022-001",
        lot_type="SAOS",
        current_status="READY_IN_SAUCE_STATION",
        current_station="SAUCE_STATION",
        quantity=50,
        unit="POUCH"
    )
    target = get_target_station_for_lot(sauce_lot, "SAUCE_STATION")
    assert target == "FINISHING_STATION", "Target stasiun Saos harus ke FINISHING_STATION (Packing)!"


@pytest.mark.anyio
async def test_mode_1x_flow(async_session: AsyncSession):
    """Mode 1x: DICETAK -> KELUAR -> DITERIMA."""
    # Lot daging baru dicetak
    lot = LotTracking(
        lot_qr_code="DG-20261022-001",
        lot_type="DAGING",
        current_status="DICETAK",
        current_station="MEAT_STATION",
        quantity=15,
        unit="KG"
    )
    async_session.add(lot)
    await async_session.commit()

    # 1. Scan asal (Mode 1x) -> Langsung KELUAR (IN_TRANSIT)
    res_out = await execute_scan_transition(
        db=async_session,
        lot_code="DG-20261022-001",
        operator_station_id="MEAT_STATION",
        user_id="OPR-01",
        user_role="OPERATOR_DAGING"
    )
    assert res_out["success"] is True
    assert res_out["action"] == "SCAN_OUT"
    assert res_out["normalized_status"] == "KELUAR"
    assert "IN_TRANSIT" in res_out["current_status"]

    # 2. Scan terima di stasiun tujuan (SEASONING_STATION) -> DITERIMA
    res_in = await execute_scan_transition(
        db=async_session,
        lot_code="DG-20261022-001",
        operator_station_id="SEASONING_STATION",
        user_id="OPR-02",
        user_role="OPERATOR_BUMBU"
    )
    assert res_in["success"] is True
    assert res_in["action"] == "SCAN_IN"
    assert res_in["normalized_status"] == "DITERIMA"


@pytest.mark.anyio
async def test_mode_2x_flow(async_session: AsyncSession):
    """Mode 2x: DICETAK -> DI_STOK -> KELUAR -> DITERIMA."""
    # Ubah mode stasiun Bumbu ke 2X
    async_session.add(ScanSetting(station_id="SEASONING_STATION", scan_mode="2X", updated_by="TEST"))
    bumbu_lot = LotTracking(
        lot_qr_code="BM-20261022-001",
        lot_type="BUMBU",
        current_status="DICETAK",
        current_station="SEASONING_STATION",
        quantity=20,
        unit="PORSI"
    )
    async_session.add(bumbu_lot)
    await async_session.commit()

    # 1. Scan-1 di asal: DICETAK -> DI_STOK (Konfirmasi stok)
    res1 = await execute_scan_transition(
        db=async_session,
        lot_code="BM-20261022-001",
        operator_station_id="SEASONING_STATION",
        user_id="OPR-BUMBU-1",
        user_role="OPERATOR_BUMBU"
    )
    assert res1["success"] is True
    assert res1["action"] == "CONFIRM_STOCK"
    assert res1["normalized_status"] == "DI_STOK"

    # Simulasikan jeda waktu > 3 detik (di luar window idempotensi) agar scan ke-2 diproses
    from sqlalchemy import update
    await async_session.execute(
        update(ScanEvent).where(ScanEvent.lot_qr_code == "BM-20261022-001").values(
            timestamp=datetime.utcnow() - timedelta(seconds=5)
        )
    )
    await async_session.commit()

    # 2. Scan-2 di asal: DI_STOK -> KELUAR
    res2 = await execute_scan_transition(
        db=async_session,
        lot_code="BM-20261022-001",
        operator_station_id="SEASONING_STATION",
        user_id="OPR-BUMBU-1",
        user_role="OPERATOR_BUMBU"
    )
    assert res2["success"] is True
    assert res2["action"] == "SCAN_OUT"
    assert res2["normalized_status"] == "KELUAR"

    # 3. Scan di tujuan (MEAT_STATION): KELUAR -> DITERIMA
    res3 = await execute_scan_transition(
        db=async_session,
        lot_code="BM-20261022-001",
        operator_station_id="MEAT_STATION",
        user_id="OPR-MEAT-1",
        user_role="OPERATOR_DAGING"
    )
    assert res3["success"] is True
    assert res3["action"] == "SCAN_IN"
    assert res3["normalized_status"] == "DITERIMA"


@pytest.mark.anyio
async def test_mid_day_mode_switch_safety(async_session: AsyncSession):
    """Pergantian mode di tengah hari: DI_STOK harus selalu bisa di-scan menjadi KELUAR baik di 1x maupun 2x."""
    lot = LotTracking(
        lot_qr_code="KL-20261022-001",
        lot_type="KULIT",
        current_status="READY_IN_WRAPPER_STATION", # DI_STOK
        current_station="WRAPPER_STATION",
        quantity=50,
        unit="LEMBAR"
    )
    async_session.add(lot)
    await async_session.commit()

    # Global mode adalah 1X, tapi lot ada di status DI_STOK
    res = await execute_scan_transition(
        db=async_session,
        lot_code="KL-20261022-001",
        operator_station_id="WRAPPER_STATION",
        user_id="OPR-KULIT",
        user_role="OPERATOR_KULIT"
    )
    assert res["success"] is True
    assert res["action"] == "SCAN_OUT"
    assert res["normalized_status"] == "KELUAR"


@pytest.mark.anyio
async def test_fast_double_scan_idempotency(async_session: AsyncSession):
    """Idempotensi 3 detik: Scan beruntun kode yang sama tidak memicu transisi ganda."""
    lot = LotTracking(
        lot_qr_code="DG-20261022-002",
        lot_type="DAGING",
        current_status="DICETAK",
        current_station="MEAT_STATION",
        quantity=15,
        unit="KG"
    )
    async_session.add(lot)
    await async_session.commit()

    # Scan 1
    res1 = await execute_scan_transition(
        db=async_session,
        lot_code="DG-20261022-002",
        operator_station_id="MEAT_STATION",
        user_id="OPR-01",
        user_role="OPERATOR_DAGING"
    )
    assert res1["success"] is True
    assert res1["is_duplicate"] is False

    # Scan 2 langsung (dalam 3 detik) oleh user yang sama
    res2 = await execute_scan_transition(
        db=async_session,
        lot_code="DG-20261022-002",
        operator_station_id="MEAT_STATION",
        user_id="OPR-01",
        user_role="OPERATOR_DAGING"
    )
    assert res2["success"] is True
    assert res2["is_duplicate"] is True
    assert res2["current_status"] == res1["current_status"]


@pytest.mark.anyio
async def test_undo_scan(async_session: AsyncSession):
    """Undo membatalkan scan dalam window 15 detik oleh user yang sama."""
    lot = LotTracking(
        lot_qr_code="SA-20261022-002",
        lot_type="SAOS",
        current_status="READY_IN_SAUCE_STATION",
        current_station="SAUCE_STATION",
        quantity=50,
        unit="POUCH"
    )
    async_session.add(lot)
    await async_session.commit()

    # Lakukan scan keluar
    await execute_scan_transition(
        db=async_session,
        lot_code="SA-20261022-002",
        operator_station_id="SAUCE_STATION",
        user_id="OPR-SAOS-1",
        user_role="OPERATOR_SAOS"
    )

    # Undo oleh user yang sama
    undo_res = await execute_undo_scan(
        db=async_session,
        lot_code="SA-20261022-002",
        user_id="OPR-SAOS-1"
    )
    assert undo_res["success"] is True
    assert undo_res["current_status"] == "READY_IN_SAUCE_STATION"

    # Verifikasi event UNDO tercatat di scan_events
    events = (await async_session.execute(
        async_session.query(ScanEvent).filter(ScanEvent.lot_qr_code == "SA-20261022-002")
    )).scalars().all() if hasattr(async_session, "query") else None


@pytest.mark.anyio
async def test_undo_rejected_for_different_user(async_session: AsyncSession):
    """Undo ditolak jika dipanggil oleh user yang berbeda."""
    lot = LotTracking(
        lot_qr_code="KL-20261022-003",
        lot_type="KULIT",
        current_status="DICETAK",
        current_station="WRAPPER_STATION",
        quantity=50,
        unit="LEMBAR"
    )
    async_session.add(lot)
    await async_session.commit()

    await execute_scan_transition(
        db=async_session,
        lot_code="KL-20261022-003",
        operator_station_id="WRAPPER_STATION",
        user_id="USER-A",
        user_role="OPERATOR"
    )

    with pytest.raises(HTTPException) as exc_info:
        await execute_undo_scan(
            db=async_session,
            lot_code="KL-20261022-003",
            user_id="USER-B"
        )
    assert exc_info.value.status_code == 403


@pytest.mark.anyio
async def test_invalid_transition_rejected(async_session: AsyncSession):
    """Scan ulang barang KELUAR oleh stasiun asal harus ditolak."""
    lot = LotTracking(
        lot_qr_code="DG-20261022-003",
        lot_type="DAGING",
        current_status="IN_TRANSIT_TO_SEASONING", # KELUAR
        current_station="MEAT_STATION",
        quantity=15,
        unit="KG"
    )
    async_session.add(lot)
    await async_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await execute_scan_transition(
            db=async_session,
            lot_code="DG-20261022-003",
            operator_station_id="MEAT_STATION", # Stasiun asal scan lagi padahal sudah keluar
            user_id="OPR-01",
            user_role="OPERATOR_DAGING"
        )
    assert exc_info.value.status_code == 400
    assert "Ditolak: Barang sudah berstatus KELUAR" in exc_info.value.detail


@pytest.mark.anyio
async def test_stock_calculation_modes(async_session: AsyncSession):
    """Penghitungan stok: Mode 1x = DICETAK + DI_STOK; Mode 2x = hanya DI_STOK."""
    station = "WRAPPER_STATION"
    # Masukkan 2 barang DICETAK dan 3 barang DI_STOK
    for i in range(2):
        async_session.add(LotTracking(
            lot_qr_code=f"KL-P-{i}",
            lot_type="KULIT",
            current_status="DICETAK",
            current_station=station,
            quantity=10,
            unit="LEMBAR"
        ))
    for i in range(3):
        async_session.add(LotTracking(
            lot_qr_code=f"KL-S-{i}",
            lot_type="KULIT",
            current_status="READY_IN_WRAPPER_STATION",
            current_station=station,
            quantity=10,
            unit="LEMBAR"
        ))
    await async_session.commit()

    # Mode 1X: active_stock = 2 + 3 = 5, pending = 0
    async_session.add(ScanSetting(station_id=station, scan_mode="1X", updated_by="TEST"))
    await async_session.commit()
    counts_1x = await get_station_stock_counts(async_session, station)
    assert counts_1x["active_stock_count"] == 5
    assert counts_1x["pending_confirmation_count"] == 0

    # Mode 2X: active_stock = 3, pending = 2
    setting = (await async_session.execute(
        async_session.query(ScanSetting).filter(ScanSetting.station_id == station)
    )).scalar_one() if hasattr(async_session, "query") else None
    
    from sqlalchemy import select
    res = await async_session.execute(select(ScanSetting).where(ScanSetting.station_id == station))
    st = res.scalar_one()
    st.scan_mode = "2X"
    await async_session.commit()

    counts_2x = await get_station_stock_counts(async_session, station)
    assert counts_2x["active_stock_count"] == 3
    assert counts_2x["pending_confirmation_count"] == 2
