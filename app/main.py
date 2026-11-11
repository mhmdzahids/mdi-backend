import os
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from .routers import production1, production2, auth, scan, admin, products, pos, public_buyer, promotions
from .auth import get_current_user
from .database import engine, Base, AsyncSessionLocal
from .models import ProductionPlan, ScanSetting, Product
from .rbac_seed import seed_rbac_data
from datetime import date, datetime
from sqlalchemy import select

app = FastAPI(
    title="Dimsum Manufacturing Traceability API",
    description="Sistem MES & Traceability Barcode Manufaktur Dimsum (Produksi 1 & POS)",
    version="1.0.0"
)

# CORS Middleware terkonfigurasi aman (tidak menggunakan wildcard dengan credentials)
cors_origins_env = os.getenv("CORS_ORIGINS", "")
allowed_origins = [o.strip() for o in cors_origins_env.split(",") if o.strip()] or [
    "http://localhost:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:5174",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register Router (Auth terbuka untuk login, Production1 & 2 & Scan & Admin & Products wajib terotentikasi)
app.include_router(auth.router, prefix="/api/v1")
app.include_router(scan.router, prefix="/api/v1", dependencies=[Depends(get_current_user)])
app.include_router(production1.router, prefix="/api/v1", dependencies=[Depends(get_current_user)])
app.include_router(production2.router, prefix="/api/v1", dependencies=[Depends(get_current_user)])
app.include_router(admin.router, prefix="/api/v1", dependencies=[Depends(get_current_user)])
app.include_router(products.router, prefix="/api/v1", dependencies=[Depends(get_current_user)])
app.include_router(pos.router, prefix="/api/v1")
app.include_router(public_buyer.router, prefix="/api/v1")
app.include_router(promotions.router, prefix="/api/v1", dependencies=[Depends(get_current_user)])

# Static Files untuk upload foto katalog produk & banner promosi
os.makedirs("uploads/products", exist_ok=True)
os.makedirs("uploads/banners", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")


@app.on_event("startup")
async def on_startup():
    # 1. Buat tabel otomatis jika belum ada (berguna saat dev lokal)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Migrasi SQLite otomatis untuk kolom baru jika belum ada
        from sqlalchemy import text
        migrations = [
            "ALTER TABLE lot_trackings ADD COLUMN photo_evidence TEXT",
            "ALTER TABLE lot_trackings ADD COLUMN batch_no INTEGER DEFAULT 1",
            "ALTER TABLE lot_trackings ADD COLUMN batch_id INTEGER",
            "ALTER TABLE production_plans ADD COLUMN version INTEGER DEFAULT 1",
            "ALTER TABLE production_plans ADD COLUMN order_mika INTEGER DEFAULT 0",
            "ALTER TABLE production_plans ADD COLUMN order_vakum INTEGER DEFAULT 0",
            "ALTER TABLE production_plans ADD COLUMN bumper_mika INTEGER DEFAULT 0",
            "ALTER TABLE production_plans ADD COLUMN bumper_vakum INTEGER DEFAULT 0",
            "ALTER TABLE production_plans ADD COLUMN manual_gilingan INTEGER",
            "ALTER TABLE production_plans ADD COLUMN manual_bumbu INTEGER",
            "ALTER TABLE production_plans ADD COLUMN target_kulit_bal INTEGER DEFAULT 0",
            "ALTER TABLE production_plans ADD COLUMN target_kulit_9cm INTEGER DEFAULT 0",
            "ALTER TABLE production_plans ADD COLUMN target_kulit_7cm INTEGER DEFAULT 0",
            "ALTER TABLE production_plans ADD COLUMN target_saos_bal INTEGER DEFAULT 0",
            "ALTER TABLE production_plans ADD COLUMN target_saos_mika INTEGER DEFAULT 0",
            "ALTER TABLE production_plans ADD COLUMN target_saos_vakum INTEGER DEFAULT 0",
            "ALTER TABLE production_plans ADD COLUMN published_at DATETIME",
            "ALTER TABLE production_plans ADD COLUMN published_by TEXT",
            "ALTER TABLE production_plans ADD COLUMN updated_at DATETIME",
            "ALTER TABLE supplier_inbounds ADD COLUMN has_qr BOOLEAN DEFAULT 1",
        ]
        for m in migrations:
            try:
                await conn.execute(text(m))
            except Exception:
                pass

    # 2. Auto-seed ProductionPlan hari ini agar plan_id dari frontend selalu valid
    today = date.today()
    today_str = today.strftime("%Y%m%d")
    plan_id = f"PLAN-{today_str}-01"

    async with AsyncSessionLocal() as session:
        existing = await session.execute(
            select(ProductionPlan).where(ProductionPlan.plan_id == plan_id)
        )
        if not existing.scalar_one_or_none():
            session.add(ProductionPlan(
                plan_id=plan_id,
                plan_date=today,
                target_gilingan=0,
                target_mika_pax=0,
                target_vakum_pax=0,
                status="DRAFT",
                version=1,
                created_by="SYSTEM"
            ))
            await session.commit()
            print(f"[STARTUP] Auto-seed ProductionPlan '{plan_id}' (DRAFT) untuk hari ini ({today}).")
        else:
            print(f"[STARTUP] ProductionPlan '{plan_id}' sudah ada.")

        # 2b. Backfill Batch 1 untuk plan yang sudah memiliki lot tapi belum memiliki record ProductionBatch
        try:
            from .models import ProductionBatch, LotTracking
            from sqlalchemy import func
            plans_res = await session.execute(select(ProductionPlan))
            all_plans = plans_res.scalars().all()
            for p in all_plans:
                lot_count_res = await session.execute(
                    select(func.count(LotTracking.lot_qr_code)).where(LotTracking.plan_id == p.plan_id)
                )
                total_cnt = lot_count_res.scalar() or 0
                if total_cnt > 0:
                    batch_check = await session.execute(
                        select(ProductionBatch).where(ProductionBatch.plan_id == p.plan_id, ProductionBatch.batch_no == 1)
                    )
                    if not batch_check.scalar_one_or_none():
                        b1 = ProductionBatch(
                            plan_id=p.plan_id,
                            batch_no=1,
                            plan_date=p.plan_date,
                            status="ACTIVE",
                            created_by=p.created_by or "SYSTEM",
                            reason="Batch 1 (Migrasi Data Lampau)",
                            counts_json=f'{{"total": {total_cnt}}}'
                        )
                        session.add(b1)
                        if p.status == "PLANNED" or p.status == "ACTIVE":
                            p.status = "PUBLISHED"
                        if not p.published_at:
                            p.published_at = datetime.utcnow()
                        if not p.published_by:
                            p.published_by = p.created_by or "SYSTEM"
            await session.commit()
        except Exception as e:
            print(f"[STARTUP] Backfill batch notice: {e}")

        # 3. Auto-seed ScanSetting GLOBAL default '1X'
        existing_scan_mode = await session.execute(
            select(ScanSetting).where(ScanSetting.station_id == "GLOBAL")
        )
        if not existing_scan_mode.scalar_one_or_none():
            session.add(ScanSetting(
                station_id="GLOBAL",
                scan_mode="1X",
                updated_by="SYSTEM"
            ))
            await session.commit()
            print("[STARTUP] Auto-seed ScanSetting GLOBAL='1X'.")

        # 4. Inisialisasi & Migrasi RBAC (Roles, Permissions, Super Admin, & 16 Legacy Accounts)
        try:
            rbac_report = await seed_rbac_data(session)
            print(f"[STARTUP] Inisialisasi & Migrasi RBAC Selesai: {rbac_report}")
        except Exception as e:
            print(f"[STARTUP] Peringatan: Gagal seeding RBAC: {e}")

        # 5. Auto-seed Produk Standar (Dimsum Mika & Dimsum Vakum) jika tabel products kosong
        try:
            prod_cnt_res = await session.execute(select(Product))
            existing_prods = prod_cnt_res.scalars().all()
            if not existing_prods:
                default_products = [
                    Product(
                        id="FG_MIKA",
                        name="Dimsum Mika",
                        description="Dimsum kukus isi 50 pcs kemasan mika menggunakan kulit 9 cm.",
                        spec="50 Pcs / Mika",
                        price=None,
                        image_url=None,
                        thumbnail_url=None,
                        is_active=False
                    ),
                    Product(
                        id="FG_VAKUM",
                        name="Dimsum Vakum",
                        description="Dimsum beku isi 25 pcs kemasan vakum menggunakan kulit 7 cm.",
                        spec="25 Pcs / Pack",
                        price=None,
                        image_url=None,
                        thumbnail_url=None,
                        is_active=False
                    )
                ]
                session.add_all(default_products)
                await session.commit()
                print("[STARTUP] Auto-seed 2 produk katalog default (FG_MIKA, FG_VAKUM).")
        except Exception as e:
            print(f"[STARTUP] Seeding default products notice: {e}")



@app.get("/health", tags=["Health Check"])
async def health_check():
    return {
        "status": "healthy",
        "service": "Dimsum Manufacturing Traceability Backend",
        "version": "1.0.0"
    }
