"""
Super Admin & RBAC Management Router - Dimsum MES
Mengelola akun pengguna, hak akses berbasis peran (RBAC),
proteksi perlindungan sistem (safeguards), dan penelusuran audit aktivitas.
"""

from typing import List, Dict, Any, Optional
from datetime import datetime, date
from fastapi import APIRouter, HTTPException, status, Depends, Request, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from sqlalchemy.orm import selectinload

from ..database import get_db
from ..models import User, Role, Permission, AuditLog
from ..auth import get_current_user, hash_password
from ..rbac import require_permission, invalidate_user_permission_cache
from ..audit_logger import log_audit_event
from ..permissions_registry import PERMISSIONS, ALL_PERMISSION_CODES
from ..schemas import (
    UserAdminResponse, UserCreateRequest, UserUpdateRequest,
    UserStatusUpdateRequest, UserResetPasswordRequest,
    RoleResponse, RoleCreateRequest, RoleUpdateRequest,
    PermissionResponse, AuditLogResponse
)

router = APIRouter(prefix="/admin", tags=["Super Admin & RBAC"])


# =========================================================================
# 1. PENGELOLAAN PENGGUNA (USERS)
# =========================================================================

@router.get("/users", response_model=List[UserAdminResponse])
async def list_users(
    db: AsyncSession = Depends(get_db),
    _auth: Dict[str, Any] = Depends(require_permission("admin_users:view"))
):
    """Melihat daftar seluruh akun pengguna beserta peran dan status aktif."""
    stmt = (
        select(User)
        .options(selectinload(User.roles))
        .order_by(User.id.asc())
    )
    res = await db.execute(stmt)
    users = res.scalars().all()

    result = []
    for u in users:
        result.append(UserAdminResponse(
            id=u.id,
            username=u.username,
            operator_id=u.operator_id,
            full_name=u.full_name,
            division=u.division,
            default_stage=u.default_stage or "PROD_1",
            default_station=u.default_station or "MEAT",
            default_prod2_tab=u.default_prod2_tab or "INBOUND",
            is_active=u.is_active,
            is_superadmin=u.is_superadmin,
            must_change_password=u.must_change_password,
            last_login_at=u.last_login_at,
            created_at=u.created_at,
            roles=[r.name for r in u.roles]
        ))
    return result


@router.post("/users", response_model=UserAdminResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_permission("admin_users:create"))
):
    """Menambahkan akun pegawai baru dengan password yang di-hash bcrypt terstandardisasi."""
    # 1. Cek keunikan username
    stmt_check = select(User).where(User.username == payload.username.strip().lower())
    res_check = await db.execute(stmt_check)
    if res_check.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Username '{payload.username}' sudah digunakan."
        )

    # 3. Ambil role dari DB
    roles_to_assign = []
    if payload.role_ids:
        stmt_roles = select(Role).where(Role.id.in_(payload.role_ids))
        res_roles = await db.execute(stmt_roles)
        roles_to_assign = res_roles.scalars().all()

    # Sinkronisasi otomatis: Jika role Super Admin dipilih, set is_superadmin = True
    has_superadmin_role = any(r.name == "Super Admin" for r in roles_to_assign)
    effective_is_superadmin = payload.is_superadmin or has_superadmin_role

    # 2. Safeguard Super Admin: Hanya Super Admin yang boleh membuat Super Admin lain
    if effective_is_superadmin and not current_user.get("is_superadmin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Hanya Super Admin yang berhak memberikan hak istimewa Super Admin."
        )

    # 4. Hash password
    pw_hash = hash_password(payload.password)

    new_user = User(
        username=payload.username.strip().lower(),
        password_hash=pw_hash,
        operator_id=payload.operator_id,
        full_name=payload.full_name.strip(),
        division=payload.division,
        default_stage=payload.default_stage or "PROD_1",
        default_station=payload.default_station or "MEAT",
        default_prod2_tab=payload.default_prod2_tab or "INBOUND",
        is_active=payload.is_active,
        is_superadmin=effective_is_superadmin,
        must_change_password=payload.must_change_password,
        created_at=datetime.utcnow()
    )
    new_user.roles = list(roles_to_assign)

    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    # Log audit
    await log_audit_event(
        db=db,
        aksi="USER_CREATE",
        objek_tipe="USER",
        objek_id=str(new_user.id),
        ringkasan=f"Super Admin membuat akun baru: '{new_user.username}' ({new_user.full_name})",
        data_sesudah={
            "id": new_user.id,
            "username": new_user.username,
            "full_name": new_user.full_name,
            "is_superadmin": new_user.is_superadmin,
            "roles": [r.name for r in new_user.roles]
        },
        user_id=current_user.get("user_id"),
        nama_user=current_user.get("sub") or current_user.get("username"),
        request=request,
        hasil="SUCCESS"
    )

    return UserAdminResponse(
        id=new_user.id,
        username=new_user.username,
        operator_id=new_user.operator_id,
        full_name=new_user.full_name,
        division=new_user.division,
        default_stage=new_user.default_stage,
        default_station=new_user.default_station,
        default_prod2_tab=new_user.default_prod2_tab,
        is_active=new_user.is_active,
        is_superadmin=new_user.is_superadmin,
        must_change_password=new_user.must_change_password,
        last_login_at=new_user.last_login_at,
        created_at=new_user.created_at,
        roles=[r.name for r in new_user.roles]
    )


@router.put("/users/{user_id}", response_model=UserAdminResponse)
async def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_permission("admin_users:update"))
):
    """
    Mengubah profil, status, peran, atau flag superadmin pengguna.
    Dilengkapi safeguard ketat: tidak dapat demote/nonaktifkan diri sendiri atau super admin terakhir.
    """
    stmt = (
        select(User)
        .options(selectinload(User.roles))
        .where(User.id == user_id)
    )
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pengguna dengan ID {user_id} tidak ditemukan."
        )

    caller_id = current_user.get("user_id")

    # Snapshot sebelum diubah
    data_before = {
        "full_name": user.full_name,
        "is_active": user.is_active,
        "is_superadmin": user.is_superadmin,
        "division": user.division,
        "roles": [r.name for r in user.roles]
    }

    # Ambil role dari DB jika role_ids disediakan
    roles_to_assign = None
    if payload.role_ids is not None:
        stmt_roles = select(Role).where(Role.id.in_(payload.role_ids))
        res_roles = await db.execute(stmt_roles)
        roles_to_assign = list(res_roles.scalars().all())

    # Tentukan status is_superadmin yang ditargetkan
    target_is_superadmin = payload.is_superadmin
    if target_is_superadmin is None and roles_to_assign is not None:
        target_is_superadmin = any(r.name == "Super Admin" for r in roles_to_assign)
    elif roles_to_assign is not None and any(r.name == "Super Admin" for r in roles_to_assign):
        target_is_superadmin = True

    # SAFEGUARD 1: Tidak dapat menonaktifkan atau mencabut hak Super Admin akun sendiri
    if caller_id and caller_id == user.id:
        if payload.is_active is False:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Safeguard: Anda tidak dapat menonaktifkan akun Anda sendiri."
            )
        if target_is_superadmin is False and user.is_superadmin:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Safeguard: Anda tidak dapat mencabut hak Super Admin dari akun Anda sendiri."
            )

    # SAFEGUARD 2: Tidak dapat menonaktifkan atau demote Super Admin terakhir di sistem
    if user.is_superadmin and (payload.is_active is False or target_is_superadmin is False):
        count_stmt = select(func.count(User.id)).where(
            User.is_superadmin == True,
            User.is_active == True,
            User.id != user.id
        )
        res_count = await db.execute(count_stmt)
        other_active_sa = res_count.scalar() or 0
        if other_active_sa == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Safeguard: Tidak dapat mengubah status Super Admin terakhir dalam sistem."
            )

    # SAFEGUARD 3: Hanya Super Admin aktif yang boleh mengubah status is_superadmin
    if target_is_superadmin is not None and target_is_superadmin != user.is_superadmin:
        if not current_user.get("is_superadmin"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Hanya Super Admin yang berhak memberikan atau mencabut hak Super Admin."
            )
        user.is_superadmin = target_is_superadmin

    if payload.full_name is not None:
        user.full_name = payload.full_name.strip()
    if payload.operator_id is not None:
        user.operator_id = payload.operator_id
    if payload.division is not None:
        user.division = payload.division
    if payload.default_stage is not None:
        user.default_stage = payload.default_stage
    if payload.default_station is not None:
        user.default_station = payload.default_station
    if payload.default_prod2_tab is not None:
        user.default_prod2_tab = payload.default_prod2_tab
    if payload.is_active is not None:
        user.is_active = payload.is_active

    # Perbarui relasi role jika diberikan
    if roles_to_assign is not None:
        user.roles = roles_to_assign

    user.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(user)

    # Invalidate cache izin user
    invalidate_user_permission_cache(user.id)

    # Snapshot setelah diubah
    data_after = {
        "full_name": user.full_name,
        "is_active": user.is_active,
        "is_superadmin": user.is_superadmin,
        "division": user.division,
        "roles": [r.name for r in user.roles]
    }

    await log_audit_event(
        db=db,
        aksi="USER_UPDATE",
        objek_tipe="USER",
        objek_id=str(user.id),
        ringkasan=f"Super Admin memperbarui data pengguna '{user.username}'.",
        data_sebelum=data_before,
        data_sesudah=data_after,
        user_id=current_user.get("user_id"),
        nama_user=current_user.get("sub") or current_user.get("username"),
        request=request,
        hasil="SUCCESS"
    )

    return UserAdminResponse(
        id=user.id,
        username=user.username,
        operator_id=user.operator_id,
        full_name=user.full_name,
        division=user.division,
        default_stage=user.default_stage,
        default_station=user.default_station,
        default_prod2_tab=user.default_prod2_tab,
        is_active=user.is_active,
        is_superadmin=user.is_superadmin,
        must_change_password=user.must_change_password,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
        roles=[r.name for r in user.roles]
    )


@router.post("/users/{user_id}/status", response_model=UserAdminResponse)
async def toggle_user_status(
    user_id: int,
    payload: UserStatusUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_permission("admin_users:update"))
):
    """Aktifkan atau nonaktifkan akun pengguna."""
    stmt = (
        select(User)
        .options(selectinload(User.roles))
        .where(User.id == user_id)
    )
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pengguna dengan ID {user_id} tidak ditemukan."
        )

    caller_id = current_user.get("user_id")

    # Safeguard jika menonaktifkan diri sendiri
    if caller_id and caller_id == user.id and not payload.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Safeguard: Anda tidak dapat menonaktifkan akun Anda sendiri."
        )

    # Safeguard jika menonaktifkan Super Admin terakhir
    if user.is_superadmin and not payload.is_active:
        count_stmt = select(func.count(User.id)).where(
            User.is_superadmin == True,
            User.is_active == True,
            User.id != user.id
        )
        res_count = await db.execute(count_stmt)
        if (res_count.scalar() or 0) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Safeguard: Tidak dapat menonaktifkan Super Admin terakhir dalam sistem."
            )

    old_status = user.is_active
    user.is_active = payload.is_active
    user.updated_at = datetime.utcnow()
    await db.commit()

    invalidate_user_permission_cache(user.id)

    status_str = "diaktifkan" if payload.is_active else "dinonaktifkan"
    await log_audit_event(
        db=db,
        aksi="USER_STATUS_CHANGE",
        objek_tipe="USER",
        objek_id=str(user.id),
        ringkasan=f"Akun pengguna '{user.username}' telah {status_str}.",
        data_sebelum={"is_active": old_status},
        data_sesudah={"is_active": user.is_active},
        user_id=current_user.get("user_id"),
        nama_user=current_user.get("sub") or current_user.get("username"),
        request=request,
        hasil="SUCCESS"
    )

    return UserAdminResponse(
        id=user.id,
        username=user.username,
        operator_id=user.operator_id,
        full_name=user.full_name,
        division=user.division,
        default_stage=user.default_stage,
        default_station=user.default_station,
        default_prod2_tab=user.default_prod2_tab,
        is_active=user.is_active,
        is_superadmin=user.is_superadmin,
        must_change_password=user.must_change_password,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
        roles=[r.name for r in user.roles]
    )


@router.post("/users/{user_id}/reset-password")
async def reset_user_password(
    user_id: int,
    payload: UserResetPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_permission("admin_users:update"))
):
    """Reset password akun pengguna oleh Super Admin (mengaktifkan must_change_password)."""
    stmt = select(User).where(User.id == user_id)
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pengguna dengan ID {user_id} tidak ditemukan."
        )

    new_hash = hash_password(payload.new_password)
    user.password_hash = new_hash
    user.must_change_password = payload.must_change_password
    user.updated_at = datetime.utcnow()
    await db.commit()

    await log_audit_event(
        db=db,
        aksi="PASSWORD_RESET",
        objek_tipe="USER",
        objek_id=str(user.id),
        ringkasan=f"Password akun '{user.username}' direset oleh Administrator (must_change_password={payload.must_change_password}).",
        user_id=current_user.get("user_id"),
        nama_user=current_user.get("sub") or current_user.get("username"),
        request=request,
        hasil="SUCCESS"
    )

    return {
        "status": "success",
        "message": f"Password untuk akun '{user.username}' berhasil direset."
    }


# =========================================================================
# 2. PENGELOLAAN PERAN (ROLES) & IZIN AKSES (PERMISSIONS)
# =========================================================================

@router.get("/roles", response_model=List[RoleResponse])
async def list_roles(
    db: AsyncSession = Depends(get_db),
    _auth: Dict[str, Any] = Depends(require_permission("admin_roles:view"))
):
    """Melihat daftar seluruh peran (roles), izin akses yang terpasang, dan jumlah pengguna."""
    stmt = (
        select(Role)
        .options(
            selectinload(Role.permissions),
            selectinload(Role.users)
        )
        .order_by(Role.id.asc())
    )
    res = await db.execute(stmt)
    roles = res.scalars().all()

    result = []
    for r in roles:
        result.append(RoleResponse(
            id=r.id,
            name=r.name,
            description=r.description,
            is_system=r.is_system,
            created_at=r.created_at,
            permission_count=len(r.permissions),
            permissions=[p.code for p in r.permissions],
            user_count=len(r.users)
        ))
    return result


@router.post("/roles", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
async def create_role(
    payload: RoleCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_permission("admin_roles:create"))
):
    """Membuat role baru kustom beserta paket izin aksesnya."""
    # 1. Cek keunikan nama role
    stmt_check = select(Role).where(Role.name == payload.name.strip())
    res_check = await db.execute(stmt_check)
    if res_check.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Role dengan nama '{payload.name}' sudah ada."
        )

    # 2. Ambil permissions dari DB
    perms_to_add = []
    if payload.permissions:
        stmt_perms = select(Permission).where(Permission.code.in_(payload.permissions))
        res_perms = await db.execute(stmt_perms)
        perms_to_add = res_perms.scalars().all()

    new_role = Role(
        name=payload.name.strip(),
        description=payload.description,
        is_system=False,  # Role buatan admin bukan role sistem permanen
        created_at=datetime.utcnow()
    )
    new_role.permissions = list(perms_to_add)

    db.add(new_role)
    await db.commit()
    await db.refresh(new_role)

    await log_audit_event(
        db=db,
        aksi="ROLE_CREATE",
        objek_tipe="ROLE",
        objek_id=str(new_role.id),
        ringkasan=f"Role baru '{new_role.name}' dibuat dengan {len(new_role.permissions)} izin akses.",
        data_sesudah={
            "id": new_role.id,
            "name": new_role.name,
            "permissions": [p.code for p in new_role.permissions]
        },
        user_id=current_user.get("user_id"),
        nama_user=current_user.get("sub") or current_user.get("username"),
        request=request,
        hasil="SUCCESS"
    )

    return RoleResponse(
        id=new_role.id,
        name=new_role.name,
        description=new_role.description,
        is_system=new_role.is_system,
        created_at=new_role.created_at,
        permission_count=len(new_role.permissions),
        permissions=[p.code for p in new_role.permissions],
        user_count=0
    )


@router.put("/roles/{role_id}", response_model=RoleResponse)
async def update_role(
    role_id: int,
    payload: RoleUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_permission("admin_roles:update"))
):
    """
    Mengubah nama, deskripsi, atau hak akses role.
    Role Super Admin terkunci dan tidak dapat diubah izin aksesnya.
    """
    stmt = (
        select(Role)
        .options(
            selectinload(Role.permissions),
            selectinload(Role.users)
        )
        .where(Role.id == role_id)
    )
    res = await db.execute(stmt)
    role = res.scalar_one_or_none()

    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Role dengan ID {role_id} tidak ditemukan."
        )

    # SAFEGUARD: Role "Super Admin" mutlak tidak dapat diubah
    if role.is_system and role.name == "Super Admin":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Safeguard: Role 'Super Admin' adalah role sistem fundamental dan tidak dapat dimodifikasi."
        )

    data_before = {
        "name": role.name,
        "description": role.description,
        "permissions": [p.code for p in role.permissions]
    }

    # Jika role sistem lain, nama role tidak boleh diubah untuk menjaga integritas logika
    if role.is_system and payload.name and payload.name.strip() != role.name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nama template role sistem bawaan tidak dapat diubah."
        )

    if payload.name and not role.is_system:
        # Cek keunikan
        stmt_check = select(Role).where(Role.name == payload.name.strip(), Role.id != role.id)
        res_check = await db.execute(stmt_check)
        if res_check.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Nama role '{payload.name}' sudah digunakan."
            )
        role.name = payload.name.strip()

    if payload.description is not None:
        role.description = payload.description

    if payload.permissions is not None:
        stmt_perms = select(Permission).where(Permission.code.in_(payload.permissions))
        res_perms = await db.execute(stmt_perms)
        role.permissions = list(res_perms.scalars().all())

    await db.commit()
    await db.refresh(role)

    # Invalidate cache izin seluruh user yang mungkin terdampak
    invalidate_user_permission_cache(None)

    data_after = {
        "name": role.name,
        "description": role.description,
        "permissions": [p.code for p in role.permissions]
    }

    await log_audit_event(
        db=db,
        aksi="ROLE_UPDATE",
        objek_tipe="ROLE",
        objek_id=str(role.id),
        ringkasan=f"Role '{role.name}' diperbarui oleh Administrator.",
        data_sebelum=data_before,
        data_sesudah=data_after,
        user_id=current_user.get("user_id"),
        nama_user=current_user.get("sub") or current_user.get("username"),
        request=request,
        hasil="SUCCESS"
    )

    return RoleResponse(
        id=role.id,
        name=role.name,
        description=role.description,
        is_system=role.is_system,
        created_at=role.created_at,
        permission_count=len(role.permissions),
        permissions=[p.code for p in role.permissions],
        user_count=len(role.users)
    )


@router.delete("/roles/{role_id}")
async def delete_role(
    role_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_permission("admin_roles:delete"))
):
    """Menghapus role kustom. Role sistem bawaan tidak dapat dihapus."""
    stmt = (
        select(Role)
        .options(selectinload(Role.users))
        .where(Role.id == role_id)
    )
    res = await db.execute(stmt)
    role = res.scalar_one_or_none()

    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Role dengan ID {role_id} tidak ditemukan."
        )

    # SAFEGUARD 1: Role sistem tidak dapat dihapus
    if role.is_system:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Safeguard: Template role sistem dilindungi dan tidak dapat dihapus."
        )

    # SAFEGUARD 2: Tidak dapat menghapus role yang masih dipakai akun aktif
    if len(role.users) > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Safeguard: Role '{role.name}' masih digunakan oleh {len(role.users)} pengguna. Pindahkan pengguna ke role lain terlebih dahulu."
        )

    role_name = role.name
    await db.delete(role)
    await db.commit()

    invalidate_user_permission_cache(None)

    await log_audit_event(
        db=db,
        aksi="ROLE_DELETE",
        objek_tipe="ROLE",
        objek_id=str(role_id),
        ringkasan=f"Role kustom '{role_name}' telah dihapus dari sistem.",
        user_id=current_user.get("user_id"),
        nama_user=current_user.get("sub") or current_user.get("username"),
        request=request,
        hasil="SUCCESS"
    )

    return {
        "status": "success",
        "message": f"Role '{role_name}' berhasil dihapus."
    }


@router.get("/permissions", response_model=List[PermissionResponse])
async def list_permissions(
    _auth: Dict[str, Any] = Depends(require_permission("admin_roles:view")),
    db: AsyncSession = Depends(get_db)
):
    """Melihat daftar seluruh izin akses (permissions) yang terdaftar dalam sistem."""
    stmt = select(Permission).order_by(Permission.feature.asc(), Permission.action.asc())
    res = await db.execute(stmt)
    perms = res.scalars().all()

    if perms:
        return perms

    # Fallback ke registry jika DB belum di-seed
    return [PermissionResponse(**p, id=idx + 1) for idx, p in enumerate(PERMISSIONS)]


# =========================================================================
# 3. PENELUSURAN AUDIT AKTIVITAS (AUDIT LOGS - IMMUTABLE / APPEND-ONLY)
# =========================================================================

@router.get("/audit-logs", response_model=Dict[str, Any])
async def list_audit_logs(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    action: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    user_id: Optional[int] = Query(default=None),
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _auth: Dict[str, Any] = Depends(require_permission("admin_audit_logs:view"))
):
    """
    Melihat riwayat log audit aktivitas sistem:
    Bersifat mutlak IMMUTABLE (hanya baca). Tidak disediakan endpoint update atau delete.
    """
    conditions = []
    if action:
        conditions.append(AuditLog.aksi == action.strip().upper())
    if status:
        conditions.append(AuditLog.hasil == status.strip().upper())
    if user_id:
        conditions.append(AuditLog.user_id == user_id)
    if start_date:
        conditions.append(AuditLog.waktu >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        conditions.append(AuditLog.waktu <= datetime.combine(end_date, datetime.max.time()))

    # Hitung total data
    count_stmt = select(func.count(AuditLog.id))
    if conditions:
        count_stmt = count_stmt.where(and_(*conditions))
    total_res = await db.execute(count_stmt)
    total_records = total_res.scalar() or 0

    # Query dengan paginasi
    offset = (page - 1) * limit
    stmt = (
        select(AuditLog)
        .order_by(AuditLog.waktu.desc())
        .offset(offset)
        .limit(limit)
    )
    if conditions:
        stmt = stmt.where(and_(*conditions))

    res = await db.execute(stmt)
    logs = res.scalars().all()

    logs_list = []
    for l in logs:
        logs_list.append(AuditLogResponse(
            id=l.id,
            waktu=l.waktu,
            user_id=l.user_id,
            nama_user=l.nama_user,
            aksi=l.aksi,
            objek_tipe=l.objek_tipe,
            objek_id=l.objek_id,
            ringkasan=l.ringkasan,
            data_sebelum=l.data_sebelum,
            data_sesudah=l.data_sesudah,
            ip=l.ip,
            user_agent=l.user_agent,
            hasil=l.hasil
        ))

    return {
        "page": page,
        "limit": limit,
        "total_records": total_records,
        "total_pages": (total_records + limit - 1) // limit if limit > 0 else 1,
        "items": logs_list
    }
