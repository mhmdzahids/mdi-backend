import os
import sys
import subprocess

# 1. Pastikan path direktori backend terdaftar di sys.path
backend_dir = os.path.dirname(os.path.abspath(__file__))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

# 2. Cek apakah aiosqlite tersedia di interpreter python saat ini.
# Jika user menjalankan `python wipe_db.py` menggunakan Python global (bukan venv),
# otomatis cari dan alihkan eksekusi ke interpreter virtual environment `venv`.
try:
    import aiosqlite  # noqa: F401
except ImportError:
    venv_python = os.path.join(backend_dir, "venv", "Scripts", "python.exe")
    if os.path.isfile(venv_python) and sys.executable.lower() != venv_python.lower():
        print(f"[BOOTSTRAP] Mengalihkan eksekusi ke Python venv: {venv_python}")
        res = subprocess.run([venv_python, os.path.abspath(__file__)] + sys.argv[1:])
        sys.exit(res.returncode)
    else:
        # Fallback tambahan: coba sisipkan direktori site-packages venv ke sys.path
        venv_site = os.path.join(backend_dir, "venv", "Lib", "site-packages")
        if os.path.isdir(venv_site) and venv_site not in sys.path:
            sys.path.insert(0, venv_site)
            try:
                import aiosqlite  # noqa: F401
            except ImportError:
                print("[ERROR] 'aiosqlite' tidak ditemukan. Harap aktifkan venv: .\\venv\\Scripts\\activate")
                sys.exit(1)

import asyncio
from datetime import date
from app.database import engine, Base, AsyncSessionLocal
from app.models import ProductionPlan, MasterRecipe

async def wipe_database():
    print("[WIPE] Menghapus seluruh tabel database...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    print("[WIPE] Berhasil membuat ulang seluruh skema tabel secara bersih.")

    today = date.today()
    today_str = today.strftime("%Y%m%d")
    plan_id = f"PLAN-{today_str}-01"

    async with AsyncSessionLocal() as session:
        # 1. Master Recipe Standar Dimsum Ayam (1 Gilingan = 15 kg daging -> 19 kg adonan)
        recipe = MasterRecipe(
            recipe_id="REC-DIMSUM-AYAM-STD",
            recipe_name="Resep Dimsum Ayam Standar",
            target_unit="1_GILINGAN_15KG",
            meat_input_gram=15000.00,
            expected_dough_yield_gram=19000.00,
            is_active=True
        )
        session.add(recipe)

        # 2. Production Plan Hari Ini (Target 40 Gilingan)
        plan = ProductionPlan(
            plan_id=plan_id,
            plan_date=today,
            target_gilingan=40,
            target_mika_pax=0,
            target_vakum_pax=0,
            status="ACTIVE",
            created_by="SYSTEM"
        )
        session.add(plan)
        await session.commit()
        print(f"[SEED] MasterRecipe 'REC-DIMSUM-AYAM-STD' & Plan '{plan_id}' aktif terpasang.")
        print("[SUCCESS] Database dimsum.db berhasil dibersihkan (0 lot / 0 log).")

if __name__ == "__main__":
    asyncio.run(wipe_database())
