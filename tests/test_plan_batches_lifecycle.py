import sys
import os
import uuid
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pytest
from httpx import AsyncClient, ASGITransport
from datetime import date, datetime
from backend.app.main import app, on_startup
from backend.app.auth import create_token, ACCOUNTS


@pytest.fixture(autouse=True)
async def setup_db():
    await on_startup()


@pytest.fixture
def ppic_headers():
    token = create_token(ACCOUNTS["ppic_ayu"])
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def meat_operator_headers():
    token = create_token(ACCOUNTS["opr_daging"])
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.anyio
async def test_batch1_publishing_and_idempotency(ppic_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        plan_id = f"PLAN-TEST-IDEMP-{uuid.uuid4().hex[:8]}"
        idemp_key = f"key-test-{uuid.uuid4().hex[:8]}"

        # 1. Terbitkan Batch 1
        res1 = await ac.post(
            "/api/v1/production1/generate-daily-lots-all",
            json={
                "plan_id": plan_id,
                "idempotency_key": idemp_key,
                "gilingan_count": 2,
                "sack_count": 1,
                "wrapper_bal": 1,
                "sauce_bal": 1
            },
            headers=ppic_headers
        )
        assert res1.status_code == 201
        data1 = res1.json()
        assert data1["status"] == "success"
        assert data1["batch_no"] == 1
        assert len(data1["lots"]) == 5

        # 2. Klik ganda dengan idempotency key yang sama -> mengembalikan hasil yang sama, tidak membuat lot baru
        res2 = await ac.post(
            "/api/v1/production1/generate-daily-lots-all",
            json={
                "plan_id": plan_id,
                "idempotency_key": idemp_key,
                "gilingan_count": 2,
                "sack_count": 1,
                "wrapper_bal": 1,
                "sauce_bal": 1
            },
            headers=ppic_headers
        )
        assert res2.status_code == 201
        data2 = res2.json()
        assert data2.get("is_idempotent_replay") is True
        assert len(data2["lots"]) == 5
        assert [l["lot_qr_code"] for l in data1["lots"]] == [l["lot_qr_code"] for l in data2["lots"]]

        # 3. Percobaan menerbitkan Batch 1 baru tanpa key atau key berbeda pada hari yang sama -> Ditolak 409
        res3 = await ac.post(
            "/api/v1/production1/generate-daily-lots-all",
            json={
                "plan_id": plan_id,
                "idempotency_key": f"diff-{uuid.uuid4().hex[:8]}",
                "gilingan_count": 2
            },
            headers=ppic_headers
        )
        assert res3.status_code == 409
        assert "Batch 1 sudah diterbitkan" in res3.json()["detail"]


@pytest.mark.anyio
async def test_additional_production_batch_delta_calculation(ppic_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        plan_id = f"PLAN-TEST-DELTA-{uuid.uuid4().hex[:8]}"

        # 1. Draft awal dan terbitkan Batch 1 (misal 5 gilingan = ~100 pack)
        res_b1 = await ac.post(
            "/api/v1/production1/generate-daily-lots-all",
            json={
                "plan_id": plan_id,
                "idempotency_key": f"b1-{plan_id}",
                "gilingan_count": 5,
                "sack_count": 1,
                "wrapper_bal": 1,
                "sauce_bal": 1
            },
            headers=ppic_headers
        )
        assert res_b1.status_code == 201

        # 2. Pratinjau Tambah Produksi (Tambahan kecil yang tidak butuh karung bumbu baru)
        prev_res = await ac.post(
            "/api/v1/production1/plans/batches/preview",
            json={
                "plan_id": plan_id,
                "manual_add_gilingan": 2,
                "manual_add_bumbu": 0,
                "manual_add_kulit": 0,
                "manual_add_saos": 0
            },
            headers=ppic_headers
        )
        assert prev_res.status_code == 200
        prev_data = prev_res.json()
        assert prev_data["next_batch_no"] == 2
        assert prev_data["delta"]["adonan"] == 2
        assert prev_data["delta"]["bumbu"] == 0
        assert prev_data["delta"]["total"] == 2

        # 3. Terbitkan Batch 2 tambahan
        add_res = await ac.post(
            "/api/v1/production1/plans/batches/add",
            json={
                "plan_id": plan_id,
                "idempotency_key": f"b2-{plan_id}",
                "reason": "Pesanan mendadak",
                "confirmed_delta_gilingan": 2,
                "confirmed_delta_bumbu": 0,
                "confirmed_delta_kulit": 0,
                "confirmed_delta_saos": 0
            },
            headers=ppic_headers
        )
        assert add_res.status_code == 201
        add_data = add_res.json()
        assert add_data["status"] == "success"
        assert add_data["batch_no"] == 2
        assert len(add_data["lots"]) == 2

        # 4. Tambahan Batch 3 yang melewati ambang batas karung bumbu
        prev_res_b3 = await ac.post(
            "/api/v1/production1/plans/batches/preview",
            json={
                "plan_id": plan_id,
                "manual_add_gilingan": 15
            },
            headers=ppic_headers
        )
        assert prev_res_b3.status_code == 200
        prev_b3 = prev_res_b3.json()
        assert prev_b3["next_batch_no"] == 3
        assert prev_b3["delta"]["adonan"] == 15
        assert prev_b3["delta"]["bumbu"] == 1


@pytest.mark.anyio
async def test_void_batch_validation_and_scanner_rejection(ppic_headers, meat_operator_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        plan_id = f"PLAN-TEST-VOID-{uuid.uuid4().hex[:8]}"

        # 1. Terbitkan Batch 1
        b1_res = await ac.post(
            "/api/v1/production1/generate-daily-lots-all",
            json={
                "plan_id": plan_id,
                "idempotency_key": f"b1-{plan_id}",
                "gilingan_count": 2,
                "sack_count": 1,
                "wrapper_bal": 0,
                "sauce_bal": 0
            },
            headers=ppic_headers
        )
        assert b1_res.status_code == 201
        lots = b1_res.json()["lots"]
        adonan_lot = lots[0]["lot_qr_code"]

        # 2. Batalkan Batch 1 (karena belum ada yang di-scan)
        void_res = await ac.post(
            f"/api/v1/production1/plans/batches/1/void?plan_id={plan_id}",
            headers=ppic_headers
        )
        assert void_res.status_code == 200
        assert void_res.json()["status"] == "success"

        # 3. Scanner mencoba memproses barcode yang sudah VOID -> Ditolak 400
        scan_res = await ac.post(
            "/api/v1/scan/process",
            json={
                "lot_code": adonan_lot,
                "station_id": "MEAT_STATION",
                "intent": "SCAN_OUT"
            },
            headers=meat_operator_headers
        )
        assert scan_res.status_code == 400
        assert "VOID" in scan_res.json()["detail"]


@pytest.mark.anyio
async def test_void_batch_blocked_if_any_lot_scanned(ppic_headers, meat_operator_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        plan_id = f"PLAN-TEST-BLOCKED-{uuid.uuid4().hex[:8]}"

        # 1. Terbitkan Batch 1
        b1_res = await ac.post(
            "/api/v1/production1/generate-daily-lots-all",
            json={
                "plan_id": plan_id,
                "idempotency_key": f"b1-{plan_id}",
                "gilingan_count": 2,
                "sack_count": 0,
                "wrapper_bal": 0,
                "sauce_bal": 0
            },
            headers=ppic_headers
        )
        assert b1_res.status_code == 201
        lots = b1_res.json()["lots"]
        adonan_lot = lots[0]["lot_qr_code"]

        # 2. Operator Daging men-scan out wadah adonan
        scan_res = await ac.post(
            "/api/v1/scan/process",
            json={
                "lot_code": adonan_lot,
                "station_id": "MEAT_STATION",
                "intent": "SCAN_OUT"
            },
            headers=meat_operator_headers
        )
        assert scan_res.status_code == 200

        # 3. PPIC mencoba membatalkan Batch 1 -> Ditolak 409 karena sudah ada yang di-scan!
        void_res = await ac.post(
            f"/api/v1/production1/plans/batches/1/void?plan_id={plan_id}",
            headers=ppic_headers
        )
        assert void_res.status_code == 409
        assert "sudah di-scan" in void_res.json()["detail"]


@pytest.mark.anyio
async def test_republish_voided_batch1(ppic_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        plan_id = f"PLAN-TEST-REPUBLISH-{uuid.uuid4().hex[:8]}"

        # 1. Terbitkan Batch 1
        res1 = await ac.post(
            "/api/v1/production1/generate-daily-lots-all",
            json={
                "plan_id": plan_id,
                "idempotency_key": f"key1-{plan_id}",
                "gilingan_count": 2,
                "sack_count": 1,
                "wrapper_bal": 0,
                "sauce_bal": 0
            },
            headers=ppic_headers
        )
        assert res1.status_code == 201

        # 2. Batalkan Batch 1 sebelum di-scan
        void_res = await ac.post(
            f"/api/v1/production1/plans/batches/1/void?plan_id={plan_id}",
            headers=ppic_headers
        )
        assert void_res.status_code == 200

        # 3. Terbitkan ulang Batch 1 dengan kuantitas baru -> Berhasil update in place, tidak UNIQUE constraint failed
        res2 = await ac.post(
            "/api/v1/production1/generate-daily-lots-all",
            json={
                "plan_id": plan_id,
                "idempotency_key": f"key2-{plan_id}",
                "gilingan_count": 3,
                "sack_count": 1,
                "wrapper_bal": 0,
                "sauce_bal": 0
            },
            headers=ppic_headers
        )
        assert res2.status_code == 201
        data2 = res2.json()
        assert data2["status"] == "success"
        assert data2["batch_no"] == 1
        assert len(data2["lots"]) == 4
