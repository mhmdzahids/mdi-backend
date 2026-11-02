"""
Empirical Adversarial Verification Suite: Role Authorization & PPIC Access Controls.
Tests every account in ACCOUNTS, attack vectors (spoofing, forged tokens, expired tokens,
API key impersonation, division boundary crossing), and verifies HTTP 401 / 403 enforcement.
"""

import asyncio
import json
import time
import hmac
import hashlib
from typing import Dict, Any, Tuple

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import app
from app.auth import ACCOUNTS, create_token, b64url_encode, JWT_SECRET, API_KEY


async def asgi_request(
    method: str,
    path: str,
    headers: Dict[str, str] = None,
    json_data: Any = None
) -> Tuple[int, Dict[str, str], Dict[str, Any]]:
    body_bytes = json.dumps(json_data).encode("utf-8") if json_data is not None else b""
    hdr_dict = dict(headers or {})
    if json_data is not None and "content-type" not in [k.lower() for k in hdr_dict]:
        hdr_dict["content-type"] = "application/json"
    hdr_dict["content-length"] = str(len(body_bytes))
    hdr_dict["host"] = "testserver"

    hdr_list = [(k.lower().encode("latin1"), v.encode("latin1")) for k, v in hdr_dict.items()]

    response_status = None
    response_headers = []
    response_body = []

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    async def send(message):
        nonlocal response_status, response_headers, response_body
        if message["type"] == "http.response.start":
            response_status = message["status"]
            response_headers = message["headers"]
        elif message["type"] == "http.response.body":
            response_body.append(message.get("body", b""))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method.upper(),
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": hdr_list,
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
    }

    await app(scope, receive, send)
    resp_headers_dict = {k.decode("latin1"): v.decode("latin1") for k, v in response_headers}
    raw_body = b"".join(response_body)
    try:
        parsed_body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception:
        parsed_body = {"raw": raw_body.decode("utf-8", errors="replace")}

    return response_status, resp_headers_dict, parsed_body


def make_forged_token(user_data: Dict[str, Any], secret: bytes = b"wrong-secret-key-12345") -> str:
    payload = {
        "sub": user_data["username"],
        "operator_id": user_data["operator_id"],
        "full_name": user_data["full_name"],
        "role": user_data["role"],
        "division": user_data["division"],
        "exp": int(time.time()) + 3600
    }
    header_json = json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8")
    payload_json = json.dumps(payload).encode("utf-8")
    h_b64 = b64url_encode(header_json)
    p_b64 = b64url_encode(payload_json)
    signature_data = f"{h_b64}.{p_b64}".encode("utf-8")
    sig = hmac.new(secret, signature_data, hashlib.sha256).digest()
    sig_b64 = b64url_encode(sig)
    return f"{h_b64}.{p_b64}.{sig_b64}"


def make_expired_token(user_data: Dict[str, Any]) -> str:
    return create_token(user_data, expires_in_seconds=-3600)


async def run_adversarial_suite():
    print("=" * 80)
    print("EMPIRICAL ADVERSARIAL ROLE AUTHORIZATION & ACCESS CONTROL SUITE")
    print("=" * 80)

    results = {
        "passed": 0,
        "failed": 0,
        "details": []
    }

    def record_test(name: str, passed: bool, info: str = ""):
        if passed:
            results["passed"] += 1
            print(f"  [PASS] {name} {info}")
        else:
            results["failed"] += 1
            print(f"  [FAIL] {name} {info}")
        results["details"].append({"test": name, "passed": passed, "info": info})

    # -------------------------------------------------------------------------
    # 1. Full Matrix: All 16 Accounts against POST /generate-daily-lots-all
    # -------------------------------------------------------------------------
    print("\n[Phase 1] Testing all 16 accounts against POST /api/v1/production1/generate-daily-lots-all")
    ppic_payload = {
        "plan_id": "PLAN-20261029-TEST",
        "gilingan_count": 1,
        "sack_count": 1,
        "wrapper_bal": 1,
        "sauce_bal": 1
    }

    for username, acc in ACCOUNTS.items():
        token = create_token(acc)
        status, _, body = await asgi_request(
            method="POST",
            path="/api/v1/production1/generate-daily-lots-all",
            headers={"Authorization": f"Bearer {token}"},
            json_data=ppic_payload
        )

        is_ppic = username in ["ppic_ayu", "supervisor"]
        if is_ppic:
            expected_status = 201
            passed = (status == expected_status and body.get("status") == "success")
        else:
            expected_status = 403
            passed = (status == expected_status)

        record_test(
            f"Account '{username}' (role={acc['role']}, div='{acc['division']}')",
            passed,
            f"-> Status {status} (Expected {expected_status})"
        )

    # -------------------------------------------------------------------------
    # 2. Testing All Protected PPIC Endpoints with Unauthorized Division PIC
    # -------------------------------------------------------------------------
    print("\n[Phase 2] Testing PPIC endpoints rejection with Division PIC (pic_daging)")
    pic_daging_token = create_token(ACCOUNTS["pic_daging"])

    endpoints_to_test = [
        ("POST", "/api/v1/production1/generate-daily-lots-all", {
            "gilingan_count": 2, "sack_count": 0, "wrapper_bal": 0, "sauce_bal": 0
        }),
        ("POST", "/api/v1/production1/supplier-inbound", {
            "category": "KULIT",
            "item_name": "Kulit Dimsum 9 cm",
            "quantity": 5.0,
            "unit": "BAL",
            "supplier_name": "Vendor A"
        }),
        ("POST", "/api/v1/production1/daily-notes", {
            "division": "DAGING",
            "reject_count": 10,
            "reject_notes": "Adonan rusak",
            "operational_notes": "Mesin mixer sempat mati"
        }),
        ("POST", "/api/v1/production1/plans", {
            "plan_id": "PLAN-20261029-FORGED",
            "plan_date": "2026-10-29",
            "target_gilingan": 50,
            "target_mika_pax": 1000,
            "target_vakum_pax": 500,
            "created_by": "PIC-DAGING"
        }),
        ("POST", "/api/v1/production1/meat/generate-by-gilingan", {
            "plan_id": "PLAN-20261029-FORGED",
            "gilingan_count": 3,
            "operator_id": "PIC-DAGING",
            "operator_role": "STAKEHOLDER"
        }),
        ("POST", "/api/v1/production1/meat-mixing/produce-batch", {
            "plan_id": "PLAN-20261029-FORGED",
            "gilingan_count": 3,
            "dough_yield_kg": 20.0,
            "operator_id": "PIC-DAGING",
            "operator_role": "STAKEHOLDER"
        }),
        ("POST", "/api/v1/production1/wrapper/generate", {
            "plan_id": "PLAN-20261029-FORGED",
            "wrapper_type": "KULIT_9CM",
            "bal_qty": 2,
            "operator_id": "PIC-DAGING",
            "operator_role": "STAKEHOLDER"
        }),
        ("POST", "/api/v1/production1/sauce/generate", {
            "plan_id": "PLAN-20261029-FORGED",
            "sauce_type": "REGULER",
            "bal_qty": 2,
            "operator_id": "PIC-DAGING",
            "operator_role": "STAKEHOLDER"
        }),
        ("POST", "/api/v1/production1/seasoning/generate-sacks", {
            "plan_id": "PLAN-20261029-FORGED",
            "sack_count": 2,
            "operator_id": "PIC-DAGING",
            "operator_role": "STAKEHOLDER"
        }),
        ("POST", "/api/v1/production1/seasoning/generate-pouches", {
            "plan_id": "PLAN-20261029-FORGED",
            "pouch_count": 40,
            "operator_id": "PIC-DAGING",
            "operator_role": "STAKEHOLDER"
        }),
        ("POST", "/api/v1/production1/meat/produce-dough", {
            "meat_lot_codes": ["DG-20261029-001"],
            "seasoning_pouch_codes": ["BM-20261029-001"],
            "operator_id": "PIC-DAGING",
            "operator_role": "STAKEHOLDER"
        })
    ]

    for method, path, payload in endpoints_to_test:
        status, _, body = await asgi_request(
            method=method,
            path=path,
            headers={"Authorization": f"Bearer {pic_daging_token}"},
            json_data=payload
        )
        passed = (status == 403)
        record_test(
            f"PIC Daging Access to {method} {path}",
            passed,
            f"-> Status {status} (Expected 403 Forbidden)"
        )

    # -------------------------------------------------------------------------
    # 3. Attack Scenarios: Payload Spoofing, Forged Token, Expired Token, API Key
    # -------------------------------------------------------------------------
    print("\n[Phase 3] Attack Scenarios: Spoofing, Forgery, Expiration, API Key Impersonation")

    # Attack 1: Payload Identity Spoofing (opr_daging claims operator_id: 'PPIC-AYU')
    opr_daging_token = create_token(ACCOUNTS["opr_daging"])
    spoofed_payload = {
        "operator_id": "PPIC-AYU",
        "operator_role": "STAKEHOLDER",
        "gilingan_count": 5
    }
    status, _, _ = await asgi_request(
        method="POST",
        path="/api/v1/production1/generate-daily-lots-all",
        headers={"Authorization": f"Bearer {opr_daging_token}"},
        json_data=spoofed_payload
    )
    record_test(
        "Attack 1: Payload Identity Spoofing (opr_daging token + 'PPIC-AYU' body)",
        status == 403,
        f"-> Status {status} (Expected 403 Forbidden)"
    )

    # Attack 2: Forged Token with Altered Signature
    forged_token = make_forged_token(ACCOUNTS["ppic_ayu"])
    status, _, _ = await asgi_request(
        method="POST",
        path="/api/v1/production1/generate-daily-lots-all",
        headers={"Authorization": f"Bearer {forged_token}"},
        json_data=ppic_payload
    )
    record_test(
        "Attack 2: Forged Token (Tampered secret key)",
        status == 401,
        f"-> Status {status} (Expected 401 Unauthorized)"
    )

    # Attack 3: Expired Token
    expired_token = make_expired_token(ACCOUNTS["ppic_ayu"])
    status, _, _ = await asgi_request(
        method="POST",
        path="/api/v1/production1/generate-daily-lots-all",
        headers={"Authorization": f"Bearer {expired_token}"},
        json_data=ppic_payload
    )
    record_test(
        "Attack 3: Expired Token (exp in the past)",
        status == 401,
        f"-> Status {status} (Expected 401 Unauthorized)"
    )

    # Attack 4: Missing Authorization Header
    status, _, _ = await asgi_request(
        method="POST",
        path="/api/v1/production1/generate-daily-lots-all",
        headers={},
        json_data=ppic_payload
    )
    record_test(
        "Attack 4: Unauthenticated Request (No headers)",
        status == 401,
        f"-> Status {status} (Expected 401 Unauthorized)"
    )

    # Attack 5: API Key Impersonation (X-API-Key SYSTEM_API on PPIC endpoint)
    status, _, _ = await asgi_request(
        method="POST",
        path="/api/v1/production1/generate-daily-lots-all",
        headers={"X-API-Key": API_KEY},
        json_data=ppic_payload
    )
    record_test(
        "Attack 5: API Key Impersonation (X-API-Key used on PPIC endpoint)",
        status == 403,
        f"-> Status {status} (Expected 403 Forbidden)"
    )

    # Attack 6: Division Cross-Access Boundary Testing
    # Meat operator tries to call Seasoning scan-in
    status, _, _ = await asgi_request(
        method="POST",
        path="/api/v1/production1/seasoning/scan-in",
        headers={"Authorization": f"Bearer {opr_daging_token}"},
        json_data={"lot_code": "DG-20261029-001", "operator_id": "OPR-DAGING", "operator_role": "OPERATOR"}
    )
    record_test(
        "Attack 6: Cross-Division Access (Meat operator on Seasoning scan-in)",
        status == 403,
        f"-> Status {status} (Expected 403 Forbidden)"
    )

    # -------------------------------------------------------------------------
    # 4. Legitimate PPIC Access Verification
    # -------------------------------------------------------------------------
    print("\n[Phase 4] Legitimate PPIC Access Verification")
    ppic_ayu_token = create_token(ACCOUNTS["ppic_ayu"])

    # Legitimate lot generation
    status, _, body = await asgi_request(
        method="POST",
        path="/api/v1/production1/generate-daily-lots-all",
        headers={"Authorization": f"Bearer {ppic_ayu_token}"},
        json_data={
            "plan_id": "PLAN-20261029-LEGIT",
            "gilingan_count": 2,
            "sack_count": 1,
            "wrapper_bal": 1,
            "sauce_bal": 1
        }
    )
    record_test(
        "Legitimate PPIC (Ayu) Lot Generation",
        status == 201 and body.get("counts", {}).get("total") == 5,
        f"-> Status {status}, Lots generated: {body.get('counts', {}).get('total')}"
    )

    # Legitimate Supplier Inbound
    status, _, body = await asgi_request(
        method="POST",
        path="/api/v1/production1/supplier-inbound",
        headers={"Authorization": f"Bearer {ppic_ayu_token}"},
        json_data={
            "category": "KULIT",
            "item_name": "Kulit Dimsum 9 cm (Mika)",
            "quantity": 10.0,
            "unit": "BAL",
            "supplier_name": "Sumber Kulit Rejeki",
            "document_no": "SJ-20261029-01",
            "notes": "Penerimaan terverifikasi"
        }
    )
    record_test(
        "Legitimate PPIC (Ayu) Supplier Inbound",
        status == 201 and body.get("supplier_name") == "Sumber Kulit Rejeki",
        f"-> Status {status}, Inbound ID: {body.get('inbound_id')}"
    )

    # Legitimate Division Daily Note
    status, _, body = await asgi_request(
        method="POST",
        path="/api/v1/production1/daily-notes",
        headers={"Authorization": f"Bearer {ppic_ayu_token}"},
        json_data={
            "division": "DAGING",
            "reject_count": 2,
            "reject_notes": "Adonan jatuh 2 kg",
            "operational_notes": "Shift 1 selesai sesuai jadwal"
        }
    )
    record_test(
        "Legitimate PPIC (Ayu) Division Daily Note",
        status == 200 and body.get("division") == "DAGING",
        f"-> Status {status}, Division: {body.get('division')}"
    )

    print("\n" + "=" * 80)
    print(f"ADVERSARIAL SUITE SUMMARY: {results['passed']} PASSED, {results['failed']} FAILED")
    print("=" * 80)
    return results


if __name__ == "__main__":
    results = asyncio.run(run_adversarial_suite())
    if results["failed"] > 0:
        exit(1)
    exit(0)
