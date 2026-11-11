"""
RBAC Core Service & Enforcement Engine - Dimsum MES
Mengatur perizinan akses dinamis berbasis peran (Role-Based Access Control),
caching jangka pendek untuk performa, rate limiter login, dan dependency require_permission.
"""

import time
from typing import Set, Dict, Any, Optional, List
from fastapi import Request, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .database import get_db
from .models import User, Role, Permission
from .auth import get_current_user
from .permissions_registry import ALL_PERMISSION_CODES
from .audit_logger import log_audit_event, extract_client_ip

# Cache perizinan in-memory jangka pendek (TTL: 30 detik)
# user_id -> {"permissions": Set[str], "expires_at": float}
_PERMISSIONS_CACHE: Dict[int, Dict[str, Any]] = {}
CACHE_TTL_SECONDS = 30

# In-memory Rate Limiting untuk proteksi brute force login
# key -> list of float timestamps
_LOGIN_FAILURES: Dict[str, List[float]] = {}
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_WINDOW_SECONDS = 300  # 5 menit


def invalidate_user_permission_cache(user_id: Optional[int] = None) -> None:
    """Hapus cache izin untuk satu user tertentu atau seluruh user (jika None)."""
    global _PERMISSIONS_CACHE
    if user_id is not None:
        _PERMISSIONS_CACHE.pop(user_id, None)
    else:
        _PERMISSIONS_CACHE.clear()


async def get_effective_user_permissions(
    user_id: Optional[int],
    username: Optional[str],
    db: AsyncSession
) -> Set[str]:
    """
    Ambil himpunan izin efektif untuk akun pengguna.
    Hasil gabungan dari semua role aktif pengguna.
    Menggunakan cache lokal 30 detik untuk menghindari query berulang ke database.
    """
    now = time.time()

    # 1. Cek cache in-memory jika user_id ada
    if user_id and user_id in _PERMISSIONS_CACHE:
        cached = _PERMISSIONS_CACHE[user_id]
        if cached["expires_at"] > now:
            return cached["permissions"]

    # 2. Query dari database
    user = None
    if user_id:
        stmt = (
            select(User)
            .options(
                selectinload(User.roles).selectinload(Role.permissions)
            )
            .where(User.id == user_id)
        )
        res = await db.execute(stmt)
        user = res.scalar_one_or_none()
    elif username:
        stmt = (
            select(User)
            .options(
                selectinload(User.roles).selectinload(Role.permissions)
            )
            .where(User.username == username)
        )
        res = await db.execute(stmt)
        user = res.scalar_one_or_none()

    if not user:
        return set()

    # 3. Jika pengguna adalah Super Admin, berikan SEMUA izin tanpa terkecuali
    if user.is_superadmin:
        effective = set(ALL_PERMISSION_CODES)
    else:
        effective = set()
        for role in user.roles:
            for perm in role.permissions:
                effective.add(perm.code)

    # 4. Simpan ke cache
    if user.id:
        _PERMISSIONS_CACHE[user.id] = {
            "permissions": effective,
            "expires_at": now + CACHE_TTL_SECONDS
        }

    return effective


def require_permission(permission_code: str):
    """
    FastAPI Dependency:
    Menegakkan otorisasi ketat berbasis izin (Deny by default).
    Jika pengguna tidak memiliki hak akses, otomatis dicatat ke Audit Log (ACCESS_DENIED)
    dan mengembalikan HTTP 403 Forbidden.
    """
    async def permission_checker(
        request: Request,
        current_user: Dict[str, Any] = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
    ) -> Dict[str, Any]:
        # A. Super Admin flag bypass (evaluasi tercepat)
        if current_user.get("is_superadmin") is True:
            return current_user

        # B. Pemeriksaan status akun aktif
        if current_user.get("is_active") is False:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Akses ditolak: Akun Anda telah dinonaktifkan oleh Administrator."
            )

        user_id = current_user.get("user_id")
        username = current_user.get("sub") or current_user.get("username")

        # C. Evaluasi izin efektif
        permissions = await get_effective_user_permissions(user_id, username, db)
        if permission_code not in permissions:
            # Catat upaya akses tidak sah ke audit log
            await log_audit_event(
                db=db,
                aksi="ACCESS_DENIED",
                objek_tipe="ENDPOINT",
                objek_id=permission_code,
                ringkasan=f"Akses ditolak untuk izin '{permission_code}' pada endpoint '{request.url.path}'",
                user_id=user_id,
                nama_user=username or "UNKNOWN",
                request=request,
                hasil="DENIED"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Akses ditolak: Anda tidak memiliki izin '{permission_code}'."
            )

        return current_user

    return permission_checker


# --- Rate Limiter Brute Force Protection ---

def check_login_rate_limit(client_identifier: str) -> None:
    """Periksa apakah IP/username telah melampaui batas percobaan login."""
    now = time.time()
    failures = _LOGIN_FAILURES.get(client_identifier, [])
    # Filter kegagalan hanya dalam jendela waktu LOCKOUT_WINDOW_SECONDS
    active_failures = [t for t in failures if (now - t) < LOCKOUT_WINDOW_SECONDS]
    _LOGIN_FAILURES[client_identifier] = active_failures

    if len(active_failures) >= MAX_FAILED_ATTEMPTS:
        time_remaining = int(LOCKOUT_WINDOW_SECONDS - (now - active_failures[0]))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Terlalu banyak percobaan login gagal. Akun/IP dikunci sementara. Silakan coba lagi dalam {max(time_remaining, 1)} detik."
        )


def record_login_failure(client_identifier: str) -> None:
    """Catat stempel waktu percobaan login yang gagal."""
    now = time.time()
    if client_identifier not in _LOGIN_FAILURES:
        _LOGIN_FAILURES[client_identifier] = []
    _LOGIN_FAILURES[client_identifier].append(now)


def reset_login_failures(client_identifier: str) -> None:
    """Bersihkan riwayat kegagalan login saat pengguna berhasil login."""
    _LOGIN_FAILURES.pop(client_identifier, None)
