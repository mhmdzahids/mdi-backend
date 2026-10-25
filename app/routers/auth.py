import os
from typing import List, Dict, Any
from fastapi import APIRouter, HTTPException, status, Depends
from ..auth import ACCOUNTS, create_token, get_current_user
from ..schemas import LoginRequest, QuickSwitchRequest, TokenResponse, AuthUserResponse

router = APIRouter(prefix="/auth", tags=["Autentikasi & Akun Pegawai"])


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest):
    """Login dengan username dan password."""
    user = ACCOUNTS.get(payload.username)
    if not user or user["password"] != payload.password:
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
            "default_prod2_tab": acc.get("default_prod2_tab", "INBOUND")
        })
    return accounts_clean
