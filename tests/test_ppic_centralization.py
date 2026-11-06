import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pytest
from datetime import datetime, timezone, timedelta
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select

from backend.app.models import (
    Base,
    LotTracking,
    CustodyTransferLog,
    ProductionPlan,
    SupplierInbound,
    DivisionDailyNote
)
from backend.app.routers.production1 import (
    generate_daily_lots_all,
    create_supplier_inbound,
    list_supplier_inbound,
    save_division_daily_note,
    list_division_daily_notes,
    get_daily_recap,
    produce_batch_unified
)
from backend.app.schemas import (
    GenerateDailyLotsAllRequest,
    SupplierInboundCreate,
    DivisionDailyNoteCreate,
    ProduceBatchUnifiedRequest
)
from backend.app.auth import require_ppic

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

@pytest.fixture
def anyio_backend():
    return "asyncio"

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
async def test_require_ppic_blocks_regular_pic(async_session: AsyncSession):
    """PIC Divisi biasa dilarang membuat QR; hanya PPIC (Ayu) / Supervisor yang berwenang."""
    pic_daging_user = {
        "username": "pic_daging",
        "operator_id": "PIC-DAGING",
        "role": "STAKEHOLDER",
        "division": "Divisi Daging (Giling & Mixer)"
    }
    checker = require_ppic()
    with pytest.raises(HTTPException) as exc_info:
        await checker(current_user=pic_daging_user)
    assert exc_info.value.status_code == 403
    assert "Hanya PPIC (Ayu)" in exc_info.value.detail


@pytest.mark.anyio
async def test_generate_daily_lots_all_by_ayu(async_session: AsyncSession):
    """Ayu (PPIC) dapat menerbitkan seluruh barcode harian untuk Adonan, Bumbu, Kulit, dan Saos sekaligus."""
    ayu_user = {
        "username": "ppic_ayu",
        "operator_id": "PPIC-AYU",
        "role": "STAKEHOLDER",
        "division": "PPIC / Administrasi Produksi"
    }

    req = GenerateDailyLotsAllRequest(
        plan_id="PLAN-TEST-01",
        operator_id="PPIC-AYU",
        operator_role="STAKEHOLDER",
        gilingan_count=5,
        sack_count=1,
        wrapper_bal=2,
        sauce_bal=1
    )

    result = await generate_daily_lots_all(payload=req, db=async_session, current_user=ayu_user)
    assert result["status"] == "success"
    assert result["counts"]["adonan"] == 5
    assert result["counts"]["bumbu"] == 1
    assert result["counts"]["kulit"] == 2
    assert result["counts"]["saos"] == 1
    assert result["counts"]["total"] == 9

    # Verifikasi di database
    lots_res = await async_session.execute(select(LotTracking))
    lots = lots_res.scalars().all()
    assert len(lots) == 9

    ad_lots = [l for l in lots if l.lot_type == "ADONAN"]
    assert len(ad_lots) == 5
    assert all(l.lot_qr_code.startswith("AD-") for l in ad_lots)


@pytest.mark.anyio
async def test_generate_daily_lots_all_detailed_mode(async_session: AsyncSession):
    """PPIC dapat menerbitkan barcode kulit & saos secara spesifik (Mika 9cm, Vakum 7cm, Saos Mika, Saos Vakum)."""
    ayu_user = {
        "username": "ppic_ayu",
        "operator_id": "PPIC-AYU",
        "role": "STAKEHOLDER",
        "division": "PPIC / Administrasi Produksi"
    }

    req = GenerateDailyLotsAllRequest(
        plan_id="PLAN-TEST-DETAILED-01",
        operator_id="PPIC-AYU",
        operator_role="STAKEHOLDER",
        gilingan_count=2,
        sack_count=1,
        wrapper_9cm_bal=2,
        wrapper_7cm_bal=3,
        sauce_mika_bal=1,
        sauce_vakum_bal=2
    )

    result = await generate_daily_lots_all(payload=req, db=async_session, current_user=ayu_user)
    assert result["status"] == "success"
    assert result["counts"]["adonan"] == 2
    assert result["counts"]["bumbu"] == 1
    assert result["counts"]["kulit"] == 5
    assert result["counts"]["saos"] == 3
    assert result["counts"]["total"] == 11

    # Verifikasi tipe lot spesifik di database
    lots_res = await async_session.execute(select(LotTracking))
    lots = lots_res.scalars().all()

    k9 = [l for l in lots if l.lot_type == "KULIT_9CM"]
    k7 = [l for l in lots if l.lot_type == "KULIT_7CM"]
    sm = [l for l in lots if l.lot_type == "SAOS_MIKA"]
    sv = [l for l in lots if l.lot_type == "SAOS_VAKUM"]

    assert len(k9) == 2
    assert len(k7) == 3
    assert len(sm) == 1
    assert len(sv) == 2


@pytest.mark.anyio
async def test_supplier_inbound_recording(async_session: AsyncSession):
    """PPIC dapat mencatat penerimaan barang supplier luar secara fleksibel & dinamis."""
    ayu_user = {
        "username": "ppic_ayu",
        "operator_id": "PPIC-AYU",
        "role": "STAKEHOLDER",
        "division": "PPIC / Administrasi Produksi"
    }

    item1 = SupplierInboundCreate(
        category="KULIT",
        item_name="Kulit Dimsum 9 cm (Mika)",
        quantity=10.0,
        unit="BAL",
        supplier_name="CV Sumber Kulit Jaya",
        document_no="SJ-20261029-01",
        notes="Kondisi bersih dan dingin"
    )
    res1 = await create_supplier_inbound(payload=item1, db=async_session, current_user=ayu_user)
    assert res1.inbound_id is not None
    assert res1.item_name == "Kulit Dimsum 9 cm (Mika)"
    assert res1.quantity == 10.0

    item2 = SupplierInboundCreate(
        category="KEMASAN",
        item_name="Mika Dimsum Isi 10",
        quantity=5.0,
        unit="DUS",
        supplier_name="Pabrik Mika Abadi",
        document_no="SJ-9988",
        notes="5 dus @ 500 pcs"
    )
    res2 = await create_supplier_inbound(payload=item2, db=async_session, current_user=ayu_user)
    assert res2.inbound_id is not None

    inbounds = await list_supplier_inbound(db=async_session, current_user=ayu_user)
    assert len(inbounds) == 2


@pytest.mark.anyio
async def test_division_daily_note_and_recap(async_session: AsyncSession):
    """Perekaman reject, catatan kendala divisi, dan rekapitulasi harian PPIC."""
    ayu_user = {
        "username": "ppic_ayu",
        "operator_id": "PPIC-AYU",
        "role": "STAKEHOLDER",
        "division": "PPIC / Administrasi Produksi"
    }

    # Simpan catatan reject divisi Daging
    note = DivisionDailyNoteCreate(
        division="DAGING",
        reject_count=2,
        reject_notes="2 kg daging berlemak keras dipisahkan",
        operational_notes="Selesai giling pukul 11:30"
    )
    saved_note = await save_division_daily_note(payload=note, db=async_session, current_user=ayu_user)
    assert saved_note.division == "DAGING"
    assert saved_note.reject_count == 2

    # Ambil daily recap
    recap = await get_daily_recap(db=async_session, current_user=ayu_user)
    assert "divisions" in recap
    assert recap["divisions"]["DAGING"]["reject_count"] == 2
    assert recap["divisions"]["DAGING"]["reject_notes"] == "2 kg daging berlemak keras dipisahkan"
    assert recap["divisions"]["DAGING"]["operational_notes"] == "Selesai giling pukul 11:30"


@pytest.mark.anyio
async def test_generate_daily_lots_exact_counts_no_formula_fallbacks(async_session: AsyncSession):
    """
    R1 Acceptance Criteria:
    Backend generate_daily_lots_all must generate exact lot counts as manually submitted by PPIC
    without enforcing formula constraints, minimums, or forced defaults (no 40 gilingan, no sauce_bal: 2).
    """
    ayu_user = {
        "username": "ppic_ayu",
        "operator_id": "PPIC-AYU",
        "role": "STAKEHOLDER",
        "division": "PPIC / Administrasi Produksi"
    }

    # Test 1: Submit 3 gilingan, 1 karung, 0 bal kulit, 0 bal saos
    req_zero_supplements = GenerateDailyLotsAllRequest(
        plan_id="PLAN-EXACT-01",
        operator_id="PPIC-AYU",
        operator_role="STAKEHOLDER",
        gilingan_count=3,
        sack_count=1,
        wrapper_bal=0,
        sauce_bal=0
    )
    res1 = await generate_daily_lots_all(payload=req_zero_supplements, db=async_session, current_user=ayu_user)
    assert res1["status"] == "success"
    assert res1["counts"]["adonan"] == 3
    assert res1["counts"]["bumbu"] == 1
    assert res1["counts"]["kulit"] == 0
    assert res1["counts"]["saos"] == 0
    assert res1["counts"]["total"] == 4
    assert len(res1["lots"]) == 4

    # Test 2: Submit 3 bal kulit with alternating 9cm and 7cm
    req_kulit_alternating = GenerateDailyLotsAllRequest(
        plan_id="PLAN-EXACT-02",
        operator_id="PPIC-AYU",
        operator_role="STAKEHOLDER",
        gilingan_count=0,
        sack_count=0,
        wrapper_bal=3,
        sauce_bal=1
    )
    res2 = await generate_daily_lots_all(payload=req_kulit_alternating, db=async_session, current_user=ayu_user)
    assert res2["counts"]["adonan"] == 0
    assert res2["counts"]["bumbu"] == 0
    assert res2["counts"]["kulit"] == 3
    assert res2["counts"]["saos"] == 1
    assert res2["counts"]["total"] == 4

    kulit_lots = [l for l in res2["lots"] if "KULIT" in l["lot_type"]]
    assert len(kulit_lots) == 3
    assert kulit_lots[0]["lot_type"] == "KULIT_9CM"
    assert kulit_lots[1]["lot_type"] == "KULIT_7CM"
    assert kulit_lots[2]["lot_type"] == "KULIT_9CM"


@pytest.mark.anyio
async def test_supplier_inbound_date_range_filtering(async_session: AsyncSession):
    """
    R3 Acceptance Criteria:
    Supplier inbound supports single date and date range queries with optional category filter.
    """
    ayu_user = {
        "username": "ppic_ayu",
        "operator_id": "PPIC-AYU",
        "role": "STAKEHOLDER",
        "division": "PPIC / Administrasi Produksi"
    }

    # Insert inbound records across multiple dates
    d1 = datetime(2026, 10, 20).date()
    d2 = datetime(2026, 10, 22).date()
    d3 = datetime(2026, 10, 25).date()
    d4 = datetime(2026, 10, 28).date()

    records = [
        SupplierInbound(inbound_date=d1, category="DAGING", item_name="Ayam Giling", quantity=50.0, unit="KG", supplier_name="Supplier 1", received_by="PPIC-AYU"),
        SupplierInbound(inbound_date=d2, category="KULIT", item_name="Kulit 9cm", quantity=10.0, unit="BAL", supplier_name="Supplier 2", received_by="PPIC-AYU"),
        SupplierInbound(inbound_date=d3, category="BUMBU", item_name="Bumbu Sparta", quantity=5.0, unit="DUS", supplier_name="Supplier 3", received_by="PPIC-AYU"),
        SupplierInbound(inbound_date=d4, category="KULIT", item_name="Kulit 7cm", quantity=8.0, unit="BAL", supplier_name="Supplier 2", received_by="PPIC-AYU"),
    ]
    for r in records:
        async_session.add(r)
    await async_session.commit()

    # Query 1: Single date d2 (2026-10-22)
    single_res = await list_supplier_inbound(inbound_date="2026-10-22", db=async_session, current_user=ayu_user)
    assert len(single_res) == 1
    assert single_res[0].item_name == "Kulit 9cm"

    # Query 2: Date range d2 to d3 (2026-10-22 to 2026-10-25)
    range_res = await list_supplier_inbound(start_date="2026-10-22", end_date="2026-10-25", db=async_session, current_user=ayu_user)
    assert len(range_res) == 2
    items = [r.item_name for r in range_res]
    assert "Kulit 9cm" in items
    assert "Bumbu Sparta" in items
    assert "Ayam Giling" not in items
    assert "Kulit 7cm" not in items

    # Query 3: Date range with category filter
    range_cat_res = await list_supplier_inbound(start_date="2026-10-20", end_date="2026-10-28", category="KULIT", db=async_session, current_user=ayu_user)
    assert len(range_cat_res) == 2
    assert all(r.category == "KULIT" for r in range_cat_res)


@pytest.mark.anyio
async def test_daily_recap_date_range_aggregation(async_session: AsyncSession):
    """
    R3 Acceptance Criteria:
    get_daily_recap aggregates division reject counts, notes, supplier inbounds,
    production plans, and lots across custom date ranges.
    """
    ayu_user = {
        "username": "ppic_ayu",
        "operator_id": "PPIC-AYU",
        "role": "STAKEHOLDER",
        "division": "PPIC / Administrasi Produksi"
    }

    d1 = datetime(2026, 10, 23).date()
    d2 = datetime(2026, 10, 24).date()

    # 1. Division notes on d1 and d2
    note1 = DivisionDailyNote(
        record_date=d1,
        division="DAGING",
        reject_count=3,
        reject_notes="Lemak keras",
        operational_notes="Selesai 11:00",
        created_by="PPIC-AYU"
    )
    note2 = DivisionDailyNote(
        record_date=d2,
        division="DAGING",
        reject_count=4,
        reject_notes="Potongan alot",
        operational_notes="Selesai 12:00",
        created_by="PPIC-AYU"
    )
    note_cetak = DivisionDailyNote(
        record_date=d1,
        division="CETAK",
        reject_count=15,
        reject_notes="Kulit sobek",
        operational_notes="Meja 2 kalibrasi",
        created_by="PPIC-AYU"
    )
    async_session.add_all([note1, note2, note_cetak])

    # 2. Production plans on d1 and d2
    plan1 = ProductionPlan(
        plan_id="PLAN-20261023-01",
        plan_date=d1,
        target_gilingan=30,
        target_mika_pax=1000,
        target_vakum_pax=500,
        status="PLANNED",
        created_by="PPIC-AYU"
    )
    plan2 = ProductionPlan(
        plan_id="PLAN-20261024-01",
        plan_date=d2,
        target_gilingan=40,
        target_mika_pax=1200,
        target_vakum_pax=600,
        status="PLANNED",
        created_by="PPIC-AYU"
    )
    async_session.add_all([plan1, plan2])

    # 3. Supplier inbound on d1
    inbound = SupplierInbound(
        inbound_date=d1,
        category="DAGING",
        item_name="Daging Paha",
        quantity=200.0,
        unit="KG",
        supplier_name="PT Meat Indo",
        received_by="PPIC-AYU"
    )
    async_session.add(inbound)

    # 4. Lots on d1
    lot1 = LotTracking(
        lot_qr_code="AD-20261023-001",
        plan_id="PLAN-20261023-01",
        lot_type="ADONAN",
        current_status="READY_IN_MEAT_MIXING_STATION",
        current_station="MEAT_MIXING_STATION",
        quantity=20.0,
        unit="KG",
        created_at=datetime(2026, 10, 23, 8, 0, 0)
    )
    lot2 = LotTracking(
        lot_qr_code="BM-20261024-001",
        plan_id="PLAN-20261024-01",
        lot_type="BUMBU",
        current_status="PRINTED_PENDING_STOCK_IN",
        current_station="SEASONING_STATION",
        quantity=20.0,
        unit="PORSI",
        created_at=datetime(2026, 10, 24, 9, 0, 0)
    )
    async_session.add_all([lot1, lot2])
    await async_session.commit()

    # Execute range query: 2026-10-23 to 2026-10-24
    recap = await get_daily_recap(
        start_date="2026-10-23",
        end_date="2026-10-24",
        db=async_session,
        current_user=ayu_user
    )

    assert recap["is_range"] is True
    assert recap["start_date"] == "2026-10-23"
    assert recap["end_date"] == "2026-10-24"
    assert recap["target_gilingan"] == 70  # 30 + 40
    assert recap["total_reject"] == 22     # 3 + 4 + 15

    # Check division DAGING: reject summed across both days (3 + 4 = 7)
    daging = recap["divisions"]["DAGING"]
    assert daging["reject_count"] == 7
    assert "[2026-10-23] Lemak keras" in daging["reject_notes"]
    assert "[2026-10-24] Potongan alot" in daging["reject_notes"]
    assert "[2026-10-23] Selesai 11:00" in daging["operational_notes"]
    assert "[2026-10-24] Selesai 12:00" in daging["operational_notes"]

    # Check division CETAK
    cetak = recap["divisions"]["CETAK"]
    assert cetak["reject_count"] == 15
    assert cetak["reject_notes"] == "Kulit sobek"

    # Check inbound
    assert len(recap["supplier_inbound"]) == 1
    assert recap["supplier_inbound"][0]["item_name"] == "Daging Paha"

    # Check lots
    assert len(recap["lots"]) == 2
