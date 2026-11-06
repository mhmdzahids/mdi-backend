import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pytest
import anyio
from datetime import datetime
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from backend.app.models import Base, LotTracking, ScanSetting, ProductionPlan
from backend.app.schemas import (
    UpdateScanModeRequest,
    ScanTransitionRequest,
    ScanBatchTransitionRequest,
    ScanUndoRequest
)
from backend.app.routers.scan import (
    get_scan_mode_settings,
    update_scan_mode_settings,
    process_scan_endpoint,
    process_batch_scan_endpoint,
    undo_scan_endpoint,
    get_station_stock_endpoint
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
        session.add(ProductionPlan(
            plan_id="PLAN-API-TEST",
            plan_date=datetime.utcnow().date(),
            target_gilingan=10,
            target_mika_pax=0,
            target_vakum_pax=0,
            status="ACTIVE",
            created_by="TEST"
        ))
        session.add(ScanSetting(station_id="GLOBAL", scan_mode="1X", updated_by="TEST"))
        await session.commit()
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.mark.anyio
async def test_router_get_and_update_settings(async_session: AsyncSession):
    supervisor_user = {
        "username": "supervisor",
        "operator_id": "SPV-01",
        "role": "SUPERVISOR",
        "division": "PPIC"
    }

    # 1. GET settings
    res = await get_scan_mode_settings(db=async_session, current_user=supervisor_user)
    assert res.global_mode == "1X"
    assert len(res.stations) >= 5

    # 2. UPDATE settings to 2X
    payload = UpdateScanModeRequest(
        global_mode="2X",
        station_modes={"MEAT_STATION": "2X", "SAUCE_STATION": "1X"}
    )
    res_update = await update_scan_mode_settings(payload=payload, db=async_session, current_user=supervisor_user)
    assert res_update.global_mode == "2X"
    assert res_update.station_modes["MEAT_STATION"] == "2X"
    assert res_update.station_modes["SAUCE_STATION"] == "1X"


@pytest.mark.anyio
async def test_router_process_scan_and_undo(async_session: AsyncSession):
    operator_user = {
        "username": "opr_daging",
        "operator_id": "OPR-DAGING-1",
        "role": "OPERATOR",
        "division": "Divisi Daging"
    }

    # Buat lot baru
    lot = LotTracking(
        lot_qr_code="DG-API-001",
        lot_type="DAGING",
        current_status="DICETAK",
        current_station="MEAT_STATION",
        quantity=15,
        unit="KG"
    )
    async_session.add(lot)
    await async_session.commit()

    # 1. Process Scan (Global 1X -> KELUAR)
    scan_req = ScanTransitionRequest(lot_code="DG-API-001", station_id="MEAT_STATION")
    scan_res = await process_scan_endpoint(payload=scan_req, db=async_session, current_user=operator_user)
    assert scan_res["success"] is True
    assert scan_res["action"] == "SCAN_OUT"
    assert scan_res["can_undo"] is True

    # 2. Undo Scan
    undo_req = ScanUndoRequest(lot_code="DG-API-001")
    undo_res = await undo_scan_endpoint(payload=undo_req, db=async_session, current_user=operator_user)
    assert undo_res["success"] is True
    assert undo_res["current_status"] == "DICETAK"


@pytest.mark.anyio
async def test_router_process_batch_scan_intent(async_session: AsyncSession):
    operator_user = {
        "username": "opr_daging",
        "operator_id": "OPR-DAGING-1",
        "role": "OPERATOR",
        "division": "Divisi Daging"
    }

    # Buat 2 lot adonan baru
    lot1 = LotTracking(
        lot_qr_code="AD-BATCH-001",
        lot_type="ADONAN",
        current_status="PRINTED_PENDING_STOCK_IN",
        current_station="MEAT_STATION",
        quantity=19,
        unit="KG"
    )
    lot2 = LotTracking(
        lot_qr_code="AD-BATCH-002",
        lot_type="ADONAN",
        current_status="PRINTED_PENDING_STOCK_IN",
        current_station="MEAT_STATION",
        quantity=19,
        unit="KG"
    )
    async_session.add_all([lot1, lot2])
    await async_session.commit()

    # Eksekusi batch Scan Out di Mode 1X
    batch_req = ScanBatchTransitionRequest(
        lot_codes=["AD-BATCH-001", "AD-BATCH-002"],
        station_id="MEAT_STATION",
        intent="SCAN_OUT"
    )
    results = await process_batch_scan_endpoint(payload=batch_req, db=async_session, current_user=operator_user)
    assert len(results) == 2
    assert results[0]["success"] is True
    assert results[0]["current_status"] == "IN_TRANSIT_TO_ASSEMBLY"
    assert results[1]["success"] is True
    assert results[1]["current_status"] == "IN_TRANSIT_TO_ASSEMBLY"

