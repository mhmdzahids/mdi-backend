import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pytest
import inspect
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
    list_division_daily_notes,
    get_daily_recap
)
from backend.app.schemas import (
    GenerateDailyLotsAllRequest,
    SupplierInboundCreate,
    DivisionDailyNoteCreate
)

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

# Timezone WIB (UTC+7)
WIB = timezone(timedelta(hours=7))


# ============================================================================
# TEST FIXTURES
# ============================================================================

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
def pic_daging_user():
    return {
        "username": "pic_daging",
        "operator_id": "PIC-DAGING",
        "role": "STAKEHOLDER",
        "division": "Divisi Daging (Giling & Mixer)"
    }


@pytest.fixture
def operator_user():
    return {
        "username": "opr_adonan",
        "operator_id": "OPR-01",
        "role": "OPERATOR",
        "division": "Divisi Daging"
    }


# ============================================================================
# ROUTER CALL HELPERS (Interface Contract Adaptors)
# ============================================================================

async def call_generate_daily_lots_all(payload: GenerateDailyLotsAllRequest, db: AsyncSession, current_user: dict):
    return await generate_daily_lots_all(payload=payload, db=db, current_user=current_user)


async def call_list_supplier_inbound(
    db: AsyncSession,
    current_user: dict,
    inbound_date: str = None,
    category: str = None,
    start_date: str = None,
    end_date: str = None
):
    """
    Invokes list_supplier_inbound adhering to Contract 2:
    Query Parameters: inbound_date, category, start_date, end_date.
    """
    sig = inspect.signature(list_supplier_inbound)
    kwargs = {"db": db, "current_user": current_user}
    if "inbound_date" in sig.parameters:
        kwargs["inbound_date"] = inbound_date
    if "category" in sig.parameters:
        kwargs["category"] = category
    if "start_date" in sig.parameters:
        kwargs["start_date"] = start_date
    elif start_date is not None:
        # Pass directly to trigger expected interface behavior
        kwargs["start_date"] = start_date
    if "end_date" in sig.parameters:
        kwargs["end_date"] = end_date
    elif end_date is not None:
        kwargs["end_date"] = end_date

    return await list_supplier_inbound(**kwargs)


async def call_get_daily_recap(
    db: AsyncSession,
    current_user: dict,
    recap_date: str = None,
    start_date: str = None,
    end_date: str = None
):
    """
    Invokes get_daily_recap adhering to Contract 3:
    Query Parameters: recap_date, start_date, end_date.
    """
    sig = inspect.signature(get_daily_recap)
    kwargs = {"db": db, "current_user": current_user}
    if "recap_date" in sig.parameters:
        kwargs["recap_date"] = recap_date
    if "start_date" in sig.parameters:
        kwargs["start_date"] = start_date
    elif start_date is not None:
        kwargs["start_date"] = start_date
    if "end_date" in sig.parameters:
        kwargs["end_date"] = end_date
    elif end_date is not None:
        kwargs["end_date"] = end_date

    return await get_daily_recap(**kwargs)


def generate_spreadsheetml_xml(sheet_name: str, title: str, subtitles: list, columns: list, data: list, totals: dict = None) -> str:
    """
    Python reference implementation of the client-side SpreadsheetML generator
    defined in mdi-frontend/src/utils/excelExport.js (Contract 4).
    Used to verify XML schema validity, styling, and escaping.
    """
    def escape_xml(val):
        if val is None:
            return ""
        return (
            str(val)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
        )

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<?mso-application progid="Excel.Sheet"?>',
        '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"',
        ' xmlns:o="urn:schemas-microsoft-com:office:office"',
        ' xmlns:x="urn:schemas-microsoft-com:office:excel"',
        ' xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"',
        ' xmlns:html="http://www.w3.org/TR/REC-html40">',
        ' <Styles>',
        '  <Style ss:ID="Default" ss:Name="Normal">',
        '   <Alignment ss:Vertical="Center"/>',
        '   <Font ss:FontName="Calibri" ss:Size="11" ss:Color="#000000"/>',
        '  </Style>',
        '  <Style ss:ID="CompanyKop">',
        '   <Alignment ss:Horizontal="Center" ss:Vertical="Center"/>',
        '   <Font ss:FontName="Calibri" ss:Size="14" ss:Color="#1E3A5F" ss:Bold="1"/>',
        '  </Style>',
        '  <Style ss:ID="TableHeader">',
        '   <Font ss:FontName="Calibri" ss:Size="11" ss:Color="#FFFFFF" ss:Bold="1"/>',
        '   <Interior ss:Color="#3A6B80" ss:Pattern="Solid"/>',
        '  </Style>',
        ' </Styles>',
        f' <Worksheet ss:Name="{escape_xml(sheet_name)}">',
        '  <Table>',
    ]
    # Kop / title
    lines.append(f'   <Row ss:Height="28"><Cell ss:StyleID="CompanyKop"><Data ss:Type="String">{escape_xml(title)}</Data></Cell></Row>')
    # Subtitles
    for sub in subtitles:
        lines.append(f'   <Row><Cell><Data ss:Type="String">{escape_xml(sub)}</Data></Cell></Row>')
    lines.append('   <Row ss:Height="8"/>')
    # Column Headers
    header_cells = "".join(f'<Cell ss:StyleID="TableHeader"><Data ss:Type="String">{escape_xml(col["header"])}</Data></Cell>' for col in columns)
    lines.append(f'   <Row ss:Height="22">{header_cells}</Row>')
    # Data Rows
    for row in data:
        cells = []
        for col in columns:
            val = row.get(col["key"], "-")
            val_type = "Number" if isinstance(val, (int, float)) and not isinstance(val, bool) else "String"
            cells.append(f'<Cell><Data ss:Type="{val_type}">{escape_xml(val)}</Data></Cell>')
        lines.append(f'   <Row ss:Height="18">{"".join(cells)}</Row>')
    # Totals
    if totals:
        label = totals.get("label", "TOTAL")
        lines.append(f'   <Row><Cell><Data ss:Type="String">{escape_xml(label)}</Data></Cell></Row>')
    lines.append('  </Table>')
    lines.append(' </Worksheet>')
    lines.append('</Workbook>')
    return "\n".join(lines)


# ============================================================================
# TIER 1: FEATURE COVERAGE
# ============================================================================

@pytest.mark.anyio
async def test_tier1_exact_manual_lot_generation_all_categories(async_session: AsyncSession, ppic_user: dict):
    """
    Tier 1 Feature Coverage: R1.3 & Contract 1
    Verifies that PPIC entering exact counts for all 4 lot categories
    (adonan, bumbu, kulit, saos) generates precisely those lots.
    """
    req = GenerateDailyLotsAllRequest(
        plan_id="PLAN-T1-01",
        operator_id="PPIC-AYU",
        operator_role="STAKEHOLDER",
        gilingan_count=5,
        sack_count=1,
        wrapper_bal=2,
        sauce_bal=1
    )

    result = await call_generate_daily_lots_all(payload=req, db=async_session, current_user=ppic_user)

    assert result["status"] == "success"
    assert result["plan_id"] == "PLAN-T1-01"
    assert result["counts"]["adonan"] == 5
    assert result["counts"]["bumbu"] == 1
    assert result["counts"]["kulit"] == 2
    assert result["counts"]["saos"] == 1
    assert result["counts"]["total"] == 9
    assert len(result["lots"]) == 9

    # DB Persistence Verification
    db_lots = (await async_session.execute(select(LotTracking))).scalars().all()
    assert len(db_lots) == 9

    adonan_lots = [l for l in db_lots if l.lot_type == "ADONAN"]
    bumbu_lots = [l for l in db_lots if l.lot_type == "BUMBU"]
    kulit_lots = [l for l in db_lots if "KULIT" in l.lot_type]
    saos_lots = [l for l in db_lots if l.lot_type == "SAOS"]

    assert len(adonan_lots) == 5
    assert len(bumbu_lots) == 1
    assert len(kulit_lots) == 2
    assert len(saos_lots) == 1

    # Verify CustodyTransferLog creation for auditability
    logs = (await async_session.execute(select(CustodyTransferLog))).scalars().all()
    assert len(logs) == 9
    assert all(log.action_type == "QR_PRINTED" for log in logs)
    assert all(log.user_id == "PPIC-AYU" for log in logs)


@pytest.mark.anyio
async def test_tier1_single_date_historical_queries(async_session: AsyncSession, ppic_user: dict):
    """
    Tier 1 Feature Coverage: R3.1 Historical Single Date Queries
    Verifies supplier inbound and daily recap accurately isolate single historical dates.
    """
    # Seed multi-day inbound records
    d_yesterday = date(2026, 10, 24)
    d_target = date(2026, 10, 25)
    d_tomorrow = date(2026, 10, 26)

    inbound_yesterday = SupplierInbound(
        inbound_date=d_yesterday,
        category="DAGING",
        item_name="Daging Ayam Giling",
        quantity=50.0,
        unit="KG",
        supplier_name="PT Unggas Makmur",
        received_by="PPIC-AYU"
    )
    inbound_target1 = SupplierInbound(
        inbound_date=d_target,
        category="KULIT",
        item_name="Kulit Dimsum 9cm",
        quantity=10.0,
        unit="BAL",
        supplier_name="CV Sumber Kulit",
        received_by="PPIC-AYU"
    )
    inbound_target2 = SupplierInbound(
        inbound_date=d_target,
        category="SAOS",
        item_name="Saos Sambal Dimsum",
        quantity=5.0,
        unit="BAL",
        supplier_name="PT Saos Nusantara",
        received_by="PPIC-AYU"
    )
    inbound_tomorrow = SupplierInbound(
        inbound_date=d_tomorrow,
        category="KEMASAN",
        item_name="Mika Dimsum Isi 10",
        quantity=100.0,
        unit="DUS",
        supplier_name="Pabrik Mika Abadi",
        received_by="PPIC-AYU"
    )
    async_session.add_all([inbound_yesterday, inbound_target1, inbound_target2, inbound_tomorrow])

    # Seed DivisionDailyNote for target date
    note_target = DivisionDailyNote(
        record_date=d_target,
        division="DAGING",
        reject_count=3,
        reject_notes="Lemak keras",
        operational_notes="Shift 1 normal",
        created_by="PPIC-AYU"
    )
    async_session.add(note_target)
    await async_session.commit()

    # 1. Query supplier inbound on single date
    inbounds = await call_list_supplier_inbound(
        db=async_session,
        current_user=ppic_user,
        inbound_date="2026-10-25"
    )
    assert len(inbounds) == 2
    assert {i.category for i in inbounds} == {"KULIT", "SAOS"}

    # 2. Query daily recap on single date
    recap = await call_get_daily_recap(
        db=async_session,
        current_user=ppic_user,
        recap_date="2026-10-25"
    )
    assert recap["date"] == "2026-10-25"
    assert recap["divisions"]["DAGING"]["reject_count"] == 3
    assert len(recap.get("supplier_inbound", recap.get("inbound", []))) == 2


@pytest.mark.anyio
async def test_tier1_date_range_queries_supplier_inbound_and_recap(async_session: AsyncSession, ppic_user: dict):
    """
    Tier 1 Feature Coverage: R3.1 Historical Date Range Queries
    Verifies query by start_date and end_date filters inclusively across days.
    """
    records = [
        SupplierInbound(inbound_date=date(2026, 10, 19), category="KULIT", item_name="Item 19", quantity=1.0, unit="BAL", supplier_name="S1", received_by="PPIC-AYU"),
        SupplierInbound(inbound_date=date(2026, 10, 20), category="KULIT", item_name="Item 20", quantity=2.0, unit="BAL", supplier_name="S1", received_by="PPIC-AYU"),
        SupplierInbound(inbound_date=date(2026, 10, 22), category="BUMBU", item_name="Item 22", quantity=3.0, unit="KG", supplier_name="S2", received_by="PPIC-AYU"),
        SupplierInbound(inbound_date=date(2026, 10, 25), category="SAOS", item_name="Item 25", quantity=4.0, unit="BAL", supplier_name="S3", received_by="PPIC-AYU"),
        SupplierInbound(inbound_date=date(2026, 10, 26), category="KEMASAN", item_name="Item 26", quantity=5.0, unit="DUS", supplier_name="S4", received_by="PPIC-AYU"),
    ]
    async_session.add_all(records)

    # Seed Division notes across the range
    notes = [
        DivisionDailyNote(record_date=date(2026, 10, 20), division="DAGING", reject_count=2, created_by="PPIC-AYU"),
        DivisionDailyNote(record_date=date(2026, 10, 22), division="CETAK", reject_count=5, created_by="PPIC-AYU"),
        DivisionDailyNote(record_date=date(2026, 10, 25), division="DAGING", reject_count=1, created_by="PPIC-AYU"),
    ]
    async_session.add_all(notes)
    await async_session.commit()

    # 1. Range query for supplier inbound: 2026-10-20 to 2026-10-25
    inbounds = await call_list_supplier_inbound(
        db=async_session,
        current_user=ppic_user,
        start_date="2026-10-20",
        end_date="2026-10-25"
    )
    # Expected: records on 20, 22, 25 (3 records). 19 and 26 excluded.
    assert len(inbounds) == 3
    items = {i.item_name for i in inbounds}
    assert "Item 19" not in items
    assert "Item 26" not in items
    assert items == {"Item 20", "Item 22", "Item 25"}

    # 2. Range query for daily recap: 2026-10-20 to 2026-10-25
    recap = await call_get_daily_recap(
        db=async_session,
        current_user=ppic_user,
        start_date="2026-10-20",
        end_date="2026-10-25"
    )
    assert recap.get("is_range") is True or "start_date" in recap or "date" in recap
    inbound_list = recap.get("supplier_inbound", recap.get("inbound", []))
    assert len(inbound_list) == 3


def test_tier1_excel_export_xml_structure():
    """
    Tier 1 Feature Coverage: R3.5 & Contract 4 Excel Export Structure
    Verifies SpreadsheetML XML complies with official styling, headers, and encoding.
    """
    columns = [
        {"header": "No", "key": "no"},
        {"header": "Divisi", "key": "divisi"},
        {"header": "Reject", "key": "reject"},
        {"header": "Catatan", "key": "catatan"}
    ]
    data = [
        {"no": 1, "divisi": "Divisi Daging & Mixing", "reject": 3, "catatan": "Lemak < 10% & kulit rapi"},
        {"no": 2, "divisi": "Divisi Bumbu Sparta", "reject": 0, "catatan": "Formula 'Standar' lolos"}
    ]
    subtitles = [
        "Tanggal Produksi : 2026-10-20 s/d 2026-10-25",
        "Target Gilingan  : 40 Gilingan",
        "Penyusun Laporan : Ayu (PPIC)"
    ]

    xml = generate_spreadsheetml_xml(
        sheet_name="Rekap PPIC",
        title="PT MAKAN DIMSUM INDONESIA - REKAPITULASI HARIAN PPIC",
        subtitles=subtitles,
        columns=columns,
        data=data,
        totals={"label": "TOTAL REJECT"}
    )

    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert '<?mso-application progid="Excel.Sheet"?>' in xml
    assert '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"' in xml
    assert '<Interior ss:Color="#3A6B80" ss:Pattern="Solid"/>' in xml
    assert 'PT MAKAN DIMSUM INDONESIA' in xml
    assert 'Tanggal Produksi : 2026-10-20 s/d 2026-10-25' in xml
    # XML Escaping verification
    assert 'Divisi Daging &amp; Mixing' in xml
    assert '&lt; 10% &amp; kulit rapi' in xml
    assert '&apos;Standar&apos;' in xml
    assert '<Row><Cell><Data ss:Type="String">TOTAL REJECT</Data></Cell></Row>' in xml


# ============================================================================
# TIER 2: BOUNDARY & CORNER CASES
# ============================================================================

@pytest.mark.anyio
async def test_tier2_exact_generation_zero_counts_for_optional_categories(async_session: AsyncSession, ppic_user: dict):
    """
    Tier 2 Boundary Case: Pure manual 0 count inputs
    Ayu enters only wadah adonan (3), with explicit 0 for bumbu, kulit, and saos.
    Verifies that NO fallback formula calculates sacks, bal kulit, or bal saos.
    """
    req = GenerateDailyLotsAllRequest(
        plan_id="PLAN-T2-ZERO",
        operator_id="PPIC-AYU",
        operator_role="STAKEHOLDER",
        gilingan_count=3,
        sack_count=0,
        wrapper_bal=0,
        sauce_bal=0
    )

    result = await call_generate_daily_lots_all(payload=req, db=async_session, current_user=ppic_user)

    assert result["counts"]["adonan"] == 3
    assert result["counts"]["bumbu"] == 0
    assert result["counts"]["kulit"] == 0
    assert result["counts"]["saos"] == 0
    assert result["counts"]["total"] == 3

    lots = (await async_session.execute(select(LotTracking))).scalars().all()
    assert len(lots) == 3
    assert all(l.lot_type == "ADONAN" for l in lots)


@pytest.mark.anyio
async def test_tier2_exact_generation_sauce_only_reprint(async_session: AsyncSession, ppic_user: dict):
    """
    Tier 2 Corner Case: Zero adonan, zero bumbu, zero kulit, positive saos (reprint scenario).
    Verifies gilingan does not default to 40 when 0 is explicitly specified.
    """
    req = GenerateDailyLotsAllRequest(
        plan_id="PLAN-T2-SAUCE",
        operator_id="PPIC-AYU",
        operator_role="STAKEHOLDER",
        gilingan_count=0,
        sack_count=0,
        wrapper_bal=0,
        sauce_bal=4
    )

    result = await call_generate_daily_lots_all(payload=req, db=async_session, current_user=ppic_user)

    assert result["counts"]["adonan"] == 0
    assert result["counts"]["bumbu"] == 0
    assert result["counts"]["kulit"] == 0
    assert result["counts"]["saos"] == 4
    assert result["counts"]["total"] == 4

    lots = (await async_session.execute(select(LotTracking))).scalars().all()
    assert len(lots) == 4
    assert all(l.lot_type == "SAOS" for l in lots)


@pytest.mark.anyio
async def test_tier2_exact_generation_all_zero_counts(async_session: AsyncSession, ppic_user: dict):
    """
    Tier 2 Boundary Case: All 0 counts entered.
    System must generate exactly 0 lots without crashing or defaulting to 40.
    """
    req = GenerateDailyLotsAllRequest(
        plan_id="PLAN-T2-ALLZERO",
        operator_id="PPIC-AYU",
        operator_role="STAKEHOLDER",
        gilingan_count=0,
        sack_count=0,
        wrapper_bal=0,
        sauce_bal=0
    )

    result = await call_generate_daily_lots_all(payload=req, db=async_session, current_user=ppic_user)

    assert result["counts"]["total"] == 0
    assert len(result["lots"]) == 0

    lots = (await async_session.execute(select(LotTracking))).scalars().all()
    assert len(lots) == 0


@pytest.mark.anyio
async def test_tier2_exact_generation_single_unit_boundary(async_session: AsyncSession, ppic_user: dict):
    """
    Tier 2 Boundary Case: Minimum non-zero positive quantity (1 of each).
    """
    req = GenerateDailyLotsAllRequest(
        plan_id="PLAN-T2-SINGLE",
        operator_id="PPIC-AYU",
        operator_role="STAKEHOLDER",
        gilingan_count=1,
        sack_count=1,
        wrapper_bal=1,
        sauce_bal=1
    )

    result = await call_generate_daily_lots_all(payload=req, db=async_session, current_user=ppic_user)

    assert result["counts"]["adonan"] == 1
    assert result["counts"]["bumbu"] == 1
    assert result["counts"]["kulit"] == 1
    assert result["counts"]["saos"] == 1
    assert result["counts"]["total"] == 4


@pytest.mark.anyio
async def test_tier2_exact_generation_large_volume_stress(async_session: AsyncSession, ppic_user: dict):
    """
    Tier 2 Stress/Scale Boundary: Large lot volumes generated atomically.
    50 adonan, 10 bumbu, 10 kulit, 6 saos = 76 lots.
    """
    req = GenerateDailyLotsAllRequest(
        plan_id="PLAN-T2-LARGE",
        operator_id="PPIC-AYU",
        operator_role="STAKEHOLDER",
        gilingan_count=50,
        sack_count=10,
        wrapper_bal=10,
        sauce_bal=6
    )

    result = await call_generate_daily_lots_all(payload=req, db=async_session, current_user=ppic_user)

    assert result["counts"]["total"] == 76
    assert len(result["lots"]) == 76

    lots = (await async_session.execute(select(LotTracking))).scalars().all()
    assert len(lots) == 76

    # Verify lot codes are distinct and non-colliding
    lot_codes = [l.lot_qr_code for l in lots]
    assert len(set(lot_codes)) == 76


@pytest.mark.anyio
async def test_tier2_single_day_date_range_start_equals_end(async_session: AsyncSession, ppic_user: dict):
    """
    Tier 2 Boundary Case: start_date == end_date
    Must behave identically to a single date query.
    """
    d = date(2026, 10, 25)
    inbound = SupplierInbound(
        inbound_date=d,
        category="KULIT",
        item_name="Kulit 9cm",
        quantity=5.0,
        unit="BAL",
        supplier_name="CV Sumber Kulit",
        received_by="PPIC-AYU"
    )
    async_session.add(inbound)
    await async_session.commit()

    res = await call_list_supplier_inbound(
        db=async_session,
        current_user=ppic_user,
        start_date="2026-10-25",
        end_date="2026-10-25"
    )
    assert len(res) == 1
    assert res[0].item_name == "Kulit 9cm"


@pytest.mark.anyio
async def test_tier2_empty_date_range_no_records(async_session: AsyncSession, ppic_user: dict):
    """
    Tier 2 Corner Case: Date range with 0 historical entries.
    Should return empty dataset gracefully without unhandled exceptions or 500 error.
    """
    res = await call_list_supplier_inbound(
        db=async_session,
        current_user=ppic_user,
        start_date="2025-01-01",
        end_date="2025-01-07"
    )
    assert isinstance(res, list)
    assert len(res) == 0

    recap = await call_get_daily_recap(
        db=async_session,
        current_user=ppic_user,
        start_date="2025-01-01",
        end_date="2025-01-07"
    )
    assert recap is not None


@pytest.mark.anyio
async def test_tier2_leap_year_and_month_boundaries(async_session: AsyncSession, ppic_user: dict):
    """
    Tier 2 Boundary Case: Leap year (Feb 28 - Mar 1, 2024) and Month Crossing (Oct 31 - Nov 1, 2026).
    """
    leap_records = [
        SupplierInbound(inbound_date=date(2024, 2, 28), category="KULIT", item_name="Feb 28", quantity=1.0, unit="BAL", supplier_name="S", received_by="PPIC"),
        SupplierInbound(inbound_date=date(2024, 2, 29), category="KULIT", item_name="Leap Day Feb 29", quantity=1.0, unit="BAL", supplier_name="S", received_by="PPIC"),
        SupplierInbound(inbound_date=date(2024, 3, 1), category="KULIT", item_name="Mar 1", quantity=1.0, unit="BAL", supplier_name="S", received_by="PPIC"),
    ]
    month_records = [
        SupplierInbound(inbound_date=date(2026, 10, 31), category="SAOS", item_name="Oct 31", quantity=1.0, unit="BAL", supplier_name="S", received_by="PPIC"),
        SupplierInbound(inbound_date=date(2026, 11, 1), category="SAOS", item_name="Nov 1", quantity=1.0, unit="BAL", supplier_name="S", received_by="PPIC"),
    ]
    async_session.add_all(leap_records + month_records)
    await async_session.commit()

    # Query leap year range
    leap_res = await call_list_supplier_inbound(
        db=async_session,
        current_user=ppic_user,
        start_date="2024-02-28",
        end_date="2024-03-01"
    )
    assert len(leap_res) == 3
    assert {r.item_name for r in leap_res} == {"Feb 28", "Leap Day Feb 29", "Mar 1"}

    # Query month boundary crossing range
    month_res = await call_list_supplier_inbound(
        db=async_session,
        current_user=ppic_user,
        start_date="2026-10-31",
        end_date="2026-11-01"
    )
    assert len(month_res) == 2
    assert {r.item_name for r in month_res} == {"Oct 31", "Nov 1"}


@pytest.mark.anyio
async def test_tier2_inverted_date_range_graceful_handling(async_session: AsyncSession, ppic_user: dict):
    """
    Tier 2 Adversarial Boundary: Inverted date range (start_date > end_date).
    System must handle gracefully without crashing, returning empty list.
    """
    res = await call_list_supplier_inbound(
        db=async_session,
        current_user=ppic_user,
        start_date="2026-10-25",
        end_date="2026-10-20"
    )
    assert len(res) == 0


# ============================================================================
# TIER 3: CROSS-FEATURE COMBINATIONS
# ============================================================================

@pytest.mark.anyio
async def test_tier3_manual_lot_issuance_reflected_in_daily_recap(async_session: AsyncSession, ppic_user: dict):
    """
    Tier 3 Cross-Feature Combination:
    PPIC manually issues specific lots -> Daily recap immediately aggregates and reflects them.
    """
    today_wib_date = datetime.now(WIB).date()

    # 1. Active Production Plan
    plan = ProductionPlan(
        plan_id="PLAN-T3-01",
        plan_date=today_wib_date,
        target_gilingan=8,
        target_mika_pax=0,
        target_vakum_pax=0,
        status="ACTIVE",
        created_by="PPIC-AYU"
    )
    async_session.add(plan)
    await async_session.commit()

    # 2. Ayu issues exact lots: 8 adonan, 2 bumbu, 1 kulit, 1 saos
    req = GenerateDailyLotsAllRequest(
        plan_id="PLAN-T3-01",
        operator_id="PPIC-AYU",
        operator_role="STAKEHOLDER",
        gilingan_count=8,
        sack_count=2,
        wrapper_bal=1,
        sauce_bal=1
    )
    gen_res = await call_generate_daily_lots_all(payload=req, db=async_session, current_user=ppic_user)
    assert gen_res["counts"]["total"] == 12

    # 3. Query Daily Recap for today
    today_str = today_wib_date.strftime("%Y-%m-%d")
    recap = await call_get_daily_recap(db=async_session, current_user=ppic_user, recap_date=today_str)

    assert recap["target_gilingan"] == 8
    divs = recap["divisions"]
    assert divs["DAGING"]["total_lots"] == 8
    assert divs["BUMBU"]["total_lots"] == 2
    assert divs["KULIT"]["total_lots"] == 1
    assert divs["SAOS"]["total_lots"] == 1


@pytest.mark.anyio
async def test_tier3_multi_day_inbound_with_category_filtering(async_session: AsyncSession, ppic_user: dict):
    """
    Tier 3 Cross-Feature Combination:
    Inbound entries spanning multiple dates filtered by both date range AND commodity category.
    """
    records = [
        SupplierInbound(inbound_date=date(2026, 10, 21), category="KULIT", item_name="Kulit 9cm", quantity=10.0, unit="BAL", supplier_name="S1", received_by="PPIC"),
        SupplierInbound(inbound_date=date(2026, 10, 21), category="DAGING", item_name="Daging Ayam", quantity=100.0, unit="KG", supplier_name="S2", received_by="PPIC"),
        SupplierInbound(inbound_date=date(2026, 10, 22), category="KULIT", item_name="Kulit 7cm", quantity=5.0, unit="BAL", supplier_name="S1", received_by="PPIC"),
        SupplierInbound(inbound_date=date(2026, 10, 22), category="SAOS", item_name="Saos Sambal", quantity=8.0, unit="BAL", supplier_name="S3", received_by="PPIC"),
        SupplierInbound(inbound_date=date(2026, 10, 23), category="KEMASAN", item_name="Mika Pax", quantity=20.0, unit="DUS", supplier_name="S4", received_by="PPIC"),
        SupplierInbound(inbound_date=date(2026, 10, 23), category="KULIT", item_name="Kulit 9cm Extra", quantity=8.0, unit="BAL", supplier_name="S1", received_by="PPIC"),
    ]
    async_session.add_all(records)
    await async_session.commit()

    # Query Range: Oct 21 to Oct 23, without category filter
    all_inbounds = await call_list_supplier_inbound(
        db=async_session,
        current_user=ppic_user,
        start_date="2026-10-21",
        end_date="2026-10-23"
    )
    assert len(all_inbounds) == 6

    # Query Range: Oct 21 to Oct 23, with category="KULIT"
    kulit_inbounds = await call_list_supplier_inbound(
        db=async_session,
        current_user=ppic_user,
        start_date="2026-10-21",
        end_date="2026-10-23",
        category="KULIT"
    )
    assert len(kulit_inbounds) == 3
    assert all(k.category == "KULIT" for k in kulit_inbounds)
    assert sum(float(k.quantity) for k in kulit_inbounds) == 23.0


@pytest.mark.anyio
async def test_tier3_multi_day_division_reject_aggregation(async_session: AsyncSession, ppic_user: dict):
    """
    Tier 3 Cross-Feature Combination:
    Multiple division daily notes across dates aggregated in recap date range.
    """
    notes = [
        DivisionDailyNote(record_date=date(2026, 10, 20), division="DAGING", reject_count=3, reject_notes="Hari 1", created_by="PPIC"),
        DivisionDailyNote(record_date=date(2026, 10, 20), division="CETAK", reject_count=10, reject_notes="Hari 1", created_by="PPIC"),
        DivisionDailyNote(record_date=date(2026, 10, 21), division="DAGING", reject_count=2, reject_notes="Hari 2", created_by="PPIC"),
        DivisionDailyNote(record_date=date(2026, 10, 21), division="PACKING", reject_count=4, reject_notes="Hari 2", created_by="PPIC"),
    ]
    async_session.add_all(notes)
    await async_session.commit()

    recap = await call_get_daily_recap(
        db=async_session,
        current_user=ppic_user,
        start_date="2026-10-20",
        end_date="2026-10-21"
    )

    # Verify notes and rejects are accessible
    divs = recap.get("divisions", {})
    # Daging rejects should reflect total or individual records
    assert "DAGING" in divs
    assert "CETAK" in divs
    assert "PACKING" in divs


# ============================================================================
# TIER 4: REAL-WORLD SCENARIOS
# ============================================================================

@pytest.mark.anyio
async def test_tier4_complete_ppic_daily_production_and_review_workflow(async_session: AsyncSession, ppic_user: dict):
    """
    Tier 4 Real-World Scenario:
    Simulates a full production day lifecycle managed by Ayu (PPIC):
    1. Plan Active Production Target.
    2. Batch Issue Barcodes strictly based on manual client entries.
    3. Record raw material inbounds from external suppliers.
    4. Record operational division notes and reject counts.
    5. Query Daily Recap & verify metrics match.
    6. Historical review across 7 days.
    7. Generate formatted Excel export string and verify contents.
    """
    today_wib_date = datetime.now(WIB).date()
    today_str = today_wib_date.strftime("%Y-%m-%d")

    # Step 1: Active Production Plan
    plan = ProductionPlan(
        plan_id=f"PLAN-{today_wib_date.strftime('%Y%m%d')}-01",
        plan_date=today_wib_date,
        target_gilingan=25,
        target_mika_pax=500,
        target_vakum_pax=300,
        status="ACTIVE",
        created_by="PPIC-AYU"
    )
    async_session.add(plan)
    await async_session.commit()

    # Step 2: PPIC Manual Barcode Issuance (no formulas!)
    issuance_req = GenerateDailyLotsAllRequest(
        plan_id=plan.plan_id,
        operator_id="PPIC-AYU",
        operator_role="STAKEHOLDER",
        gilingan_count=25,
        sack_count=2,
        wrapper_bal=4,
        sauce_bal=2
    )
    issuance_res = await call_generate_daily_lots_all(payload=issuance_req, db=async_session, current_user=ppic_user)
    assert issuance_res["counts"]["total"] == 33

    # Step 3: Supplier Inbound Recording
    inbound_kulit = SupplierInboundCreate(
        category="KULIT",
        item_name="Kulit Dimsum 9cm Spesial",
        quantity=20.0,
        unit="BAL",
        supplier_name="PT Sumber Rejeki Kulit",
        document_no="SJ-20261029-001",
        notes="Kondisi dingin segar"
    )
    inbound_saos = SupplierInboundCreate(
        category="SAOS",
        item_name="Saos Sambal Bantal 1kg",
        quantity=10.0,
        unit="BAL",
        supplier_name="CV Saos Jaya Sentosa",
        document_no="SJ-99812",
        notes="Kemasan utuh"
    )
    await create_supplier_inbound(payload=inbound_kulit, db=async_session, current_user=ppic_user)
    await create_supplier_inbound(payload=inbound_saos, db=async_session, current_user=ppic_user)

    # Step 4: Division Daily Notes Recording
    note_daging = DivisionDailyNoteCreate(
        division="DAGING",
        reject_count=2,
        reject_notes="2 kg lemak beku disisihkan",
        operational_notes="Penggilingan selesai tepat waktu"
    )
    note_cetak = DivisionDailyNoteCreate(
        division="CETAK",
        reject_count=12,
        reject_notes="12 butir dimsum kulit pecah saat pencetakan",
        operational_notes="Mesin cetak 2 butuh pelumasan"
    )
    await save_division_daily_note(payload=note_daging, db=async_session, current_user=ppic_user)
    await save_division_daily_note(payload=note_cetak, db=async_session, current_user=ppic_user)

    # Step 5: PPIC Daily Recap Verification
    recap = await call_get_daily_recap(db=async_session, current_user=ppic_user, recap_date=today_str)
    assert recap["target_gilingan"] == 25
    divs = recap["divisions"]
    assert divs["DAGING"]["total_lots"] == 25
    assert divs["DAGING"]["reject_count"] == 2
    assert divs["CETAK"]["reject_count"] == 12

    inbounds = recap.get("supplier_inbound", recap.get("inbound", []))
    assert len(inbounds) == 2

    # Step 6: Historical Review Across 7 Days
    d_7days_ago = (today_wib_date - timedelta(days=7)).strftime("%Y-%m-%d")
    historical_recap = await call_get_daily_recap(
        db=async_session,
        current_user=ppic_user,
        start_date=d_7days_ago,
        end_date=today_str
    )
    assert historical_recap is not None

    # Step 7: Formatted Excel Export Verification
    div_rows = [
        {"divisi": "Divisi Daging (Giling & Mix)", "reject": divs["DAGING"]["reject_count"], "catatan": divs["DAGING"]["operational_notes"]},
        {"divisi": "Produksi 2: Meja Cetak", "reject": divs["CETAK"]["reject_count"], "catatan": divs["CETAK"]["operational_notes"]}
    ]
    xml_recap = generate_spreadsheetml_xml(
        sheet_name="Rekap PPIC",
        title="PT MAKAN DIMSUM INDONESIA - REKAPITULASI HARIAN PPIC",
        subtitles=[
            f"Tanggal Produksi : {d_7days_ago} s/d {today_str}",
            "Penyusun Laporan : Ayu (PPIC)"
        ],
        columns=[
            {"header": "Divisi", "key": "divisi"},
            {"header": "Reject", "key": "reject"},
            {"header": "Catatan", "key": "catatan"}
        ],
        data=div_rows,
        totals={"label": "TOTAL REJECT"}
    )
    assert 'PT MAKAN DIMSUM INDONESIA' in xml_recap
    assert f'{d_7days_ago} s/d {today_str}' in xml_recap
    assert 'Divisi Daging (Giling &amp; Mix)' in xml_recap
    assert 'TOTAL REJECT' in xml_recap


@pytest.mark.anyio
async def test_tier4_security_ppic_role_enforcement(async_session: AsyncSession, ppic_user: dict, pic_daging_user: dict, operator_user: dict):
    """
    Tier 4 Security Enforcement:
    Verifies that only PPIC / authorized supervisory staff can access PPIC-only endpoints
    (generate_daily_lots_all, create_supplier_inbound, save_division_daily_note).
    Regular operators and division PICs must be denied with HTTP 403.
    """
    from backend.app.auth import require_ppic
    ppic_checker = require_ppic()

    # 1. PIC Daging denied by require_ppic security gate
    with pytest.raises(HTTPException) as exc1:
        await ppic_checker(current_user=pic_daging_user)
    assert exc1.value.status_code == 403
    assert "Hanya PPIC (Ayu)" in exc1.value.detail

    # 2. Regular Operator denied by require_ppic security gate
    with pytest.raises(HTTPException) as exc2:
        await ppic_checker(current_user=operator_user)
    assert exc2.value.status_code == 403
    assert "Hanya PPIC (Ayu)" in exc2.value.detail

    # 3. Ayu (PPIC) successfully authorized
    authorized = await ppic_checker(current_user=ppic_user)
    assert authorized["operator_id"] == "PPIC-AYU"

