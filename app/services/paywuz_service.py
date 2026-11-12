import os
import hmac
import hashlib
import logging
from typing import Optional, Dict, Any
import httpx

logger = logging.getLogger(__name__)

PAYWUZ_API_KEY = os.getenv("PAYWUZ_API_KEY", "pk_sand_e65dd0378cfaf7f87e6e80aa5d33ab59")
PAYWUZ_API_URL = os.getenv("PAYWUZ_API_URL", "https://api.paywuz.id/v1").rstrip("/")
PAYWUZ_WEBHOOK_SECRET = os.getenv("PAYWUZ_WEBHOOK_SECRET", "")


def verify_webhook_signature(
    payload_bytes: bytes,
    signature_header: str,
    secret: Optional[str] = None
) -> bool:
    """
    Verifikasi HMAC-SHA256 signature dari webhook Paywuz gateway.
    """
    webhook_secret = secret or PAYWUZ_WEBHOOK_SECRET or os.getenv("PAYWUZ_WEBHOOK_SECRET", "")
    if not webhook_secret or not signature_header:
        return False
    try:
        sig = signature_header.strip()
        if sig.startswith("sha256="):
            sig = sig[7:]
        expected = hmac.new(
            webhook_secret.encode("utf-8"),
            payload_bytes,
            hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected.lower(), sig.lower())
    except Exception as e:
        logger.error(f"Error validating webhook signature: {e}")
        return False


class PaywuzService:
    verify_webhook_signature = staticmethod(verify_webhook_signature)
    @staticmethod
    def get_headers() -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {PAYWUZ_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "MDI-POS/1.0"
        }

    @classmethod
    async def create_qris(
        cls,
        order_id: str,
        amount: int,
        customer_name: Optional[str] = "Walk-in POS",
        phone: Optional[str] = "081234567890",
        email: Optional[str] = "pos@makandimsum.com"
    ) -> Dict[str, Any]:
        url = f"{PAYWUZ_API_URL}/transactions"
        payload = {
            "orderId": order_id,
            "amount": int(amount),
            "paymentMethod": "QRIS",
            "metadata": {
                "customerName": (customer_name or "Walk-in POS")[:100],
                "phone": phone or "081234567890",
                "email": email or "pos@makandimsum.com"
            }
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=cls.get_headers())
            if resp.status_code in (200, 201):
                body = resp.json()
                return body.get("data", body)
            else:
                err_text = resp.text
                logger.error(f"Paywuz create QRIS failed HTTP {resp.status_code}: {err_text}")
                try:
                    err_json = resp.json()
                    msg = err_json.get("message") or err_json.get("error") or "Gagal membuat transaksi QRIS"
                except Exception:
                    msg = f"Paywuz HTTP {resp.status_code}"
                raise ValueError(msg)

    @classmethod
    async def create_va(
        cls,
        order_id: str,
        amount: int,
        bank_code: str = "BNIVA",
        customer_name: Optional[str] = "Walk-in POS",
        phone: Optional[str] = "081234567890",
        email: Optional[str] = "pos@makandimsum.com"
    ) -> Dict[str, Any]:
        url = f"{PAYWUZ_API_URL}/transactions"
        payload = {
            "orderId": order_id,
            "amount": int(amount),
            "paymentMethod": bank_code,
            "metadata": {
                "customerName": (customer_name or "Walk-in POS")[:100],
                "phone": phone or "081234567890",
                "email": email or "pos@makandimsum.com"
            }
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=cls.get_headers())
            if resp.status_code in (200, 201):
                body = resp.json()
                return body.get("data", body)
            else:
                err_text = resp.text
                logger.error(f"Paywuz create VA failed HTTP {resp.status_code}: {err_text}")
                try:
                    err_json = resp.json()
                    msg = err_json.get("message") or err_json.get("error") or "Gagal membuat Virtual Account"
                except Exception:
                    msg = f"Paywuz HTTP {resp.status_code}"
                raise ValueError(msg)

    @classmethod
    async def get_status(cls, order_id: str) -> Dict[str, Any]:
        url = f"{PAYWUZ_API_URL}/transactions/{order_id}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=cls.get_headers())
            if resp.status_code == 200:
                body = resp.json()
                return body.get("data", body)
            elif resp.status_code == 404:
                return {"status": "not_found", "orderId": order_id}
            else:
                logger.warning(f"Paywuz check status HTTP {resp.status_code}: {resp.text}")
                return {"status": "unknown", "orderId": order_id}
