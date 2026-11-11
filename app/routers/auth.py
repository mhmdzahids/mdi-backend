import os
from typing import List, Dict, Any, Optional
from datetime import datetime
from fastapi import APIRouter, HTTPException, status, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..database import get_db
from ..models import User, Role
from ..auth import (
    ACCOUNTS, create_token, get_current_user, verify_password,
    hash_password, update_account_password
)
from ..schemas import (
    LoginRequest, QuickSwitchRequest, TokenResponse,
    AuthUserResponse, ChangePasswordRequest
)
from ..rbac import (
    get_effective_user_permissions, check_login_rate_limit,
    record_login_failure, reset_login_failures, invalidate_user_permission_cache
)
from ..audit_logger import log_audit_event, extract_client_ip

router = APIRouter(prefix="/auth", tags=["Autentikasi & Akun Pegawai"])


async def _resolve_user_response(
    user_db: Optional[User],
    fallback_acc: Optional[Dict[str, Any]],
    db: AsyncSession
) -> Dict[str, Any]:
    """Helper untuk menyusun dictionary profil user lengkap dengan role dan permissions."""
    if user_db:
        roles_list = [r.name for r in user_db.roles]
        perms = await get_effective_user_permissions(user_db.id, user_db.username, db)
        return {
            "id": user_db.id,
            "username": user_db.username,
            "operator_id": user_db.operator_id or "OPR",
            "full_name": user_db.full_name,
            "role": roles_list[0] if roles_list else "OPERATOR",
            "division": user_db.division or "",
            "default_stage": user_db.default_stage or "PROD_1",
            "default_station": user_db.default_station or "MEAT",
            "default_prod2_tab": user_db.default_prod2_tab or "INBOUND",
            "created_at": user_db.created_at.isoformat() if user_db.created_at else None,
            "is_active": user_db.is_active,
            "is_superadmin": user_db.is_superadmin,
            "must_change_password": user_db.must_change_password,
            "roles": roles_list,
            "permissions": sorted(list(perms))
        }

    # Fallback jika hanya ada di dictionary ACCOUNTS
    username = fallback_acc["username"]
    role_name = fallback_acc.get("role", "OPERATOR")
    return {
        "id": None,
        "username": username,
        "operator_id": fallback_acc.get("operator_id", "OPR"),
        "full_name": fallback_acc.get("full_name", username),
        "role": role_name,
        "division": fallback_acc.get("division", ""),
        "default_stage": fallback_acc.get("default_stage", "PROD_1"),
        "default_station": fallback_acc.get("default_station", "MEAT"),
        "default_prod2_tab": fallback_acc.get("default_prod2_tab", "INBOUND"),
        "created_at": fallback_acc.get("created_at"),
        "is_active": True,
        "is_superadmin": False,
        "must_change_password": False,
        "roles": [role_name],
        "permissions": []
    }


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    Login dengan username dan password:
    - Proteksi rate limiting brute force (max 5 kegagalan per 5 menit).
    - Verifikasi bcrypt password aman terstandardisasi industri.
    - Catat login sukses dan gagal ke Audit Log.
    - Mengembalikan token JWT berisi flag is_superadmin dan must_change_password.
    """
    client_ip = extract_client_ip(request) or "127.0.0.1"
    rate_limit_key = f"{client_ip}:{payload.username.lower()}"

    # 1. Cek rate limiting
    check_login_rate_limit(rate_limit_key)

    # 2. Cari akun di database SQL
    stmt = (
        select(User)
        .options(selectinload(User.roles).selectinload(Role.permissions))
        .where(User.username == payload.username)
    )
    res = await db.execute(stmt)
    user_db = res.scalar_one_or_none()

    fallback_acc = ACCOUNTS.get(payload.username)

    if not user_db and not fallback_acc:
        record_login_failure(rate_limit_key)
        await log_audit_event(
            db=db,
            aksi="LOGIN",
            objek_tipe="AUTH",
            objek_id=payload.username,
            ringkasan=f"Percobaan login gagal: Akun '{payload.username}' tidak ditemukan.",
            nama_user=payload.username,
            request=request,
            hasil="DENIED"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Username atau password salah."
        )

    # Cek verifikasi password
    stored_hash = user_db.password_hash if user_db else fallback_acc["password"]
    if not verify_password(payload.password, stored_hash):
        record_login_failure(rate_limit_key)
        await log_audit_event(
            db=db,
            aksi="LOGIN",
            objek_tipe="AUTH",
            objek_id=payload.username,
            ringkasan=f"Percobaan login gagal: Password salah untuk pengguna '{payload.username}'.",
            user_id=user_db.id if user_db else None,
            nama_user=payload.username,
            request=request,
            hasil="DENIED"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Username atau password salah."
        )

    # Cek status aktif
    if user_db and not user_db.is_active:
        await log_audit_event(
            db=db,
            aksi="LOGIN",
            objek_tipe="AUTH",
            objek_id=payload.username,
            ringkasan=f"Login ditolak: Akun '{payload.username}' dinonaktifkan.",
            user_id=user_db.id,
            nama_user=payload.username,
            request=request,
            hasil="DENIED"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akun Anda dinonaktifkan oleh Administrator. Hubungi Super Admin."
        )

    # Login Berhasil: Reset kegagalan dan perbarui last_login_at
    reset_login_failures(rate_limit_key)
    if user_db:
        user_db.last_login_at = datetime.utcnow()
        await db.commit()

    user_info = await _resolve_user_response(user_db, fallback_acc, db)

    await log_audit_event(
        db=db,
        aksi="LOGIN",
        objek_tipe="AUTH",
        objek_id=payload.username,
        ringkasan=f"Login berhasil untuk pengguna '{payload.username}' ({user_info.get('full_name')}).",
        user_id=user_info.get("id"),
        nama_user=payload.username,
        request=request,
        hasil="SUCCESS"
    )

    token = create_token(user_info)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": user_info
    }


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Ubah password akun pengguna yang sedang login.
    Memerlukan validasi password saat ini, konfirmasi password baru,
    serta hashing standar industri (bcrypt).
    """
    username = current_user.get("sub") or current_user.get("username")
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sesi login tidak valid."
        )

    # 1. Cari user di DB SQL
    stmt = select(User).where(User.username == username)
    res = await db.execute(stmt)
    user_db = res.scalar_one_or_none()

    fallback_acc = ACCOUNTS.get(username)
    stored_hash = user_db.password_hash if user_db else (fallback_acc.get("password") if fallback_acc else None)

    if not stored_hash:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Akun pengguna tidak ditemukan."
        )

    # 2. Verifikasi kecocokan password saat ini
    if not verify_password(payload.current_password, stored_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password saat ini salah."
        )

    # 3. Verifikasi konfirmasi kecocokan password baru
    if payload.new_password != payload.confirm_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Konfirmasi password baru tidak cocok."
        )

    # 4. Validasi panjang password baru
    if len(payload.new_password.strip()) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password baru minimal 6 karakter."
        )

    # 5. Validasi password baru tidak boleh sama persis dengan password saat ini
    if verify_password(payload.new_password, stored_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password baru tidak boleh sama dengan password saat ini."
        )

    # 6. Hash password baru dengan bcrypt dan simpan
    hashed = hash_password(payload.new_password)

    if user_db:
        user_db.password_hash = hashed
        user_db.must_change_password = False
        user_db.updated_at = datetime.utcnow()
        await db.commit()

    # Sinkronisasi ke disk kredensial fallback jika user legacy
    update_account_password(username, hashed)

    await log_audit_event(
        db=db,
        aksi="PASSWORD_CHANGE",
        objek_tipe="USER",
        objek_id=username,
        ringkasan=f"Pengguna '{username}' berhasil mengubah password sendiri.",
        user_id=user_db.id if user_db else None,
        nama_user=username,
        request=request,
        hasil="SUCCESS"
    )

    return {
        "status": "success",
        "message": "Password berhasil diperbarui. Silakan gunakan password baru untuk login berikutnya."
    }


@router.post("/quick-switch", response_model=TokenResponse)
async def quick_switch_account(
    payload: QuickSwitchRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Dev Mode / Quick Switcher:
    Ganti akun divisi secara instan tanpa input password.
    """
    if os.getenv("ENVIRONMENT", "").lower() == "production":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Quick Switcher dinonaktifkan pada lingkungan produksi."
        )

    stmt = (
        select(User)
        .options(selectinload(User.roles).selectinload(Role.permissions))
        .where(User.username == payload.username)
    )
    res = await db.execute(stmt)
    user_db = res.scalar_one_or_none()
    fallback_acc = ACCOUNTS.get(payload.username)

    if not user_db and not fallback_acc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Akun '{payload.username}' tidak ditemukan."
        )

    user_info = await _resolve_user_response(user_db, fallback_acc, db)
    token = create_token(user_info)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": user_info
    }


@router.get("/me", response_model=Dict[str, Any])
async def get_my_profile(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Ambil profil pengguna yang sedang login beserta:
    - Daftar izin akses efektif (permissions)
    - Daftar peran aktif (roles)
    - Flag is_superadmin & must_change_password
    """
    username = current_user.get("sub") or current_user.get("username")
    user_id = current_user.get("user_id")

    user_db = None
    if user_id:
        stmt = (
            select(User)
            .options(selectinload(User.roles).selectinload(Role.permissions))
            .where(User.id == user_id)
        )
        res = await db.execute(stmt)
        user_db = res.scalar_one_or_none()
    elif username:
        stmt = (
            select(User)
            .options(selectinload(User.roles).selectinload(Role.permissions))
            .where(User.username == username)
        )
        res = await db.execute(stmt)
        user_db = res.scalar_one_or_none()

    fallback_acc = ACCOUNTS.get(username) if username else None
    if user_db or fallback_acc:
        return await _resolve_user_response(user_db, fallback_acc, db)

    return current_user


@router.get("/accounts", response_model=List[AuthUserResponse])
async def list_available_accounts(db: AsyncSession = Depends(get_db)):
    """
    Daftar akun pegawai seluruh divisi & PIC
    untuk kebutuhan Dev Mode switcher dan demo.
    """
    # 1. Coba ambil dari DB SQL
    stmt = (
        select(User)
        .options(selectinload(User.roles).selectinload(Role.permissions))
        .where(User.is_active == True)
        .order_by(User.id.asc())
    )
    res = await db.execute(stmt)
    users_db = res.scalars().all()

    if users_db:
        result = []
        for u in users_db:
            u_info = await _resolve_user_response(u, None, db)
            result.append(u_info)
        return result

    # 2. Fallback jika DB kosong
    accounts_clean = []
    for acc in ACCOUNTS.values():
        accounts_clean.append({
            "id": None,
            "username": acc["username"],
            "operator_id": acc["operator_id"],
            "full_name": acc["full_name"],
            "role": acc["role"],
            "division": acc["division"],
            "default_stage": acc.get("default_stage", "PROD_1"),
            "default_station": acc.get("default_station", "MEAT"),
            "default_prod2_tab": acc.get("default_prod2_tab", "INBOUND"),
            "created_at": acc.get("created_at"),
            "is_active": True,
            "is_superadmin": False,
            "must_change_password": False,
            "roles": [acc["role"]],
            "permissions": []
        })
    return accounts_clean
