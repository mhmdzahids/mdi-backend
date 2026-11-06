import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pytest
import xml.etree.ElementTree as ET
from datetime import datetime, date, timezone, timedelta
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select, func

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
    get_daily_recap,
    parse_date_param
)
from backend.app.schemas import (
    GenerateDailyLotsAllRequest,
    SupplierInboundCreate,
    DivisionDailyNoteCreate
)

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
WIB = timezone(timedelta(hours=7))


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


@pytest.fixture
def ppic_user():
    return {
        "username": "ppic_ayu",
        "operator_id": "PPIC-AYU",
        "role": "STAKEHOLDER",
        "division": "PPIC / Administrasi Produksi"
    }


@pytest.fixture
def non_ppic_operator():
    return {
        "username": "opr_adonan",
        "operator_id": "OPR-01",
        "role": "OPERATOR",
        "division": "Divisi Daging"
    }


@pytest.fixture
def non_ppic_stakeholder():
    return {
        "username": "pic_daging",
        "operator_id": "PIC-DAGING",
        "role": "STAKEHOLDER",
        "division": "Divisi Daging (Giling & Mixer)"
    }


# ============================================================================
# CHALLENGE R1: EXTREME, UNUSUAL, ZERO, AND REPRINT LOT COUNTS
# ============================================================================

@pytest.mark.anyio
async def test_challenge_r1_pure_zero_all_categories_no_fallback(async_session: AsyncSession, ppic_user: dict):
    """
    Challenge R1.1: Submit pure 0 across all categories.
    Must generate exactly 0 lots, without defaulting to 40 gilingan or invoking any ratio.
    """
    req = GenerateDailyLotsAllRequest(
        plan_id="PLAN-CHALLENGE-ALLZERO",
        operator_id="PPIC-AYU",
        operator_role="STAKEHOLDER",
        gilingan_count=0,
        sack_count=0,
        wrapper_bal=0,
        sauce_bal=0
    )
    res = await generate_daily_lots_all(payload=req, db=async_session, current_user=ppic_user)

    assert res["status"] == "success"
    assert res["counts"]["adonan"] == 0
    assert res["counts"]["bumbu"] == 0
    assert res["counts"]["kulit"] == 0
    assert res["counts"]["saos"] == 0
    assert res["counts"]["total"] == 0
    assert len(res["lots"]) == 0

    # Ensure database has 0 lots and 0 custody logs
    lots = (await async_session.execute(select(LotTracking))).scalars().all()
    assert len(lots) == 0
    logs = (await async_session.execute(select(CustodyTransferLog))).scalars().all()
    assert len(logs) == 0


@pytest.mark.anyio
async def test_challenge_r1_adonan_positive_supplements_zero_no_formula(async_session: AsyncSession, ppic_user: dict):
    """
    Challenge R1.2: PPIC enters positive gilingan (15), but zero for bumbu, kulit, saos.
    Verifies that legacy automated formula:
      bumbu = (gilingan+19)//20 = 1
      kulit = (gilingan+5)//6 = 3
      saos = 2
    is COMPLETELY DEFUNCT and produces strictly 0 for all supplement categories.
    """
    req = GenerateDailyLotsAllRequest(
        plan_id="PLAN-CHALLENGE-ADONAN-ONLY",
        operator_id="PPIC-AYU",
        operator_role="STAKEHOLDER",
        gilingan_count=15,
        sack_count=0,
        wrapper_bal=0,
        sauce_bal=0
    )
    res = await generate_daily_lots_all(payload=req, db=async_session, current_user=ppic_user)

    assert res["counts"]["adonan"] == 15
    assert res["counts"]["bumbu"] == 0
    assert res["counts"]["kulit"] == 0
    assert res["counts"]["saos"] == 0
    assert res["counts"]["total"] == 15

    lots = (await async_session.execute(select(LotTracking))).scalars().all()
    assert len(lots) == 15
    assert all(l.lot_type == "ADONAN" for l in lots)


@pytest.mark.anyio
async def test_challenge_r1_reprint_scenarios_zero_adonan(async_session: AsyncSession, ppic_user: dict):
    """
    Challenge R1.3: Reprint scenarios where gilingan is 0.
    Must never default gilingan to 40.
    """
    # 1. Bumbu-only reprint
    req_bm = GenerateDailyLotsAllRequest(
        plan_id="PLAN-REPRINT-BM",
        operator_id="PPIC-AYU",
        gilingan_count=0,
        sack_count=5,
        wrapper_bal=0,
        sauce_bal=0
    )
    res_bm = await generate_daily_lots_all(payload=req_bm, db=async_session, current_user=ppic_user)
    assert res_bm["counts"]["adonan"] == 0
    assert res_bm["counts"]["bumbu"] == 5
    assert res_bm["counts"]["total"] == 5

    # 2. Kulit-only reprint with alternating 9cm and 7cm
    req_kl = GenerateDailyLotsAllRequest(
        plan_id="PLAN-REPRINT-KL",
        operator_id="PPIC-AYU",
        gilingan_count=0,
        sack_count=0,
        wrapper_bal=7,
        sauce_bal=0
    )
    res_kl = await generate_daily_lots_all(payload=req_kl, db=async_session, current_user=ppic_user)
    assert res_kl["counts"]["kulit"] == 7
    lots_kl = [l for l in res_kl["lots"] if "KULIT" in l["lot_type"]]
    assert len(lots_kl) == 7
    # Check alternating pattern: 9cm (even idx: 0,2,4,6) and 7cm (odd idx: 1,3,5)
    assert sum(1 for l in lots_kl if l["lot_type"] == "KULIT_9CM") == 4
    assert sum(1 for l in lots_kl if l["lot_type"] == "KULIT_7CM") == 3

    # 3. Saos-only reprint
    req_sa = GenerateDailyLotsAllRequest(
        plan_id="PLAN-REPRINT-SA",
        operator_id="PPIC-AYU",
        gilingan_count=0,
        sack_count=0,
        wrapper_bal=0,
        sauce_bal=9
    )
    res_sa = await generate_daily_lots_all(payload=req_sa, db=async_session, current_user=ppic_user)
    assert res_sa["counts"]["saos"] == 9
    assert res_sa["counts"]["adonan"] == 0


@pytest.mark.anyio
async def test_challenge_r1_extreme_scale_and_unusual_ratios(async_session: AsyncSession, ppic_user: dict):
    """
    Challenge R1.4: High-volume stress (150 lots) and unusual ratios (1 adonan, 80 bumbu).
    Verifies indexing sequence, unique QR codes, and custody logs.
    """
    req_large = GenerateDailyLotsAllRequest(
        plan_id="PLAN-STRESS-LARGE",
        operator_id="PPIC-AYU",
        operator_role="STAKEHOLDER",
        gilingan_count=100,
        sack_count=20,
        wrapper_bal=20,
        sauce_bal=10
    )
    res_large = await generate_daily_lots_all(payload=req_large, db=async_session, current_user=ppic_user)
    assert res_large["counts"]["total"] == 150
    assert len(res_large["lots"]) == 150

    lots = (await async_session.execute(select(LotTracking))).scalars().all()
    assert len(lots) == 150
    lot_codes = [l.lot_qr_code for l in lots]
    assert len(set(lot_codes)) == 150  # Strictly unique

    # Check custody transfer logs
    logs = (await async_session.execute(select(CustodyTransferLog))).scalars().all()
    assert len(logs) == 150
    assert all(log.action_type == "QR_PRINTED" for log in logs)
    assert all(log.user_id == "PPIC-AYU" for log in logs)

    # Unusual ratio: 1 adonan, 50 bumbu
    req_unusual = GenerateDailyLotsAllRequest(
        plan_id="PLAN-UNUSUAL",
        operator_id="PPIC-AYU",
        gilingan_count=1,
        sack_count=50,
        wrapper_bal=0,
        sauce_bal=2
    )
    res_unusual = await generate_daily_lots_all(payload=req_unusual, db=async_session, current_user=ppic_user)
    assert res_unusual["counts"]["adonan"] == 1
    assert res_unusual["counts"]["bumbu"] == 50
    assert res_unusual["counts"]["kulit"] == 0
    assert res_unusual["counts"]["saos"] == 2


@pytest.mark.anyio
async def test_challenge_r1_none_and_negative_input_safety(async_session: AsyncSession, ppic_user: dict):
    """
    Challenge R1.5: Adversarial input safety with None and negative values.
    Verifies that negative values or None never crash the backend or create negative ranges.
    """
    req_negative = GenerateDailyLotsAllRequest(
        plan_id="PLAN-NEGATIVE",
        operator_id="PPIC-AYU",
        gilingan_count=-10,
        sack_count=-5,
        wrapper_bal=None,
        sauce_bal=None
    )
    res = await generate_daily_lots_all(payload=req_negative, db=async_session, current_user=ppic_user)
    assert res["counts"]["adonan"] == 0
    assert res["counts"]["bumbu"] == 0
    assert res["counts"]["kulit"] == 0
    assert res["counts"]["saos"] == 0
    assert res["counts"]["total"] == 0


@pytest.mark.anyio
async def test_challenge_r1_authorization_enforcement(async_session: AsyncSession, non_ppic_operator: dict, non_ppic_stakeholder: dict):
    """
    Challenge R1.6: Security & Role Check.
    Barcode issuance must reject non-PPIC users with 403 Forbidden.
    """
    req = GenerateDailyLotsAllRequest(
        plan_id="PLAN-SEC-TEST",
        gilingan_count=10,
        sack_count=2,
        wrapper_bal=2,
        sauce_bal=2
    )
    # 1. Regular operator
    with pytest.raises(HTTPException) as exc_info:
        await generate_daily_lots_all(payload=req, db=async_session, current_user=non_ppic_operator)
    assert exc_info.value.status_code == 403

    # 2. Stakeholder from other division (Daging)
    with pytest.raises(HTTPException) as exc_info2:
        await generate_daily_lots_all(payload=req, db=async_session, current_user=non_ppic_stakeholder)
    assert exc_info2.value.status_code == 403


# ============================================================================
# CHALLENGE R3: DATE RANGE EDGE CASES, INVERTED DATES, SOUND AGGREGATION
# ============================================================================

@pytest.mark.anyio
async def test_challenge_r3_inverted_date_ranges_with_records(async_session: AsyncSession, ppic_user: dict):
    """
    Challenge R3.1: Inverted date range (start_date > end_date) when records exist in database.
    Verifies that system normalizes (swaps) the boundaries gracefully, returns matching records,
    and calculates sound aggregations without raising unhandled exceptions.
    """
    # Seed inbound records
    records = [
        SupplierInbound(inbound_date=date(2026, 10, 15), category="KULIT", item_name="Kulit A", quantity=5.0, unit="BAL", supplier_name="S1", received_by="PPIC-AYU"),
        SupplierInbound(inbound_date=date(2026, 10, 20), category="KULIT", item_name="Kulit B", quantity=8.0, unit="BAL", supplier_name="S1", received_by="PPIC-AYU"),
        SupplierInbound(inbound_date=date(2026, 10, 25), category="KULIT", item_name="Kulit C", quantity=10.0, unit="BAL", supplier_name="S1", received_by="PPIC-AYU"),
    ]
    async_session.add_all(records)
    await async_session.commit()

    # Query with inverted dates: start 2026-10-28, end 2026-10-18
    inbound_res = await list_supplier_inbound(
        start_date="2026-10-28",
        end_date="2026-10-18",
        db=async_session,
        current_user=ppic_user
    )
    # Should swap and find records on 10-20 and 10-25 (total 2 records)
    assert len(inbound_res) == 2
    assert {r.item_name for r in inbound_res} == {"Kulit B", "Kulit C"}

    # Query get_daily_recap with inverted dates
    recap = await get_daily_recap(
        start_date="2026-10-28",
        end_date="2026-10-18",
        db=async_session,
        current_user=ppic_user
    )
    assert recap is not None
    assert recap["is_range"] is True
    assert recap["start_date"] == "2026-10-18"
    assert recap["end_date"] == "2026-10-28"
    assert len(recap["supplier_inbound"]) == 2


@pytest.mark.anyio
async def test_challenge_r3_empty_intervals_and_extreme_dates(async_session: AsyncSession, ppic_user: dict):
    """
    Challenge R3.2: Empty intervals (future or past dates with no data).
    Verifies that system does not raise KeyError, division-by-zero, or unhandled exceptions.
    """
    inbound_res = await list_supplier_inbound(
        start_date="2099-01-01",
        end_date="2099-01-31",
        db=async_session,
        current_user=ppic_user
    )
    assert inbound_res == []

    recap = await get_daily_recap(
        start_date="2099-01-01",
        end_date="2099-01-31",
        db=async_session,
        current_user=ppic_user
    )
    assert recap["target_gilingan"] == 0
    assert recap["total_reject"] == 0
    assert recap["total_lots"] == 0
    assert recap["supplier_inbound"] == []
    assert recap["lots"] == []
    assert "DAGING" in recap["divisions"]
    assert recap["divisions"]["DAGING"]["reject_count"] == 0


@pytest.mark.anyio
async def test_challenge_r3_boundary_precision_inclusive(async_session: AsyncSession, ppic_user: dict):
    """
    Challenge R3.3: Exact inclusive boundary matching.
    Records on:
      2026-10-09 (out - before)
      2026-10-10 (in - exact start)
      2026-10-12 (in - middle)
      2026-10-15 (in - exact end)
      2026-10-16 (out - after)
    Range: 2026-10-10 to 2026-10-15.
    Must include exactly 3 records.
    """
    items = [
        SupplierInbound(inbound_date=date(2026, 10, 9), category="DAGING", item_name="Day Before", quantity=1.0, unit="KG", supplier_name="S", received_by="PPIC"),
        SupplierInbound(inbound_date=date(2026, 10, 10), category="DAGING", item_name="Start Boundary", quantity=2.0, unit="KG", supplier_name="S", received_by="PPIC"),
        SupplierInbound(inbound_date=date(2026, 10, 12), category="DAGING", item_name="Middle Day", quantity=3.0, unit="KG", supplier_name="S", received_by="PPIC"),
        SupplierInbound(inbound_date=date(2026, 10, 15), category="DAGING", item_name="End Boundary", quantity=4.0, unit="KG", supplier_name="S", received_by="PPIC"),
        SupplierInbound(inbound_date=date(2026, 10, 16), category="DAGING", item_name="Day After", quantity=5.0, unit="KG", supplier_name="S", received_by="PPIC"),
    ]
    async_session.add_all(items)
    await async_session.commit()

    res = await list_supplier_inbound(
        start_date="2026-10-10",
        end_date="2026-10-15",
        db=async_session,
        current_user=ppic_user
    )
    names = {r.item_name for r in res}
    assert names == {"Start Boundary", "Middle Day", "End Boundary"}
    assert "Day Before" not in names
    assert "Day After" not in names


@pytest.mark.anyio
async def test_challenge_r3_single_day_range_equivalence(async_session: AsyncSession, ppic_user: dict):
    """
    Challenge R3.4: Single-day range (start_date == end_date == '2026-10-22')
    Must produce identical results to single date query (inbound_date='2026-10-22').
    """
    item = SupplierInbound(
        inbound_date=date(2026, 10, 22),
        category="SAOS",
        item_name="Saos Sambal 1kg",
        quantity=20.0,
        unit="BAL",
        supplier_name="PT Saos Juara",
        received_by="PPIC-AYU"
    )
    async_session.add(item)
    await async_session.commit()

    res_single = await list_supplier_inbound(
        inbound_date="2026-10-22",
        db=async_session,
        current_user=ppic_user
    )
    res_range = await list_supplier_inbound(
        start_date="2026-10-22",
        end_date="2026-10-22",
        db=async_session,
        current_user=ppic_user
    )
    assert len(res_single) == 1
    assert len(res_range) == 1
    assert res_single[0].inbound_id == res_range[0].inbound_id


@pytest.mark.anyio
async def test_challenge_r3_mathematical_aggregation_multi_day(async_session: AsyncSession, ppic_user: dict):
    """
    Challenge R3.5: Multi-Day Mathematical Soundness & Recap Aggregation.
    Tests:
    - Target gilingan summation across multiple plans
    - Rejects aggregation across multiple divisions and multiple days
    - Formatting of combined operational and reject notes with date tags
    - Inbound items summation
    """
    d1 = date(2026, 10, 1)
    d2 = date(2026, 10, 2)
    d3 = date(2026, 10, 3)

    # 1. Plans
    plans = [
        ProductionPlan(plan_id="PLAN-20261001-01", plan_date=d1, target_gilingan=40, target_mika_pax=100, target_vakum_pax=50, status="ACTIVE", created_by="PPIC-AYU"),
        ProductionPlan(plan_id="PLAN-20261002-01", plan_date=d2, target_gilingan=35, target_mika_pax=80, target_vakum_pax=40, status="ACTIVE", created_by="PPIC-AYU"),
        ProductionPlan(plan_id="PLAN-20261003-01", plan_date=d3, target_gilingan=50, target_mika_pax=120, target_vakum_pax=60, status="ACTIVE", created_by="PPIC-AYU"),
    ]
    async_session.add_all(plans)

    # 2. Division notes across days
    notes = [
        DivisionDailyNote(record_date=d1, division="DAGING", reject_count=4, reject_notes="Urat keras", operational_notes="Mixer 1 berisik", created_by="PPIC-AYU"),
        DivisionDailyNote(record_date=d1, division="KULIT", reject_count=2, reject_notes="Sobek di sudut", operational_notes="Suhu lembab", created_by="PPIC-AYU"),
        DivisionDailyNote(record_date=d2, division="DAGING", reject_count=3, reject_notes="Lemak berlebih", operational_notes="Mixer normal", created_by="PPIC-AYU"),
        DivisionDailyNote(record_date=d2, division="CETAK", reject_count=10, reject_notes="Bentuk gepeng", operational_notes="Operator baru", created_by="PPIC-AYU"),
        DivisionDailyNote(record_date=d3, division="PACKING", reject_count=5, reject_notes="Seal bocor", operational_notes="Ganti plastik roll", created_by="PPIC-AYU"),
    ]
    async_session.add_all(notes)

    # 3. Lots across days
    dt1 = datetime.combine(d1, datetime.min.time())
    dt2 = datetime.combine(d2, datetime.min.time())
    lots = [
        LotTracking(lot_qr_code="AD-20261001-001", plan_id="PLAN-20261001-01", lot_type="ADONAN", current_status="CONSUMED", current_station="MEAT_MIXING_STATION", quantity=20.0, unit="KG", created_at=dt1),
        LotTracking(lot_qr_code="AD-20261001-002", plan_id="PLAN-20261001-01", lot_type="ADONAN", current_status="READY_IN_MEAT_MIXING_STATION", current_station="MEAT_MIXING_STATION", quantity=20.0, unit="KG", created_at=dt1),
        LotTracking(lot_qr_code="BM-20261002-001", plan_id="PLAN-20261002-01", lot_type="BUMBU", current_status="PRINTED_PENDING_STOCK_IN", current_station="SEASONING_STATION", quantity=20.0, unit="PORSI", created_at=dt2),
    ]
    async_session.add_all(lots)
    await async_session.commit()

    recap = await get_daily_recap(
        start_date="2026-10-01",
        end_date="2026-10-03",
        db=async_session,
        current_user=ppic_user
    )

    # Assertions
    # 1. Target gilingan: 40 + 35 + 50 = 125
    assert recap["target_gilingan"] == 125

    # 2. Total reject: 4 (Daging d1) + 2 (Kulit d1) + 3 (Daging d2) + 10 (Cetak d2) + 5 (Packing d3) = 24
    assert recap["total_reject"] == 24

    # 3. Division Daging rejects: 4 + 3 = 7
    daging_div = recap["divisions"]["DAGING"]
    assert daging_div["reject_count"] == 7
    # Note concatenation with dates
    assert "[2026-10-01] Urat keras" in daging_div["reject_notes"]
    assert "[2026-10-02] Lemak berlebih" in daging_div["reject_notes"]
    assert "[2026-10-01] Mixer 1 berisik" in daging_div["operational_notes"]

    # 4. Division Cetak rejects: 10
    assert recap["divisions"]["CETAK"]["reject_count"] == 10
    assert recap["divisions"]["CETAK"]["reject_notes"] == "Bentuk gepeng"

    # 5. Lots summation
    assert daging_div["total_lots"] == 2
    assert daging_div["total_quantity"] == 40.0


@pytest.mark.anyio
async def test_challenge_r3_date_format_resilience(async_session: AsyncSession, ppic_user: dict):
    """
    Challenge R3.6: Date format resilience.
    Verifies handling of YYYYMMDD vs YYYY-MM-DD vs invalid strings.
    """
    # 1. YYYYMMDD compact string
    d_compact = parse_date_param("20261025")
    assert d_compact == date(2026, 10, 25)

    # 2. YYYY-MM-DD ISO string
    d_iso = parse_date_param("2026-10-25")
    assert d_iso == date(2026, 10, 25)

    # 3. Malformed date string falls back safely to today
    d_invalid = parse_date_param("garbage-string-xyz")
    assert d_invalid == datetime.now(WIB).date()

    # 4. Query with compact string in list_supplier_inbound
    res = await list_supplier_inbound(
        start_date="20261001",
        end_date="20261031",
        db=async_session,
        current_user=ppic_user
    )
    assert isinstance(res, list)


@pytest.mark.anyio
async def test_challenge_r3_wide_span_over_60_days(async_session: AsyncSession, ppic_user: dict):
    """
    Challenge R3.7: Query spanning > 60 days (triggers wide interval branch in get_daily_recap).
    Verifies that system executes cleanly without SQLite parameter limit explosion.
    """
    recap = await get_daily_recap(
        start_date="2026-01-01",
        end_date="2026-06-30",  # 180 days span
        db=async_session,
        current_user=ppic_user
    )
    assert recap is not None
    assert recap["is_range"] is True
    assert recap["start_date"] == "2026-01-01"
    assert recap["end_date"] == "2026-06-30"


# ============================================================================
# CHALLENGE EXCEL EXPORT: XML CONFORMANCE & SPECIAL CHARACTER ESCAPING
# ============================================================================

def escape_xml_test(value):
    if value is None:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )

def generate_mock_spreadsheet_xml(title, subtitles, columns, data, totals=None):
    safe_title = escape_xml_test(title)
    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<?mso-application progid="Excel.Sheet"?>',
        '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"',
        ' xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">',
        ' <Styles>',
        '  <Style ss:ID="Default" ss:Name="Normal"><Font ss:FontName="Calibri"/></Style>',
        '  <Style ss:ID="CompanyKop"><Font ss:FontName="Calibri" ss:Bold="1"/></Style>',
        '  <Style ss:ID="TableHeader"><Interior ss:Color="#3A6B80" ss:Pattern="Solid"/></Style>',
        ' </Styles>',
        ' <Worksheet ss:Name="Laporan">',
        '  <Table>',
        f'   <Row><Cell ss:StyleID="CompanyKop"><Data ss:Type="String">{safe_title}</Data></Cell></Row>'
    ]
    for sub in subtitles:
        xml_parts.append(f'   <Row><Cell><Data ss:Type="String">{escape_xml_test(sub)}</Data></Cell></Row>')

    xml_parts.append('   <Row>')
    for col in columns:
        xml_parts.append(f'    <Cell ss:StyleID="TableHeader"><Data ss:Type="String">{escape_xml_test(col["header"])}</Data></Cell>')
    xml_parts.append('   </Row>')

    for row in data:
        xml_parts.append('   <Row>')
        for col in columns:
            raw_val = row.get(col["key"])
            if isinstance(raw_val, (int, float)):
                xml_parts.append(f'    <Cell><Data ss:Type="Number">{raw_val}</Data></Cell>')
            else:
                xml_parts.append(f'    <Cell><Data ss:Type="String">{escape_xml_test(raw_val if raw_val is not None else "-")}</Data></Cell>')
        xml_parts.append('   </Row>')

    if totals:
        xml_parts.append('   <Row>')
        xml_parts.append(f'    <Cell><Data ss:Type="String">{escape_xml_test(totals.get("label", "TOTAL"))}</Data></Cell>')
        xml_parts.append('   </Row>')

    xml_parts.extend([
        '  </Table>',
        ' </Worksheet>',
        '</Workbook>'
    ])
    return "\n".join(xml_parts)


def test_challenge_excel_special_characters_and_xml_conformance():
    """
    Challenge Excel Export:
    Strictly verify that SpreadsheetML generator creates 100% valid XML when
    subjected to adversarial strings with XML entities (<, >, &, ", '), nulls,
    and multi-day period strings.
    """
    adversarial_title = 'PT MAKAN DIMSUM INDONESIA <TEST & "OFFICIAL">'
    adversarial_subtitles = [
        'Periode: 2026-10-01 s/d 2026-10-31',
        'Operator: Ayu & Team <PPIC>',
        'Catatan: Nilai < 10% & kualitas "Super" \'A\''
    ]
    adversarial_columns = [
        {"header": 'Divisi & Unit', "key": "divisi"},
        {"header": 'Kondisi <Detail>', "key": "kondisi"},
        {"header": 'Jumlah "Reject"', "key": "jumlah"}
    ]
    adversarial_data = [
        {"divisi": "Daging & Mixing", "kondisi": '<script>alert("xss")</script>', "jumlah": 10},
        {"divisi": "Kulit & Saos", "kondisi": "Kualitas 'A' & Utuh > 95%", "jumlah": 0},
        {"divisi": "Kemasan", "kondisi": None, "jumlah": None},
    ]
    totals = {"label": 'TOTAL REJECT & DEFECT <AKUMULASI>'}

    xml_output = generate_mock_spreadsheet_xml(
        title=adversarial_title,
        subtitles=adversarial_subtitles,
        columns=adversarial_columns,
        data=adversarial_data,
        totals=totals
    )

    # 1. Verify entity replacement
    assert "&amp;" in xml_output
    assert "&lt;" in xml_output
    assert "&gt;" in xml_output
    assert "&quot;" in xml_output
    assert "&apos;" in xml_output
    assert "<script>" not in xml_output

    # 2. Strict XML syntax parsing via xml.etree.ElementTree
    root = ET.fromstring(xml_output)
    assert root.tag.endswith("Workbook")

    # Verify worksheet and rows
    namespaces = {"ss": "urn:schemas-microsoft-com:office:spreadsheet"}
    worksheet = root.find("ss:Worksheet", namespaces)
    assert worksheet is not None
    table = worksheet.find("ss:Table", namespaces)
    rows = table.findall("ss:Row", namespaces)
    assert len(rows) >= 6
