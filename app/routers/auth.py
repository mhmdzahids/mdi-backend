import os
from typing import List, Dict, Any
from fastapi import APIRouter, HTTPException, status, Depends
from ..auth import ACCOUNTS, create_token, get_current_user, verify_password, hash_password, update_account_password
from ..schemas import LoginRequest, QuickSwitchRequest, TokenResponse, AuthUserResponse, ChangePasswordRequest

router = APIRouter(prefix="/auth", tags=["Autentikasi & Akun Pegawai"])


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest):
    """Login dengan username dan password."""
    user = ACCOUNTS.get(payload.username)
    if not user or not verify_password(payload.password, user["password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Username atau password salah."
        )

    token = create_token(user)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": user
    }


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Ubah password akun pengguna yang sedang login.
    Memerlukan validasi password saat ini, konfirmasi password baru,
    serta hashing standar industri (bcrypt).
    """
    username = current_user.get("sub") or current_user.get("username")
    if not username or username not in ACCOUNTS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Akun pengguna tidak ditemukan."
        )

    user = ACCOUNTS[username]

    # 1. Verifikasi kecocokan password saat ini
    if not verify_password(payload.current_password, user["password"]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password saat ini salah."
        )

    # 2. Verifikasi konfirmasi kecocokan password baru
    if payload.new_password != payload.confirm_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Konfirmasi password baru tidak cocok."
        )

    # 3. Validasi panjang password baru
    if len(payload.new_password.strip()) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password baru minimal 6 karakter."
        )

    # 4. Validasi password baru tidak boleh sama persis dengan password saat ini
    if verify_password(payload.new_password, user["password"]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password baru tidak boleh sama dengan password saat ini."
        )

    # 5. Hash password baru dengan bcrypt dan simpan
    hashed = hash_password(payload.new_password)
    update_account_password(username, hashed)

    return {
        "status": "success",
        "message": "Password berhasil diperbarui. Silakan gunakan password baru untuk login berikutnya."
    }


@router.post("/quick-switch", response_model=TokenResponse)
async def quick_switch_account(payload: QuickSwitchRequest):
    """
    Dev Mode / Quick Switcher:
    Ganti akun divisi secara instan tanpa input password.
    """
    if os.getenv("ENVIRONMENT", "").lower() == "production":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Quick Switcher dinonaktifkan pada lingkungan produksi."
        )

    user = ACCOUNTS.get(payload.username)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Akun '{payload.username}' tidak ditemukan."
        )

    token = create_token(user)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": user
    }


@router.get("/me", response_model=Dict[str, Any])
async def get_my_profile(current_user: Dict[str, Any] = Depends(get_current_user)):
    """Ambil informasi profil pengguna yang sedang login."""
    username = current_user.get("sub") or current_user.get("username")
    user = ACCOUNTS.get(username) if username else None
    if user:
        return {
            "username": user["username"],
            "operator_id": user["operator_id"],
            "full_name": user["full_name"],
            "role": user["role"],
            "division": user["division"],
            "default_stage": user.get("default_stage", "PROD_1"),
            "default_station": user.get("default_station", "MEAT"),
            "default_prod2_tab": user.get("default_prod2_tab", "INBOUND"),
            "created_at": user.get("created_at")
        }
    return current_user


@router.get("/accounts", response_model=List[AuthUserResponse])
async def list_available_accounts():
    """
    Daftar akun pegawai seluruh divisi & PIC
    untuk kebutuhan Dev Mode switcher dan demo.
    """
    accounts_clean = []
    for acc in ACCOUNTS.values():
        accounts_clean.append({
            "username": acc["username"],
            "operator_id": acc["operator_id"],
            "full_name": acc["full_name"],
            "role": acc["role"],
            "division": acc["division"],
            "default_stage": acc.get("default_stage", "PROD_1"),
            "default_station": acc.get("default_station", "MEAT"),
            "default_prod2_tab": acc.get("default_prod2_tab", "INBOUND"),
            "created_at": acc.get("created_at")
        })
    return accounts_clean
