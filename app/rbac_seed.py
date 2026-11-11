"""
RBAC Database Seeding & Migration Script - Dimsum MES
Memastikan tabel RBAC terisi, template role sistem dibuat,
Super Admin diinisialisasi secara aman, dan 16 akun legacy dimigrasikan tanpa kehilangan akses.
"""

import os
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .models import Permission, Role, User, RolePermission, UserRole
from .permissions_registry import PERMISSIONS, SYSTEM_ROLE_TEMPLATES
from .auth import ACCOUNTS, hash_password, CREDENTIALS_FILE
import json


async def seed_rbac_data(session: AsyncSession) -> dict:
    """
    Eksekusi seeding dan migrasi data RBAC.
    Idempotent: aman dijalankan berulang kali saat startup aplikasi.
    """
    report = {
        "permissions_created": 0,
        "roles_created": 0,
        "users_migrated": 0,
        "superadmin_created": False
    }

    # 1. Sinkronisasi Izin (Permissions)
    existing_perms_res = await session.execute(select(Permission))
    existing_perms = {p.code: p for p in existing_perms_res.scalars().all()}

    for p_def in PERMISSIONS:
        if p_def["code"] not in existing_perms:
            new_perm = Permission(
                code=p_def["code"],
                name=p_def["name"],
                feature=p_def["feature"],
                action=p_def["action"],
                description=p_def.get("description")
            )
            session.add(new_perm)
            report["permissions_created"] += 1
        else:
            # Perbarui nama atau deskripsi jika ada perubahan
            perm = existing_perms[p_def["code"]]
            perm.name = p_def["name"]
            perm.feature = p_def["feature"]
            perm.action = p_def["action"]
            perm.description = p_def.get("description")

    await session.flush()

    # Muat ulang seluruh permission yang ada di DB
    all_perms_res = await session.execute(select(Permission))
    perm_map = {p.code: p for p in all_perms_res.scalars().all()}

    # 2. Sinkronisasi Template Role Sistem
    roles_res = await session.execute(
        select(Role).options(selectinload(Role.permissions))
    )
    existing_roles = {r.name: r for r in roles_res.scalars().all()}

    for role_name, role_meta in SYSTEM_ROLE_TEMPLATES.items():
        role = existing_roles.get(role_name)
        target_perm_codes = set(role_meta.get("permissions", []))
        target_perm_objs = [perm_map[c] for c in target_perm_codes if c in perm_map]

        if not role:
            role = Role(
                name=role_name,
                description=role_meta.get("description"),
                is_system=role_meta.get("is_system", True),
                created_at=datetime.utcnow()
            )
            role.permissions = target_perm_objs
            session.add(role)
            existing_roles[role_name] = role
            report["roles_created"] += 1
        else:
            role.is_system = role_meta.get("is_system", True)
            if role_meta.get("description"):
                role.description = role_meta.get("description")
            role.permissions = target_perm_objs

    # 2b. Migrasi Izin Harga untuk seluruh role (Akses Minimal):
    # - Setiap role yang memiliki 'freezer_sales:view' otomatis diberikan 'freezer_sales:view_price'
    # - 'freezer_sales:edit_price' hanya untuk Super Admin
    view_price_perm = perm_map.get("freezer_sales:view_price")
    edit_price_perm = perm_map.get("freezer_sales:edit_price")
    for r in existing_roles.values():
        r_perms = {p.code for p in r.permissions}
        if "freezer_sales:view" in r_perms and view_price_perm and "freezer_sales:view_price" not in r_perms:
            r.permissions.append(view_price_perm)
        if r.name != "Super Admin" and edit_price_perm and "freezer_sales:edit_price" in r_perms:
            r.permissions = [p for p in r.permissions if p.code != "freezer_sales:edit_price"]

    await session.flush()

    # 3. Muat kredensial persisten dari credentials.json (jika user pernah ubah password)
    custom_passwords = {}
    if os.path.exists(CREDENTIALS_FILE):
        try:
            with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
                custom_passwords = json.load(f)
        except Exception:
            pass

    # 4. Inisialisasi Akun Super Admin
    superadmin_user_res = await session.execute(
        select(User).options(selectinload(User.roles)).where(User.username == "superadmin")
    )
    superadmin_user = superadmin_user_res.scalar_one_or_none()

    if not superadmin_user:
        initial_pw = os.getenv("SUPERADMIN_INITIAL_PASSWORD", "SuperAdmin#2026")
        sa_pw_hash = hash_password(initial_pw)
        superadmin_user = User(
            username="superadmin",
            password_hash=sa_pw_hash,
            operator_id="SUPER-01",
            full_name="Super Administrator",
            division="Kantor Manajemen Pusat",
            default_stage="PROD_1",
            default_station="PLAN",
            default_prod2_tab="PRODUCTS",
            is_active=True,
            is_superadmin=True,
            must_change_password=True,
            created_at=datetime.utcnow()
        )
        if "Super Admin" in existing_roles:
            superadmin_user.roles.append(existing_roles["Super Admin"])
        session.add(superadmin_user)
        report["superadmin_created"] = True
    else:
        # Pastikan flag boolean is_superadmin selalu True
        superadmin_user.is_superadmin = True
        superadmin_user.is_active = True
        if "Super Admin" in existing_roles and existing_roles["Super Admin"] not in superadmin_user.roles:
            superadmin_user.roles.append(existing_roles["Super Admin"])

    # 5. Migrasi 16 Akun Legacy dari ACCOUNTS
    legacy_role_mapping = {
        "opr_daging": ["Produksi Giling & Mix"],
        "pic_daging": ["Produksi Giling & Mix", "Viewer"],
        "opr_bumbu": ["Produksi Bumbu"],
        "pic_bumbu": ["Produksi Bumbu", "Viewer"],
        "opr_kulit": ["Produksi Kulit Dimsum"],
        "pic_kulit": ["Produksi Kulit Dimsum", "Viewer"],
        "opr_saos": ["Produksi Saos"],
        "pic_saos": ["Produksi Saos", "Viewer"],
        "opr_cetak": ["Meja Cetak"],
        "pic_cetak": ["Meja Cetak", "Viewer"],
        "opr_packing": ["Meja Packing"],
        "pic_packing": ["Meja Packing", "Viewer"],
        "opr_gudang": ["Gudang / Penerima Barang"],
        "pic_gudang": ["Gudang / Penerima Barang", "Viewer"],
        "opr_kasir": ["Kasir"],
        "pic_kasir": ["PIC Kasir", "Viewer"],
        "ppic_ayu": ["PPIC"],
        "supervisor": [
            "PPIC", "Viewer", "Produksi Giling & Mix", "Produksi Bumbu",
            "Produksi Kulit Dimsum", "Produksi Saos", "Meja Cetak",
            "Meja Packing", "Gudang / Penerima Barang"
        ]
    }

    for username, acc_data in ACCOUNTS.items():
        user_res = await session.execute(
            select(User).options(selectinload(User.roles)).where(User.username == username)
        )
        existing_user = user_res.scalar_one_or_none()

        pw_hash = custom_passwords.get(username, acc_data.get("password"))

        if not existing_user:
            created_dt = datetime.utcnow()
            if acc_data.get("created_at"):
                try:
                    created_dt = datetime.fromisoformat(acc_data["created_at"].replace("Z", "+00:00"))
                except Exception:
                    pass

            new_user = User(
                username=username,
                password_hash=pw_hash,
                operator_id=acc_data.get("operator_id"),
                full_name=acc_data.get("full_name"),
                division=acc_data.get("division"),
                default_stage=acc_data.get("default_stage", "PROD_1"),
                default_station=acc_data.get("default_station", "MEAT"),
                default_prod2_tab=acc_data.get("default_prod2_tab", "INBOUND"),
                is_active=True,
                is_superadmin=False,
                must_change_password=False,
                created_at=created_dt
            )

            # Tetapkan role
            target_roles = legacy_role_mapping.get(username, ["Viewer"])
            new_user.roles = [existing_roles[r] for r in target_roles if r in existing_roles]

            session.add(new_user)
            report["users_migrated"] += 1
        else:
            # Perbarui data jika belum punya role
            if not existing_user.roles:
                target_roles = legacy_role_mapping.get(username, ["Viewer"])
                existing_user.roles = [existing_roles[r] for r in target_roles if r in existing_roles]

    await session.commit()
    return report
