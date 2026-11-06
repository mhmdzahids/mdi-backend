import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pytest
import anyio
from datetime import datetime
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select

from backend.app.models import (
    Base,
    LotTracking,
    CustodyTransferLog,
    ScanEvent,
    DiscrepancyLog,
    ProductionPlan,
    ScanSetting
)
from backend.app.routers.production1 import dev_wipe_database

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
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.mark.anyio
async def test_wipe_database_forbidden_for_non_ppic(async_session: AsyncSession):
    operator_user = {
        "username": "opr_daging",
        "operator_id": "OPR-DAGING",
        "role": "OPERATOR",
        "division": "Divisi Daging (Giling & Mixer)"
    }

    with pytest.raises(HTTPException) as exc_info:
        await dev_wipe_database(db=async_session, current_user=operator_user)

    assert exc_info.value.status_code == 403
    assert "hanya diizinkan untuk PPIC" in exc_info.value.detail


@pytest.mark.anyio
async def test_wipe_database_success_for_ppic(async_session: AsyncSession):
    supervisor_user = {
        "username": "supervisor",
        "operator_id": "STK-YOSI",
        "role": "STAKEHOLDER",
        "division": "Supervisor & PPIC"
    }

    # Seed data dummy
    plan = ProductionPlan(
        plan_id="PLAN-OLD-01",
        plan_date=datetime.utcnow().date(),
        target_gilingan=50,
        target_mika_pax=10,
        target_vakum_pax=10,
        status="ACTIVE",
        created_by="TEST"
    )
    async_session.add(plan)
    await async_session.flush()

    lot = LotTracking(
        lot_qr_code="LOT-TEST-001",
        plan_id="PLAN-OLD-01",
        lot_type="DAGING",
        current_status="DICETAK",
        current_station="MEAT",
        quantity=15,
        unit="KG"
    )
    async_session.add(lot)
    await async_session.flush()

    log = CustodyTransferLog(
        lot_qr_code="LOT-TEST-001",
        action_type="LOT_GENERATED",
        station_id="MEAT",
        user_id="OPR-01",
        user_role_active="OPERATOR"
    )
    async_session.add(log)

    event = ScanEvent(
        lot_qr_code="LOT-TEST-001",
        station_id="MEAT",
        user_id="OPR-01",
        action="CONFIRM_STOCK",
        mode_applied="1X",
        previous_status="DICETAK",
        new_status="SIAP_KIRIM"
    )
    async_session.add(event)

    discrepancy = DiscrepancyLog(
        plan_id="PLAN-OLD-01",
        expected_qty=15,
        actual_qty=14,
        variance=-1,
        unit="KG"
    )
    async_session.add(discrepancy)

    setting = ScanSetting(
        station_id="MEAT_STATION",
        scan_mode="2X",
        updated_by="TEST"
    )
    async_session.add(setting)

    await async_session.commit()

    # Eksekusi wipe
    res = await dev_wipe_database(db=async_session, current_user=supervisor_user)

    assert res["status"] == "success"
    assert "Database berhasil di-wipe" in res["message"]
    assert "PLAN-" in res["plan_id"]

    # Verifikasi tabel setelah wipe
    lots = (await async_session.execute(select(LotTracking))).scalars().all()
    logs = (await async_session.execute(select(CustodyTransferLog))).scalars().all()
    events = (await async_session.execute(select(ScanEvent))).scalars().all()
    discrepancies = (await async_session.execute(select(DiscrepancyLog))).scalars().all()
    plans = (await async_session.execute(select(ProductionPlan))).scalars().all()
    settings = (await async_session.execute(select(ScanSetting))).scalars().all()

    assert len(lots) == 0
    assert len(logs) == 0
    assert len(events) == 0
    assert len(discrepancies) == 0
    assert len(plans) == 1
    assert plans[0].target_gilingan == 0
    assert len(settings) == 1
    assert settings[0].station_id == "GLOBAL"
    assert settings[0].scan_mode == "1X"
