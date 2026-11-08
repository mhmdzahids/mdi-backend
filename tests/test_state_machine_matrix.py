import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pytest
from datetime import datetime
from sqlalchemy import update, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from fastapi import HTTPException

from backend.app.models import Base, LotTracking, ScanSetting, ScanEvent, ProductionPlan
from backend.app.services.scan_engine import (
    execute_scan_transition,
    get_effective_scan_mode,
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
        # Seed default plan
        plan = ProductionPlan(
            plan_id="PLAN-TEST-SM",
            plan_date=datetime.utcnow().date(),
            target_gilingan=10,
            target_mika_pax=0,
            target_vakum_pax=0,
            status="ACTIVE",
            created_by="TEST"
        )
        session.add(plan)
        # Seed default scan settings
        session.add(ScanSetting(station_id="GLOBAL", scan_mode="1X", updated_by="TEST"))
        session.add(ScanSetting(station_id="MEAT_STATION", scan_mode="1X", updated_by="TEST"))
        session.add(ScanSetting(station_id="SEASONING_STATION", scan_mode="2X", updated_by="TEST"))
        await session.commit()
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.mark.anyio
async def test_mode_2x_scan_out_without_scan_in_rejected(async_session: AsyncSession):
    """Mode 2X: Scan Out pada barang Belum discan (DICETAK) WAJIB ditolak."""
    # Bumbu di-set mode 2X
    lot = LotTracking(
        lot_qr_code="BM-20261025-001",
        lot_type="BUMBU",
        current_status="PRINTED_PENDING_STOCK_IN",
        current_station="SEASONING_STATION",
        quantity=20,
        unit="PORSI"
    )
    async_session.add(lot)
    await async_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await execute_scan_transition(
            db=async_session,
            lot_code="BM-20261025-001",
            operator_station_id="SEASONING_STATION",
            user_id="OPR-BUMBU",
            user_role="OPERATOR_BUMBU",
            intent="SCAN_OUT"
        )
    assert exc_info.value.status_code == 400
    assert "Belum scan in" in exc_info.value.detail


@pytest.mark.anyio
async def test_mode_2x_full_happy_path(async_session: AsyncSession):
    """Mode 2X: Belum discan -> Scan In (READY) -> Scan Out (IN_TRANSIT) -> Terima di Tujuan."""
    lot = LotTracking(
        lot_qr_code="BM-20261025-002",
        lot_type="BUMBU",
        current_status="PRINTED_PENDING_STOCK_IN",
        current_station="SEASONING_STATION",
        quantity=20,
        unit="PORSI"
    )
    async_session.add(lot)
    await async_session.commit()

    # 1. Scan In di Bumbu (Mode 2X)
    res_in = await execute_scan_transition(
        db=async_session,
        lot_code="BM-20261025-002",
        operator_station_id="SEASONING_STATION",
        user_id="OPR-BUMBU",
        user_role="OPERATOR_BUMBU",
        intent="SCAN_IN"
    )
    assert res_in["success"] is True
    assert res_in["normalized_status"] == "DI_STOK"
    assert res_in["current_status"] == "READY_IN_SEASONING_STATION"

    # 2. Idempoten: Scan In ulang pada status yang sama -> info no-op
    res_in_repeat = await execute_scan_transition(
        db=async_session,
        lot_code="BM-20261025-002",
        operator_station_id="SEASONING_STATION",
        user_id="OPR-BUMBU",
        user_role="OPERATOR_BUMBU",
        intent="SCAN_IN"
    )
    assert res_in_repeat["success"] is True
    assert res_in_repeat["is_duplicate"] is True
    assert "Sudah scan in" in res_in_repeat["message"]

    # 3. Scan Out dari Bumbu ke Daging (Mixer)
    res_out = await execute_scan_transition(
        db=async_session,
        lot_code="BM-20261025-002",
        operator_station_id="SEASONING_STATION",
        user_id="OPR-BUMBU",
        user_role="OPERATOR_BUMBU",
        intent="SCAN_OUT"
    )
    assert res_out["success"] is True
    assert res_out["normalized_status"] == "KELUAR"
    assert res_out["current_status"] == "IN_TRANSIT_TO_MEAT_MIXING"

    # 4. Idempoten: Scan Out ulang -> info no-op
    res_out_repeat = await execute_scan_transition(
        db=async_session,
        lot_code="BM-20261025-002",
        operator_station_id="SEASONING_STATION",
        user_id="OPR-BUMBU",
        user_role="OPERATOR_BUMBU",
        intent="SCAN_OUT"
    )
    assert res_out_repeat["success"] is True
    assert res_out_repeat["is_duplicate"] is True
    assert "Sudah scan out" in res_out_repeat["message"]

    # 5. Daging (Meja Mixer) melakukan Scan In penerimaan
    res_recv = await execute_scan_transition(
        db=async_session,
        lot_code="BM-20261025-002",
        operator_station_id="MEAT_STATION",
        user_id="OPR-DAGING",
        user_role="OPERATOR_DAGING",
        intent="SCAN_IN"
    )
    assert res_recv["success"] is True
    assert res_recv["normalized_status"] == "DITERIMA"
    assert res_recv["current_status"] == "IN_MEAT_MIXING_STATION"


@pytest.mark.anyio
async def test_mode_1x_scan_out_directly_from_printed(async_session: AsyncSession):
    """Mode 1X: Barang Belum discan boleh langsung Scan Out ke transit."""
    # MEAT_STATION disetel 1X
    lot = LotTracking(
        lot_qr_code="AD-20261025-001",
        lot_type="ADONAN",
        current_status="PRINTED_PENDING_STOCK_IN",
        current_station="MEAT_STATION",
        quantity=19,
        unit="KG"
    )
    async_session.add(lot)
    await async_session.commit()

    # 1. Coba Scan In di Mode 1x untuk barang sendiri -> info bahwa scan in tidak wajib
    res_in = await execute_scan_transition(
        db=async_session,
        lot_code="AD-20261025-001",
        operator_station_id="MEAT_STATION",
        user_id="OPR-DAGING",
        user_role="OPERATOR_DAGING",
        intent="SCAN_IN"
    )
    assert res_in["success"] is True
    assert "Mode 1x aktif" in res_in["message"]
    # Status tetap belum discan
    assert res_in["normalized_status"] == "DICETAK"

    # 2. Scan Out langsung -> Berhasil ke transit
    res_out = await execute_scan_transition(
        db=async_session,
        lot_code="AD-20261025-001",
        operator_station_id="MEAT_STATION",
        user_id="OPR-DAGING",
        user_role="OPERATOR_DAGING",
        intent="SCAN_OUT"
    )
    assert res_out["success"] is True
    assert res_out["normalized_status"] == "KELUAR"
    assert res_out["current_status"] == "IN_TRANSIT_TO_ASSEMBLY"


@pytest.mark.anyio
async def test_midday_mode_switch_safety(async_session: AsyncSession):
    """Pergantian mode di tengah hari aman untuk barang yang sudah scan in."""
    # 1. Buat lot di mode 2X, lalu lakukan scan in
    lot = LotTracking(
        lot_qr_code="KL-20261025-001",
        lot_type="KULIT",
        current_status="PRINTED_PENDING_STOCK_IN",
        current_station="WRAPPER_STATION",
        quantity=1,
        unit="BAL"
    )
    setting = ScanSetting(station_id="WRAPPER_STATION", scan_mode="2X", updated_by="TEST")
    async_session.add_all([lot, setting])
    await async_session.commit()

    # Scan In di mode 2X
    res_in = await execute_scan_transition(
        db=async_session,
        lot_code="KL-20261025-001",
        operator_station_id="WRAPPER_STATION",
        user_id="OPR-KULIT",
        user_role="OPERATOR_KULIT",
        intent="SCAN_IN"
    )
    assert res_in["success"] is True
    assert res_in["normalized_status"] == "DI_STOK"

    # 2. PIC mengubah mode ke 1X di tengah hari
    setting.scan_mode = "1X"
    await async_session.commit()

    # 3. Karyawan scan out barang yang sudah di-stok tadi -> Tetap berhasil di Mode 1X!
    res_out = await execute_scan_transition(
        db=async_session,
        lot_code="KL-20261025-001",
        operator_station_id="WRAPPER_STATION",
        user_id="OPR-KULIT",
        user_role="OPERATOR_KULIT",
        intent="SCAN_OUT"
    )
    assert res_out["success"] is True
    assert res_out["normalized_status"] == "KELUAR"
    assert res_out["current_status"] == "IN_TRANSIT_TO_ASSEMBLY"


@pytest.mark.anyio
async def test_division_isolation_and_unknown_qr(async_session: AsyncSession):
    """Validasi QR tidak dikenal dan QR bukan milik divisi."""
    # 1. QR tidak dikenal
    with pytest.raises(HTTPException) as exc_404:
        await execute_scan_transition(
            db=async_session,
            lot_code="UNKNOWN-QR-999",
            operator_station_id="MEAT_STATION",
            user_id="OPR-DAGING",
            user_role="OPERATOR_DAGING",
            intent="SCAN_OUT"
        )
    assert exc_404.value.status_code == 404
    assert "QR tidak dikenal" in exc_404.value.detail

    # 2. Operator Bumbu mencoba scan out adonan daging -> Ditolak bukan milik divisi
    adonan_lot = LotTracking(
        lot_qr_code="AD-20261025-009",
        lot_type="ADONAN",
        current_status="READY_IN_MEAT_MIXING_STATION",
        current_station="MEAT_STATION",
        quantity=19,
        unit="KG"
    )
    async_session.add(adonan_lot)
    await async_session.commit()

    with pytest.raises(HTTPException) as exc_div:
        await execute_scan_transition(
            db=async_session,
            lot_code="AD-20261025-009",
            operator_station_id="SEASONING_STATION", # Bumbu mencoba scan out adonan
            user_id="OPR-BUMBU",
            user_role="OPERATOR_BUMBU",
            intent="SCAN_OUT"
        )
    assert exc_div.value.status_code == 400
    assert "QR bukan milik divisi ini" in exc_div.value.detail


@pytest.mark.anyio
async def test_packing_pic_generation_and_karyawan_verification(async_session: AsyncSession):
    """
    Test bug packing:
    1. PIC menerbitkan QR -> status awal WAJIB READY_IN_ASSEMBLY_STATION (Belum diverifikasi), bukan PACKED_VERIFIED.
    2. Karyawan packing scan verifikasi -> status berubah jadi PACKED_VERIFIED.
    3. Counter 'Selesai hari ini' hanya menghitung yang sudah PACKED_VERIFIED.
    """
    from backend.app.routers.production2 import complete_packing_to_fg, verify_packing
    from backend.app.schemas import CompletePackingToFgRequest, VerifyPackingRequest

    # 1. PIC generate 2 pack mika dan 2 pack vakum
    req = CompletePackingToFgRequest(
        plan_id="PLAN-TEST-SM",
        operator_id="PIC-01",
        operator_role="STAKEHOLDER",
        mika_output_qty=2,
        vakum_output_qty=2,
        reject_pcs=0
    )
    user_pic = {"username": "pic_test", "division": "PACKING", "role": "STAKEHOLDER"}
    fg_lots = await complete_packing_to_fg(payload=req, db=async_session, current_user=user_pic)
    assert len(fg_lots) == 4

    # Pastikan SEMUA berstatus READY_IN_ASSEMBLY_STATION (bukan PACKED_VERIFIED)
    for lot in fg_lots:
        assert lot.current_status == "READY_IN_ASSEMBLY_STATION", f"{lot.lot_qr_code} harus READY_IN_ASSEMBLY_STATION"
        assert normalize_status(lot.current_status) == "DICETAK"

    # 2. Karyawan memindai 1 kemasan
    mika_1 = fg_lots[0].lot_qr_code
    verify_req = VerifyPackingRequest(
        lot_codes=[mika_1],
        operator_id="OPR-PACK-01",
        operator_role="OPERATOR_PACKING",
        notes="Fisik diverifikasi"
    )
    user_opr = {"username": "opr_pack", "division": "PACKING", "role": "OPERATOR"}
    updated = await verify_packing(payload=verify_req, db=async_session, current_user=user_opr)
    assert len(updated) == 1
    assert updated[0].lot_qr_code == mika_1
    assert updated[0].current_status == "PACKED_VERIFIED"
    assert normalize_status(updated[0].current_status) == "DITERIMA"

    # 3. Verifikasi sisa lot masih belum diverifikasi (READY_IN_ASSEMBLY_STATION)
    from sqlalchemy import select
    res = await async_session.execute(select(LotTracking).where(LotTracking.lot_type.in_(["FG_MIKA", "FG_VAKUM"])))
    all_fg = res.scalars().all()
    verified_count = sum(1 for f in all_fg if f.current_status == "PACKED_VERIFIED")
    pending_count = sum(1 for f in all_fg if f.current_status == "READY_IN_ASSEMBLY_STATION")

    assert verified_count == 1, "Hanya 1 lot yang selesai hari ini"
    assert pending_count == 3, "3 lot masih menunggu scan QR"


@pytest.mark.anyio
async def test_meat_station_bumbu_scan_out_rejected_both_modes(async_session: AsyncSession):
    """Bumbu x Scan Out di stasiun Daging/Giling WAJIB ditolak di kedua mode tanpa event & state tidak berubah."""
    from backend.app.models import ScanSetting

    # Mode 1x:
    await async_session.execute(
        update(ScanSetting).where(ScanSetting.station_id == "MEAT_STATION").values(scan_mode="1X")
    )
    bumbu_lot = LotTracking(
        lot_qr_code="SP-20261025-M01",
        lot_type="BUMBU",
        current_status="IN_TRANSIT_TO_MEAT_MIXING",
        current_station="SEASONING_STATION",
        quantity=20,
        unit="KARUNG"
    )
    async_session.add(bumbu_lot)
    await async_session.commit()

    with pytest.raises(HTTPException) as exc_out_1x:
        await execute_scan_transition(
            db=async_session,
            lot_code="SP-20261025-M01",
            operator_station_id="MEAT_STATION",
            user_id="OPR-DAGING",
            user_role="OPERATOR_DAGING",
            intent="SCAN_OUT"
        )
    assert exc_out_1x.value.status_code == 400
    assert "QR bumbu tidak bisa di-scan out di bagian ini" in exc_out_1x.value.detail

    # Mode 2x:
    await async_session.execute(
        update(ScanSetting).where(ScanSetting.station_id == "MEAT_STATION").values(scan_mode="2X")
    )
    await async_session.commit()

    with pytest.raises(HTTPException) as exc_out_2x:
        await execute_scan_transition(
            db=async_session,
            lot_code="SP-20261025-M01",
            operator_station_id="MEAT_STATION",
            user_id="OPR-DAGING",
            user_role="OPERATOR_DAGING",
            intent="SCAN_OUT"
        )
    assert exc_out_2x.value.status_code == 400
    assert "QR bumbu tidak bisa di-scan out di bagian ini" in exc_out_2x.value.detail

    # Pastikan status bumbu sama sekali tidak berubah
    res = await async_session.execute(select(LotTracking).where(LotTracking.lot_qr_code == "SP-20261025-M01"))
    unchanged = res.scalar_one()
    assert unchanged.current_status == "IN_TRANSIT_TO_MEAT_MIXING"


@pytest.mark.anyio
async def test_meat_station_bumbu_scan_in_lifecycle(async_session: AsyncSession):
    """
    Bumbu x Scan In di stasiun Daging:
    1. Belum dikirim dari stasiun Bumbu -> Ditolak
    2. Sedang dalam pengiriman -> Berhasil diterima (IN_MEAT_MIXING_STATION)
    3. Sudah diterima -> Idempoten info 'Sudah diterima', tanpa event baru
    """
    # 1. Belum dikirim
    bumbu_not_sent = LotTracking(
        lot_qr_code="SP-20261025-M02",
        lot_type="BUMBU",
        current_status="READY_IN_SEASONING_STATION",
        current_station="SEASONING_STATION",
        quantity=20,
        unit="KARUNG"
    )
    async_session.add(bumbu_not_sent)
    await async_session.commit()

    with pytest.raises(HTTPException) as exc_not_sent:
        await execute_scan_transition(
            db=async_session,
            lot_code="SP-20261025-M02",
            operator_station_id="MEAT_STATION",
            user_id="OPR-DAGING",
            user_role="OPERATOR_DAGING",
            intent="SCAN_IN"
        )
    assert exc_not_sent.value.status_code == 400
    assert "Bumbu belum dikirim dari bagian Bumbu" in exc_not_sent.value.detail

    # 2. Sedang dalam pengiriman -> Berhasil diterima
    bumbu_transit = LotTracking(
        lot_qr_code="SP-20261025-M03",
        lot_type="BUMBU",
        current_status="IN_TRANSIT_TO_MEAT_MIXING",
        current_station="SEASONING_STATION",
        quantity=20,
        unit="KARUNG"
    )
    async_session.add(bumbu_transit)
    await async_session.commit()

    res_recv = await execute_scan_transition(
        db=async_session,
        lot_code="SP-20261025-M03",
        operator_station_id="MEAT_STATION",
        user_id="OPR-DAGING",
        user_role="OPERATOR_DAGING",
        intent="SCAN_IN"
    )
    assert res_recv["success"] is True
    assert res_recv["action"] == "SCAN_IN"
    assert res_recv["current_status"] == "IN_MEAT_MIXING_STATION"
    assert "Bumbu diterima" in res_recv["message"]

    # 3. Sudah diterima -> Scan in lagi = Idempoten 'Sudah diterima'
    res_idem = await execute_scan_transition(
        db=async_session,
        lot_code="SP-20261025-M03",
        operator_station_id="MEAT_STATION",
        user_id="OPR-DAGING",
        user_role="OPERATOR_DAGING",
        intent="SCAN_IN"
    )
    assert res_idem["success"] is True
    assert res_idem["is_duplicate"] is True
    assert res_idem["message"] == "Sudah diterima"


@pytest.mark.anyio
async def test_meat_station_adonan_scan_in_modes(async_session: AsyncSession):
    """Adonan x Scan In di Giling & Mix: 1x info tidak perlu scan in, 2x masuk stok."""
    from backend.app.models import ScanSetting

    # Mode 1x
    await async_session.execute(
        update(ScanSetting).where(ScanSetting.station_id == "MEAT_STATION").values(scan_mode="1X")
    )
    ad_1 = LotTracking(
        lot_qr_code="AD-20261025-M10",
        lot_type="ADONAN",
        current_status="PRINTED_PENDING_STOCK_IN",
        current_station="MEAT_STATION",
        quantity=20,
        unit="KG"
    )
    async_session.add(ad_1)
    await async_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await execute_scan_transition(
            db=async_session,
            lot_code="AD-20261025-M10",
            operator_station_id="MEAT_STATION",
            user_id="OPR-DAGING",
            user_role="OPERATOR_DAGING",
            intent="SCAN_IN"
        )
    assert exc_info.value.status_code == 400
    assert "tidak bisa di-scan in" in exc_info.value.detail

    # Mode 2x
    await async_session.execute(
        update(ScanSetting).where(ScanSetting.station_id == "MEAT_STATION").values(scan_mode="2X")
    )
    ad_2 = LotTracking(
        lot_qr_code="AD-20261025-M20",
        lot_type="ADONAN",
        current_status="PRINTED_PENDING_STOCK_IN",
        current_station="MEAT_STATION",
        quantity=20,
        unit="KG"
    )
    async_session.add(ad_2)
    await async_session.commit()

    res_2x = await execute_scan_transition(
        db=async_session,
        lot_code="AD-20261025-M20",
        operator_station_id="MEAT_STATION",
        user_id="OPR-DAGING",
        user_role="OPERATOR_DAGING",
        intent="SCAN_IN"
    )
    assert res_2x["success"] is True
    assert res_2x["action"] == "CONFIRM_STOCK"
    assert res_2x["current_status"] == "READY_IN_MEAT_STATION" or res_2x["current_status"] == "READY_IN_MEAT_MIXING_STATION"
    assert "Adonan scan in" in res_2x["message"]


@pytest.mark.anyio
async def test_meat_station_reject_kulit_and_saos(async_session: AsyncSession):
    """QR Kulit dan Saos ditolak di stasiun Daging dengan menyebutkan jenisnya."""
    kulit_lot = LotTracking(
        lot_qr_code="KL-20261025-M99",
        lot_type="KULIT",
        current_status="READY_IN_WRAPPER_STATION",
        current_station="WRAPPER_STATION",
        quantity=1,
        unit="BAL"
    )
    saos_lot = LotTracking(
        lot_qr_code="SS-20261025-M99",
        lot_type="SAOS",
        current_status="READY_IN_SAUCE_STATION",
        current_station="SAUCE_STATION",
        quantity=1,
        unit="BAL"
    )
    async_session.add_all([kulit_lot, saos_lot])
    await async_session.commit()

    # Coba scan in kulit di Meat Station
    with pytest.raises(HTTPException) as exc_kulit:
        await execute_scan_transition(
            db=async_session,
            lot_code="KL-20261025-M99",
            operator_station_id="MEAT_STATION",
            user_id="OPR-DAGING",
            user_role="OPERATOR_DAGING",
            intent="SCAN_IN"
        )
    assert exc_kulit.value.status_code == 400
    assert "jenis: Kulit" in exc_kulit.value.detail

    # Coba scan out saos di Meat Station
    with pytest.raises(HTTPException) as exc_saos:
        await execute_scan_transition(
            db=async_session,
            lot_code="SS-20261025-M99",
            operator_station_id="MEAT_STATION",
            user_id="OPR-DAGING",
            user_role="OPERATOR_DAGING",
            intent="SCAN_OUT"
        )
    assert exc_saos.value.status_code == 400
    assert "jenis: Saos" in exc_saos.value.detail
