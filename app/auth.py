import os
import hmac
import hashlib
import json
import base64
import time
import bcrypt
from typing import Optional, Dict, Any
from fastapi import Security, HTTPException, status, Depends
from fastapi.security import APIKeyHeader, HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv

load_dotenv()

JWT_SECRET = os.getenv("JWT_SECRET", "dimsum-secret-jwt-key-production-2026").encode("utf-8")
API_KEY = os.getenv("API_KEY", "dimsum-mes-secret-2026")
SUPERVISOR_PIN = os.getenv("SUPERVISOR_PIN", "8888")

# Jalur penyimpanan file kredensial persisten (jika user mengubah password)
CREDENTIALS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "credentials.json")

# Hash bcrypt terstandardisasi industri (cost factor 12) untuk password default 'password123'
DEFAULT_BCRYPT_HASH = "$2b$12$FeitLrYSkG9EmWagS1oSc.WSZMGv1rvvpTRoEkegwAT390yYXYwAC"


def hash_password(password: str) -> str:
    """Hash password menggunakan algoritma standar industri bcrypt (cost factor 12)."""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_or_plain: str) -> bool:
    """
    Verifikasi kecocokan password terhadap hash bcrypt dengan proteksi timing-attack.
    Mendukung fallback backward compatibility.
    """
    if not plain_password or not hashed_or_plain:
        return False

    if hashed_or_plain.startswith(("$2a$", "$2b$", "$2y$")):
        try:
            return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_or_plain.encode("utf-8"))
        except Exception:
            return False

    # Fallback aman jika terdapat plain password sebelum migrasi
    return hmac.compare_digest(plain_password, hashed_or_plain)


# Predefined Accounts untuk setiap Divisi & PIC
ACCOUNTS: Dict[str, Dict[str, Any]] = {
    # 1. Divisi Daging (Giling & Mixer)
    "opr_daging": {
        "username": "opr_daging",
        "password": DEFAULT_BCRYPT_HASH,
        "operator_id": "OPR-DAGING",
        "full_name": "Budi Santoso",
        "role": "OPERATOR",
        "division": "Divisi Daging (Giling & Mixer)",
        "default_stage": "PROD_1",
        "default_station": "MEAT",
        "default_prod2_tab": "INBOUND"
    },
    "pic_daging": {
        "username": "pic_daging",
        "password": DEFAULT_BCRYPT_HASH,
        "operator_id": "PIC-DAGING",
        "full_name": "Pak Hendra (PIC Daging)",
        "role": "STAKEHOLDER",
        "division": "Divisi Daging (Giling & Mixer)",
        "default_stage": "PROD_1",
        "default_station": "MEAT",
        "default_prod2_tab": "INBOUND"
    },

    # 2. Divisi Bumbu Racik
    "opr_bumbu": {
        "username": "opr_bumbu",
        "password": DEFAULT_BCRYPT_HASH,
        "operator_id": "OPR-BUMBU",
        "full_name": "Agus Setiawan",
        "role": "OPERATOR",
        "division": "Divisi Bumbu Racik",
        "default_stage": "PROD_1",
        "default_station": "SEASONING",
        "default_prod2_tab": "INBOUND"
    },
    "pic_bumbu": {
        "username": "pic_bumbu",
        "password": DEFAULT_BCRYPT_HASH,
        "operator_id": "PIC-BUMBU",
        "full_name": "Pak Ilham (PIC Bumbu)",
        "role": "STAKEHOLDER",
        "division": "Divisi Bumbu Racik",
        "default_stage": "PROD_1",
        "default_station": "SEASONING",
        "default_prod2_tab": "INBOUND"
    },

    # 3. Divisi Kulit Pangsit
    "opr_kulit": {
        "username": "opr_kulit",
        "password": DEFAULT_BCRYPT_HASH,
        "operator_id": "OPR-KULIT",
        "full_name": "Siti Rahma",
        "role": "OPERATOR",
        "division": "Divisi Kulit Pangsit",
        "default_stage": "PROD_1",
        "default_station": "WRAPPER",
        "default_prod2_tab": "INBOUND"
    },
    "pic_kulit": {
        "username": "pic_kulit",
        "password": DEFAULT_BCRYPT_HASH,
        "operator_id": "PIC-KULIT",
        "full_name": "Ibu Ratna (PIC Kulit)",
        "role": "STAKEHOLDER",
        "division": "Divisi Kulit Pangsit",
        "default_stage": "PROD_1",
        "default_station": "WRAPPER",
        "default_prod2_tab": "INBOUND"
    },

    # 4. Divisi Saos Dimsum
    "opr_saos": {
        "username": "opr_saos",
        "password": DEFAULT_BCRYPT_HASH,
        "operator_id": "OPR-SAOS",
        "full_name": "Rian Hidayat",
        "role": "OPERATOR",
        "division": "Divisi Saos Dimsum",
        "default_stage": "PROD_1",
        "default_station": "SAUCE",
        "default_prod2_tab": "INBOUND"
    },
    "pic_saos": {
        "username": "pic_saos",
        "password": DEFAULT_BCRYPT_HASH,
        "operator_id": "PIC-SAOS",
        "full_name": "Pak Anwar (PIC Saos)",
        "role": "STAKEHOLDER",
        "division": "Divisi Saos Dimsum",
        "default_stage": "PROD_1",
        "default_station": "SAUCE",
        "default_prod2_tab": "INBOUND"
    },

    # 5. Produksi 2: Cetak & Assembly
    "opr_cetak": {
        "username": "opr_cetak",
        "password": DEFAULT_BCRYPT_HASH,
        "operator_id": "OPR-CETAK",
        "full_name": "Yanto Subekti",
        "role": "OPERATOR",
        "division": "Produksi 2: Cetak & Assembly",
        "default_stage": "PROD_2",
        "default_station": "ASSEMBLY",
        "default_prod2_tab": "INBOUND"
    },
    "pic_cetak": {
        "username": "pic_cetak",
        "password": DEFAULT_BCRYPT_HASH,
        "operator_id": "PIC-CETAK",
        "full_name": "Pak Bowo (PIC Cetak)",
        "role": "STAKEHOLDER",
        "division": "Produksi 2: Cetak & Assembly",
        "default_stage": "PROD_2",
        "default_station": "ASSEMBLY",
        "default_prod2_tab": "INBOUND"
    },

    # 6. Produksi 2: Packing & Checker
    "opr_packing": {
        "username": "opr_packing",
        "password": DEFAULT_BCRYPT_HASH,
        "operator_id": "OPR-PACKING",
        "full_name": "Dewi Lestari",
        "role": "OPERATOR",
        "division": "Produksi 2: Packing & Checker",
        "default_stage": "PROD_2",
        "default_station": "PACKING",
        "default_prod2_tab": "PACKING"
    },
    "pic_packing": {
        "username": "pic_packing",
        "password": DEFAULT_BCRYPT_HASH,
        "operator_id": "PIC-PACKING",
        "full_name": "Ibu Maya (PIC Packing)",
        "role": "STAKEHOLDER",
        "division": "Produksi 2: Packing & Checker",
        "default_stage": "PROD_2",
        "default_station": "PACKING",
        "default_prod2_tab": "PACKING"
    },

    # 7. Gudang Freezer & Sales (POS)
    "opr_gudang": {
        "username": "opr_gudang",
        "password": DEFAULT_BCRYPT_HASH,
        "operator_id": "OPR-GUDANG",
        "full_name": "Hadi Prasetyo",
        "role": "OPERATOR",
        "division": "Gudang Freezer & Sales (POS)",
        "default_stage": "PROD_2",
        "default_station": "SALES",
        "default_prod2_tab": "SALES"
    },
    "pic_gudang": {
        "username": "pic_gudang",
        "password": DEFAULT_BCRYPT_HASH,
        "operator_id": "PIC-GUDANG",
        "full_name": "Pak Arif (PIC Gudang)",
        "role": "STAKEHOLDER",
        "division": "Gudang Freezer & Sales (POS)",
        "default_stage": "PROD_2",
        "default_station": "SALES",
        "default_prod2_tab": "SALES"
    },

    # 8. Supervisor Global & PPIC
    "ppic_ayu": {
        "username": "ppic_ayu",
        "password": DEFAULT_BCRYPT_HASH,
        "operator_id": "PPIC-AYU",
        "full_name": "Ayu (PPIC & Cetak Barcode)",
        "role": "STAKEHOLDER",
        "division": "PPIC / Administrasi Produksi",
        "default_stage": "PROD_1",
        "default_station": "PLAN",
        "default_prod2_tab": "PRODUCTS"
    },
    "supervisor": {
        "username": "supervisor",
        "password": DEFAULT_BCRYPT_HASH,
        "operator_id": "STK-YOSI",
        "full_name": "Pak Yosi",
        "role": "STAKEHOLDER",
        "division": "Supervisor & PPIC",
        "default_stage": "PROD_1",
        "default_station": "PLAN",
        "default_prod2_tab": "PRODUCTS"
    }
}


def load_persisted_credentials() -> None:
    """Muat hash password persisten yang telah diubah pengguna dari file credentials.json."""
    if os.path.exists(CREDENTIALS_FILE):
        try:
            with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                for uname, pwd_hash in saved.items():
                    if uname in ACCOUNTS:
                        ACCOUNTS[uname]["password"] = pwd_hash
        except Exception as e:
            print(f"[AUTH] Peringatan: Gagal memuat kredensial persisten: {e}")


def update_account_password(username: str, new_hashed_password: str) -> None:
    """Simpan perubahan password akun ke memori aktif dan persistensi file JSON."""
    if username in ACCOUNTS:
        ACCOUNTS[username]["password"] = new_hashed_password
    try:
        os.makedirs(os.path.dirname(CREDENTIALS_FILE), exist_ok=True)
        current = {}
        if os.path.exists(CREDENTIALS_FILE):
            try:
                with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
                    current = json.load(f)
            except Exception:
                current = {}
        current[username] = new_hashed_password
        with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
    except Exception as e:
        print(f"[AUTH] Peringatan: Gagal menyimpan kredensial ke disk: {e}")


# Inisialisasi kredensial saat modul dimuat
load_persisted_credentials()


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def b64url_decode(s: str) -> bytes:
    padding = 4 - (len(s) % 4)
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s.encode("utf-8"))


def create_token(user_data: Dict[str, Any], expires_in_seconds: int = 86400 * 30) -> str:
    payload = {
        "sub": user_data["username"],
        "operator_id": user_data["operator_id"],
        "full_name": user_data["full_name"],
        "role": user_data["role"],
        "division": user_data["division"],
        "exp": int(time.time()) + expires_in_seconds
    }
    header_json = json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8")
    payload_json = json.dumps(payload).encode("utf-8")

    h_b64 = b64url_encode(header_json)
    p_b64 = b64url_encode(payload_json)
    signature_data = f"{h_b64}.{p_b64}".encode("utf-8")
    sig = hmac.new(JWT_SECRET, signature_data, hashlib.sha256).digest()
    sig_b64 = b64url_encode(sig)

    return f"{h_b64}.{p_b64}.{sig_b64}"


def verify_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        h_b64, p_b64, sig_b64 = parts
        signature_data = f"{h_b64}.{p_b64}".encode("utf-8")
        expected_sig = hmac.new(JWT_SECRET, signature_data, hashlib.sha256).digest()
        actual_sig = b64url_decode(sig_b64)

        if not hmac.compare_digest(expected_sig, actual_sig):
            return None

        payload = json.loads(b64url_decode(p_b64).decode("utf-8"))
        if payload.get("exp", 0) < time.time():
            return None

        return payload
    except Exception:
        return None


# Security Header Schemes
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    api_key: Optional[str] = Security(api_key_header),
    credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme)
) -> Dict[str, Any]:
    # 1. Cek Token Bearer jika ada
    if credentials and credentials.credentials:
        payload = verify_token(credentials.credentials)
        if payload:
            return payload

    # 2. Cek API Key jika ada
    if api_key and api_key == API_KEY:
        return {
            "sub": "api_client",
            "operator_id": "SYSTEM_API",
            "full_name": "Automated Client",
            "role": "STAKEHOLDER",
            "division": "System API"
        }

    # Jika keduanya tidak valid
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Autentikasi gagal: Sertakan Bearer Token yang valid atau X-API-Key yang sesuai.",
        headers={"WWW-Authenticate": "Bearer"}
    )


def require_role(allowed_roles: list):
    async def role_checker(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
        if current_user.get("role") not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Akses ditolak: Operasi ini membutuhkan peran {', '.join(allowed_roles)}."
            )
        return current_user
    return role_checker


def is_global_supervisor(user: Dict[str, Any]) -> bool:
    sub = (user.get("sub") or user.get("username") or "").lower()
    op_id = (user.get("operator_id") or "").upper()
    div = (user.get("division") or "").upper()
    return (
        sub in ["supervisor", "ppic_ayu", "ayu"]
        or "YOSI" in op_id
        or "AYU" in op_id
        or "PPIC" in div
        or "SUPERVISOR" in div
    )


def require_ppic():
    """Hanya Ayu atau Supervisor Global / PPIC yang berwenang menerbitkan barcode dan merencanakan produksi."""
    async def ppic_checker(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
        if not is_global_supervisor(current_user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Akses ditolak: Hanya PPIC (Ayu) yang berhak menerbitkan barcode dan mengelola rencana."
            )
        return current_user
    return ppic_checker


def require_division(allowed_keywords: list):
    """
    Validasi akses divisi:
    Supervisor Global / PPIC diizinkan di seluruh divisi.
    Operator dan PIC Divisi hanya diizinkan untuk divisi yang sesuai.
    """
    async def division_checker(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
        if is_global_supervisor(current_user):
            return current_user

        op_id = (current_user.get("operator_id") or "").upper()
        div = (current_user.get("division") or "").upper()
        sub = (current_user.get("sub") or "").upper()
        if not any(k.upper() in op_id or k.upper() in div or k.upper() in sub for k in allowed_keywords):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Akses ditolak: Operasi ini hanya untuk divisi {', '.join(allowed_keywords)}."
            )
        return current_user
    return division_checker
