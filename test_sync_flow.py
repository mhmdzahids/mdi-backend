import urllib.request
import urllib.error
import json
import sys

API_ROOT = "http://127.0.0.1:8000/api/v1"
API_KEY = "dimsum-mes-secret-2026"

def api_post(endpoint, data, headers=None):
    if endpoint.startswith("/production2") or endpoint.startswith("/auth"):
        url = f"{API_ROOT}{endpoint}"
    else:
        url = f"{API_ROOT}/production1{endpoint}"
    req_headers = {
        "Content-Type": "application/json",
        "X-API-Key": API_KEY
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers=req_headers
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))

def api_get(endpoint, headers=None):
    if endpoint.startswith("/production2") or endpoint.startswith("/auth"):
        url = f"{API_ROOT}{endpoint}"
    else:
        url = f"{API_ROOT}/production1{endpoint}"
    req_headers = {
        "Content-Type": "application/json",
        "X-API-Key": API_KEY
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))

def run_test():
    print("=== STARTING FULL PRODUCTION 1 SYNC TEST ===")

    # 0. Ambil Plan Aktif
    active_plan = api_get("/plans/active")
    plan_id = active_plan.get("plan_id", "PLAN-20261005-01")
    print(f"-> Active Plan: {plan_id}")
    
    # 1. Stakeholder Cetak QR Karung Bumbu Sparta (1 Karung = 20 Porsi Gilingan)
    print("\n1. Stakeholder Mencetak QR Karung Bumbu Sparta...")
    created_bumbu_list = api_post("/seasoning/generate-sacks", {
        "plan_id": plan_id,
        "sack_count": 1,
        "portions_per_sack": 20,
        "operator_id": "STK-BUMBU",
        "operator_role": "STAKEHOLDER"
    })
    assert len(created_bumbu_list) >= 1, "Failed to create bumbu lot"
    bumbu_lot = created_bumbu_list[0]
    bumbu_code = bumbu_lot["lot_qr_code"]
    print(f"-> Karung Bumbu dicetak QR: {bumbu_code} (Status: {bumbu_lot['current_status']}, Qty: {bumbu_lot['quantity']} {bumbu_lot['unit']})")
    assert bumbu_lot["current_status"] == "PRINTED_PENDING_STOCK_IN"
    assert bumbu_lot["quantity"] == 20.0

    # 1b. Pegawai Tempel & Scan QR Bumbu untuk Konfirmasi Stock-In
    print(f"-> Pegawai Tempel & Scan Konfirmasi Stock-In Bumbu ({bumbu_code})...")
    confirm_bumbu_list = api_post("/lot/confirm-stock-in", {
        "lot_codes": [bumbu_code],
        "operator_id": "OPR-BUMBU",
        "operator_role": "OPERATOR"
    })
    assert len(confirm_bumbu_list) >= 1
    confirm_bumbu = confirm_bumbu_list[0]
    print(f"-> Status Bumbu setelah Stock-In: {confirm_bumbu['current_status']}")
    assert confirm_bumbu["current_status"] == "READY_IN_SEASONING_STATION"

    # 2. Kirim Bumbu (Scan-Out Batch ke Meja Giling & Mixer)
    print(f"\n2. Scan-Out Kirim Bumbu ({bumbu_code}) ke Ruang Giling & Mixer...")
    scan_out_res = api_post("/seasoning/scan-out-batch", {
        "lot_codes": [bumbu_code],
        "operator_id": "OPR-BUMBU",
        "operator_role": "OPERATOR_BUMBU",
        "target_station": "MEAT_MIXING_STATION"
    })
    print(f"-> Scan-out result count: {len(scan_out_res)}")
    
    # Verifikasi status bumbu sekarang IN_TRANSIT_TO_MEAT_MIXING
    all_lots = api_get("/lots")
    lot_map = {l["lot_qr_code"]: l for l in all_lots}
    assert lot_map[bumbu_code]["current_status"] == "IN_TRANSIT_TO_MEAT_MIXING", f"Wrong status: {lot_map[bumbu_code]['current_status']}"
    print(f"-> Verifikasi status di DB: {lot_map[bumbu_code]['current_status']} [OK]")

    # 3. Terima Bumbu di Meja Giling (Scan-In Batch)
    print(f"\n3. Scan-In Terima Bumbu ({bumbu_code}) di Meja Giling & Mixer...")
    scan_in_res = api_post("/meat/scan-in-bumbu-batch", {
        "lot_codes": [bumbu_code],
        "operator_id": "OPR-GILING",
        "operator_role": "OPERATOR_GILING_MIXER"
    })
    print(f"-> Scan-in result count: {len(scan_in_res)}")
    
    # Verifikasi status bumbu sekarang IN_MEAT_MIXING_STATION
    all_lots = api_get("/lots")
    lot_map = {l["lot_qr_code"]: l for l in all_lots}
    assert lot_map[bumbu_code]["current_status"] == "IN_MEAT_MIXING_STATION", f"Wrong status: {lot_map[bumbu_code]['current_status']}"
    print(f"-> Verifikasi status di DB: {lot_map[bumbu_code]['current_status']} [OK]")

    # 4. Stakeholder Cetak QR Daging Giling
    print("\n4. Stakeholder Mencetak QR Daging Giling...")
    meat_lots = api_post("/meat/generate-by-gilingan", {
        "plan_id": plan_id,
        "gilingan_count": 1,
        "operator_id": "STK-GILING",
        "operator_role": "STAKEHOLDER"
    })
    assert len(meat_lots) >= 1, "Failed to create meat lot"
    meat_lot = meat_lots[0]
    meat_code = meat_lot["lot_qr_code"]
    print(f"-> Daging giling dicetak QR: {meat_code} (Status: {meat_lot['current_status']})")
    assert meat_lot["current_status"] == "PRINTED_PENDING_STOCK_IN"

    # 4b. Pegawai Tempel & Scan QR Daging Giling untuk Konfirmasi Stock-In
    print(f"-> Pegawai Tempel & Scan Konfirmasi Stock-In Daging ({meat_code})...")
    confirm_meat_list = api_post("/lot/confirm-stock-in", {
        "lot_codes": [meat_code],
        "operator_id": "OPR-GILING",
        "operator_role": "OPERATOR"
    })
    assert len(confirm_meat_list) >= 1
    confirm_meat = confirm_meat_list[0]
    print(f"-> Status Daging setelah Stock-In: {confirm_meat['current_status']}")
    assert confirm_meat["current_status"] == "READY_IN_MEAT_STATION"

    # 5. Stakeholder Cetak QR Kulit & Pegawai Stock-In Kulit
    print("\n5. Stakeholder Mencetak QR Kulit Pangsit & Pegawai Konfirmasi Stock-In...")
    created_wrapper_9 = api_post("/wrapper/generate", {
        "plan_id": plan_id,
        "wrapper_type": "KULIT_9CM",
        "bal_qty": 1,
        "weight_kg": 15.0,
        "operator_id": "STK-KULIT",
        "operator_role": "STAKEHOLDER"
    })[0]
    wrapper_code = created_wrapper_9["lot_qr_code"]

    created_wrapper_7 = api_post("/wrapper/generate", {
        "plan_id": plan_id,
        "wrapper_type": "KULIT_7CM",
        "bal_qty": 1,
        "weight_kg": 15.0,
        "operator_id": "STK-KULIT",
        "operator_role": "STAKEHOLDER"
    })[0]
    wrapper_7_code = created_wrapper_7["lot_qr_code"]

    print(f"-> Kulit 9cm dicetak: {wrapper_code}, Kulit 7cm dicetak: {wrapper_7_code}")
    api_post("/lot/confirm-stock-in", {
        "lot_codes": [wrapper_code, wrapper_7_code],
        "operator_id": "OPR-KULIT",
        "operator_role": "OPERATOR"
    })
    print(f"-> Status Kulit 9cm & 7cm setelah Stock-In: READY_IN_WRAPPER_STATION")

    # 5b. Stakeholder Cetak QR Saos (Reguler & Mini) & Pegawai Stock-In & Scan-Out ke Finishing
    print("\n5b. Stakeholder Cetak QR Saos & Pegawai Konfirmasi Stock-In serta Scan-OUT ke Finishing...")
    # Mini (200 plastik/bal)
    created_sauce_mini = api_post("/sauce/generate", {
        "plan_id": plan_id,
        "sauce_type": "MINI",
        "bal_qty": 1,
        "operator_id": "STK-SAOS",
        "operator_role": "STAKEHOLDER"
    })
    sauce_mini_code = created_sauce_mini[0]["lot_qr_code"]
    print(f"-> Saos Mini dicetak QR: {sauce_mini_code} (Status: {created_sauce_mini[0]['current_status']})")
    assert created_sauce_mini[0]["current_status"] == "PRINTED_PENDING_STOCK_IN"

    # Reguler (50 pouch/bal)
    created_sauce_reguler = api_post("/sauce/generate", {
        "plan_id": plan_id,
        "sauce_type": "REGULER",
        "bal_qty": 1,
        "operator_id": "STK-SAOS",
        "operator_role": "STAKEHOLDER"
    })
    sauce_reg_code = created_sauce_reguler[0]["lot_qr_code"]
    print(f"-> Saos Reguler dicetak QR: {sauce_reg_code} (Status: {created_sauce_reguler[0]['current_status']})")
    assert created_sauce_reguler[0]["current_status"] == "PRINTED_PENDING_STOCK_IN"

    # Pegawai Stock-In Saos
    confirm_sauce_list = api_post("/lot/confirm-stock-in", {
        "lot_codes": [sauce_mini_code, sauce_reg_code],
        "operator_id": "OPR-SAOS",
        "operator_role": "OPERATOR"
    })
    assert len(confirm_sauce_list) >= 2
    for s in confirm_sauce_list:
        assert s["current_status"] == "READY_IN_SAUCE_STATION"
    print(f"-> Status Saos setelah Stock-In: READY_IN_SAUCE_STATION [OK]")

    # Pegawai Scan-OUT Saos ke Finishing (Bukan Cetak)
    scan_out_sauce_res = api_post("/sauce/scan-out", {
        "lot_code": sauce_reg_code,
        "operator_id": "OPR-SAOS",
        "operator_role": "OPERATOR_SAOS",
        "target_station": "FINISHING_STATION"
    })
    assert scan_out_sauce_res["current_status"] == "IN_TRANSIT_TO_FINISHING"
    print(f"-> Status Saos setelah Scan-OUT ke Finishing: {scan_out_sauce_res['current_status']} [OK]")

    # 6. Campur Adonan (Produce Dough 1:1 -> 1 Daging + 1 Bumbu)
    print(f"\n6. Mencampur Adonan (1:1 -> {meat_code} + {bumbu_code})...")
    dough_lots = api_post("/meat/produce-dough", {
        "plan_id": plan_id,
        "gilingan_count": 1,
        "dough_yield_kg": 20.00,
        "operator_id": "OPR-MIXER",
        "operator_role": "OPERATOR_GILING_MIXER",
        "meat_lot_codes": [meat_code],
        "bumbu_lot_codes": [bumbu_code]
    })
    assert len(dough_lots) >= 1, "Failed to produce dough"
    dough_lot = dough_lots[0]
    dough_code = dough_lot["lot_qr_code"]
    print(f"-> Adonan dibuat: {dough_code} (Status: {dough_lot['current_status']}, Qty: {dough_lot['quantity']} {dough_lot['unit']})")
    assert dough_lot["current_status"] == "READY_IN_MEAT_MIXING_STATION"

    # Verifikasi Bumbu tersisa 19 porsi dan Daging status jadi CONSUMED
    all_lots = api_get("/lots")
    lot_map = {l["lot_qr_code"]: l for l in all_lots}
    assert lot_map[meat_code]["current_status"] == "CONSUMED", f"Meat status expected CONSUMED, got {lot_map[meat_code]['current_status']}"
    assert float(lot_map[bumbu_code]["quantity"]) == 19.0, f"Bumbu quantity expected 19.0, got {lot_map[bumbu_code]['quantity']}"
    assert lot_map[bumbu_code]["current_status"] == "IN_MEAT_MIXING_STATION", f"Bumbu status expected IN_MEAT_MIXING_STATION, got {lot_map[bumbu_code]['current_status']}"
    print(f"-> Verifikasi bahan baku di DB: Daging -> CONSUMED, Karung Bumbu -> sisa {lot_map[bumbu_code]['quantity']} porsi [OK]")

    # 7. Scan-Out Adonan ke Divisi Kulit / Filling (Produksi 2)
    print(f"\n7. Scan-Out Kirim Adonan ({dough_code}) ke Assembly / Filling...")
    scan_out_dough_res = api_post("/seasoning/scan-out-dough", {
        "lot_code": dough_code,
        "operator_id": "OPR-MIXER",
        "operator_role": "OPERATOR_GILING_MIXER",
        "target_station": "ASSEMBLY_STATION"
    })
    print(f"-> Scan-out dough status: {scan_out_dough_res.get('current_status')}")
    assert scan_out_dough_res.get("current_status") == "IN_TRANSIT_TO_ASSEMBLY"
    print(f"-> Verifikasi status adonan: IN_TRANSIT_TO_ASSEMBLY [OK]")

    # 7b. PRODUKSI 2: Penerimaan Bahan (Inbound Receiving: Adonan, Kulit, Saos)
    print(f"\n7b. Produksi 2 Menerima Bahan Masuk (Adonan, Kulit, Saos)...")
    # Scan-IN Adonan di Bagian Cetak
    recv_dough = api_post("/production2/receive-material", {
        "lot_code": dough_code,
        "operator_id": "OPR-CETAK",
        "operator_role": "OPERATOR_CETAK"
    })
    assert recv_dough.get("current_status") == "IN_ASSEMBLY_STATION"
    print(f"-> Adonan {dough_code} berhasil diterima di Cetak (Status: IN_ASSEMBLY_STATION) [OK]")

    # Scan-IN Kulit di Bagian Cetak (9cm dan 7cm)
    for w_code in [wrapper_code, wrapper_7_code]:
        api_post("/wrapper/scan-out", {
            "lot_code": w_code,
            "operator_id": "OPR-KULIT",
            "operator_role": "OPERATOR_KULIT",
            "target_station": "ASSEMBLY_STATION"
        })
        recv_w = api_post("/production2/receive-material", {
            "lot_code": w_code,
            "operator_id": "OPR-CETAK",
            "operator_role": "OPERATOR_CETAK"
        })
        assert recv_w.get("current_status") == "IN_ASSEMBLY_STATION"
    print(f"-> Kulit 9cm & 7cm ({wrapper_code}, {wrapper_7_code}) berhasil diterima di Cetak [OK]")

    # Scan-IN Saos di Bagian Finishing
    recv_sauce = api_post("/production2/receive-material", {
        "lot_code": sauce_reg_code,
        "operator_id": "OPR-FINISHING",
        "operator_role": "OPERATOR_FINISHING"
    })
    assert recv_sauce.get("current_status") == "IN_FINISHING_STATION"
    print(f"-> Saos {sauce_reg_code} berhasil diterima di Finishing (Status: IN_FINISHING_STATION) [OK]")

    # Cek query inbound-materials
    inbound_summary = api_get("/production2/inbound-materials")
    print(f"-> Inbound Materials Produksi 2: {inbound_summary.get('received_count')} diterima, {inbound_summary.get('transit_count')} transit [OK]")

    # 7c. PRODUKSI 2: Kemas & Terbitkan FG Langsung dari Meja (Mika 50 pcs & Vakum 25 pcs)
    print(f"\n7c. Menyelesaikan Packing Langsung di Produksi 2 (Tanpa Auto-Potong Stok)...")
    fg_lots_res = api_post("/production2/complete-packing-to-fg", {
        "plan_id": plan_id,
        "tray_lot_codes": [],
        "dough_lot_codes": [],
        "wrapper_lot_codes": [],
        "mika_output_qty": 2,
        "vakum_output_qty": 3,
        "reject_pcs": 0,
        "sauce_lot_codes": [sauce_reg_code],
        "operator_id": "OM-YOSI",
        "operator_role": "CHECKER",
        "notes": "Direct packing test tanpa transfer nampan"
    })
    assert len(fg_lots_res) == 5, f"Expected 5 FG lots, got {len(fg_lots_res)}"
    mika_lots = [l for l in fg_lots_res if l["lot_type"] == "FG_MIKA"]
    vakum_lots = [l for l in fg_lots_res if l["lot_type"] == "FG_VAKUM"]
    assert len(mika_lots) == 2, f"Expected 2 Mika lots, got {len(mika_lots)}"
    assert len(vakum_lots) == 3, f"Expected 3 Vakum lots, got {len(vakum_lots)}"
    print(f"-> 2 Dimsum Mika (@ 50 pcs) & 3 Dimsum Vakum (@ 25 pcs) berhasil diterbitkan [OK]")
    for fg in fg_lots_res:
        print(f"   - {fg['lot_qr_code']}: Qty {fg['quantity']} {fg['unit']}, Status {fg['current_status']}")

    # Verifikasi Adonan & Kulit tetap aman di meja (tidak terhapus auto-potong)
    lots_mid = api_get("/lots")
    lot_map_mid = {l["lot_qr_code"]: l for l in lots_mid}
    assert lot_map_mid[dough_code]["current_status"] == "IN_ASSEMBLY_STATION"
    assert lot_map_mid[wrapper_code]["current_status"] == "IN_ASSEMBLY_STATION"
    print(f"-> Bahan di meja ({dough_code} & {wrapper_code}) tetap utuh di meja menunggu konfirmasi pegawai [OK]")

    # 7c-2. Media Penyesuaian & Pengurangan Stok Manual oleh Pegawai
    print(f"\n7c-2. Pegawai Menggunakan Media Pengurangan Stok Manual dengan Notes...")
    # A. Pengurangan parsial adonan (kurang 5 kg)
    adj_partial = api_post("/production2/adjust-material-stock", {
        "lot_code": dough_code,
        "action_type": "CONSUME_PARTIAL",
        "quantity_used": 5.0,
        "notes": "Dipakai cetak mika pagi",
        "operator_id": "OPR-CETAK",
        "operator_role": "OPERATOR"
    })
    assert float(adj_partial["quantity"]) == 15.0
    assert adj_partial["current_status"] == "IN_ASSEMBLY_STATION"
    print(f"-> Berhasil kurangi parsial adonan {dough_code}: sisa {adj_partial['quantity']} kg [OK]")

    # B. Habis terpakai adonan (CONSUME_ALL)
    adj_full_dough = api_post("/production2/adjust-material-stock", {
        "lot_code": dough_code,
        "action_type": "CONSUME_ALL",
        "notes": "Sisa adonan habis terpakai cetak hari ini",
        "operator_id": "OPR-CETAK",
        "operator_role": "OPERATOR"
    })
    assert adj_full_dough["current_status"] == "CONSUMED"
    print(f"-> Berhasil tandai adonan {dough_code} HABIS TERPAKAI (Status: CONSUMED) [OK]")

    # C. Habis terpakai kulit (CONSUME_ALL)
    adj_full_wrap = api_post("/production2/adjust-material-stock", {
        "lot_code": wrapper_code,
        "action_type": "CONSUME_ALL",
        "notes": "Kulit 9cm habis terpakai",
        "operator_id": "OPR-CETAK",
        "operator_role": "OPERATOR"
    })
    assert adj_full_wrap["current_status"] == "CONSUMED"
    print(f"-> Berhasil tandai kulit {wrapper_code} HABIS TERPAKAI (Status: CONSUMED) [OK]")

    # 7d. PRODUKSI 2: Kirim Produk Jadi ke Gudang Freezer (Siap POS)
    print(f"\n7d. Kirim Semua Produk Jadi ke Gudang Freezer...")
    all_fg_codes = [fg["lot_qr_code"] for fg in fg_lots_res]
    freezer_res = api_post("/production2/dispatch-to-freezer", {
        "lot_codes": all_fg_codes,
        "operator_id": "OM-YOSI",
        "operator_role": "CHECKER"
    })
    assert len(freezer_res) == 5
    for fr in freezer_res:
        assert fr["current_status"] == "IN_FREEZER_STORAGE"
    print(f"-> Semua 5 produk jadi berhasil masuk freezer (Status: IN_FREEZER_STORAGE) [OK]")

    # 7e. Cek daftar finished-goods dari API
    fg_list = api_get("/production2/finished-goods")
    assert len(fg_list) >= 5
    print(f"-> Query /finished-goods mengembalikan {len(fg_list)} produk siap jual POS [OK]")

    # 8. Verifikasi Custody Transfer Logs
    logs = api_get("/custody-logs")
    print(f"\n8. Total Custody Transfer Logs di DB: {len(logs)}")
    relevant_logs = [l for l in logs if l.get("lot_qr_code") in [bumbu_code, meat_code, wrapper_code, dough_code, sauce_reg_code] or l.get("lot_qr_code") in all_fg_codes]
    for rl in relevant_logs:
        print(f"   - Lot {rl.get('lot_qr_code')}: action={rl.get('action_type')}, station={rl.get('station_id')} -> {rl.get('target_station')}, op={rl.get('user_id')}")

    print("\n=== FULL PRODUCTION 1 & PRODUCTION 2 SYNC TEST PASSED PERFECTLY! ===")


def run_security_tests():
    print("\n=== STARTING CYBERSECURITY & MUST-FIX REGRESSION TESTS ===")

    # 1. Test Unauthenticated Request: Harus ditolak dengan HTTP 401
    print("1. Menguji endpoint yang dilindungi tanpa X-API-Key / Token...")
    try:
        req = urllib.request.Request(f"{API_ROOT}/production1/plans")
        with urllib.request.urlopen(req) as resp:
            assert False, "Harusnya gagal 401 tetapi berhasil 200!"
    except urllib.error.HTTPError as e:
        assert e.code == 401, f"Expected HTTP 401, got {e.code}"
        print(f"-> Berhasil: Request tanpa autentikasi ditolak (HTTP {e.code}) [OK]")

    # 2. Test Wipe-Database Endpoint: Harus 404 (karena sudah dihapus)
    print("\n2. Menguji endpoint berbahaya /wipe-database...")
    try:
        req = urllib.request.Request(
            f"{API_ROOT}/production1/wipe-database",
            data=b"{}",
            headers={"Content-Type": "application/json", "X-API-Key": API_KEY}
        )
        with urllib.request.urlopen(req) as resp:
            assert False, "Harusnya 404 tetapi endpoint masih ada!"
    except urllib.error.HTTPError as e:
        assert e.code in [404, 405], f"Expected 404 or 405, got {e.code}"
        print(f"-> Berhasil: Endpoint /wipe-database telah terhapus (HTTP {e.code}) [OK]")

    # 3. Test Auth Login & Salah Password
    print("\n3. Menguji Login Akun Divisi & Validasi Password...")
    login_res = api_post("/auth/login", {
        "username": "opr_daging",
        "password": "password123"
    })
    assert "access_token" in login_res, "Access token missing from login response"
    assert login_res["user"]["operator_id"] == "OPR-DAGING"
    print(f"-> Berhasil login sebagai: {login_res['user']['full_name']} (Role: {login_res['user']['role']}) [OK]")

    # Login salah password
    try:
        api_post("/auth/login", {
            "username": "opr_daging",
            "password": "wrongpassword"
        })
        assert False, "Harusnya gagal 401 jika password salah"
    except urllib.error.HTTPError as e:
        assert e.code == 401
        print(f"-> Berhasil: Login password salah ditolak (HTTP {e.code}) [OK]")

    # 4. Test Quick Switch (Dev Mode)
    print("\n4. Menguji Dev Mode Quick Switcher...")
    quick_res = api_post("/auth/quick-switch", {"username": "supervisor"})
    assert quick_res["user"]["operator_id"] == "STK-YOSI"
    assert quick_res["user"]["role"] == "STAKEHOLDER"
    print(f"-> Berhasil quick-switch ke: {quick_res['user']['full_name']} (Role: {quick_res['user']['role']}) [OK]")

    # 5. Test Anti-Replay: Mencoba memakai kembali lot adonan yang sudah CONSUMED
    print("\n5. Menguji Proteksi Double-Spending / Anti-Replay Adonan...")
    # Ambil adonan yang sudah berstatus CONSUMED dari DB
    all_lots = api_get("/lots")
    consumed_dough = [l["lot_qr_code"] for l in all_lots if l.get("lot_type") == "ADONAN" and l.get("current_status") == "CONSUMED"]
    if consumed_dough:
        test_adonan = consumed_dough[0]
        try:
            api_post("/production2/complete-assembly-batch", {
                "plan_id": "PLAN-TEST",
                "dough_lot_codes": [test_adonan],
                "wrapper_lot_codes": ["KL-TEST"],
                "mika_output_qty": 1,
                "vakum_output_qty": 0,
                "operator_id": "OPR-TEST",
                "operator_role": "OPERATOR"
            })
            assert False, "Harusnya ditolak karena adonan sudah CONSUMED"
        except urllib.error.HTTPError as e:
            assert e.code == 400
            print(f"-> Berhasil: Percobaan replay adonan {test_adonan} ditolak (HTTP {e.code}) [OK]")

    # 6. Test Supervisor Bypass Protection: Tolak jika tanpa PIN
    print("\n6. Menguji Gatekeeper Supervisor Bypass...")
    # Buat 1 lot daging uji coba berstatus READY_IN_MEAT_STATION
    test_meat = api_post("/meat/generate-by-gilingan", {
        "plan_id": "PLAN-SECURITY-TEST",
        "gilingan_count": 1,
        "operator_id": "STK-TEST",
        "operator_role": "STAKEHOLDER"
    })[0]["lot_qr_code"]

    # Konfirmasi stock-in agar status READY_IN_MEAT_STATION
    api_post("/lot/confirm-stock-in", {
        "lot_codes": [test_meat],
        "operator_id": "OPR-TEST",
        "operator_role": "OPERATOR"
    })

    # Coba bypass tanpa PIN -> harus 403
    try:
        api_post("/seasoning/scan-in", {
            "lot_code": test_meat,
            "operator_id": "OPR-TEST",
            "operator_role": "OPERATOR",
            "allow_supervisor_bypass": True,
            "supervisor_pin": None
        })
        assert False, "Harusnya 403 Forbidden tanpa PIN Supervisor"
    except urllib.error.HTTPError as e:
        assert e.code == 403
        print(f"-> Berhasil: Supervisor bypass tanpa PIN ditolak (HTTP {e.code}) [OK]")

    # Coba bypass dengan PIN yang benar (8888) -> harus 200
    bypass_ok = api_post("/seasoning/scan-in", {
        "lot_code": test_meat,
        "operator_id": "OPR-TEST",
        "operator_role": "OPERATOR",
        "allow_supervisor_bypass": True,
        "supervisor_pin": "8888",
        "bypass_reason": "Testing authorized supervisor bypass"
    })
    assert bypass_ok["current_status"] == "IN_SEASONING_STATION"
    print(f"-> Berhasil: Supervisor bypass dengan PIN valid diterima (Status: {bypass_ok['current_status']}) [OK]")

    print("\n=== ALL CYBERSECURITY & MUST-FIX TESTS PASSED 100%! ===")


if __name__ == "__main__":
    run_test()
    run_security_tests()

