import os
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from .routers import production1, production2, auth, scan
from .auth import get_current_user
from .database import engine, Base, AsyncSessionLocal
from .models import ProductionPlan, ScanSetting
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

# Register Router (Auth terbuka untuk login, Production1 & 2 wajib terotentikasi)
app.include_router(auth.router, prefix="/api/v1")
app.include_router(scan.router, prefix="/api/v1", dependencies=[Depends(get_current_user)])
app.include_router(production1.router, prefix="/api/v1", dependencies=[Depends(get_current_user)])
app.include_router(production2.router, prefix="/api/v1", dependencies=[Depends(get_current_user)])


@app.on_event("startup")
async def on_startup():
    # 1. Buat tabel otomatis jika belum ada (berguna saat dev lokal)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Migrasi SQLite otomatis untuk kolom photo_evidence jika belum ada
        try:
            from sqlalchemy import text
            await conn.execute(text("ALTER TABLE lot_trackings ADD COLUMN photo_evidence TEXT"))
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
                target_gilingan=40,
                target_mika_pax=0,
                target_vakum_pax=0,
                status="ACTIVE",
                created_by="SYSTEM"
            ))
            await session.commit()
            print(f"[STARTUP] Auto-seed ProductionPlan '{plan_id}' untuk hari ini ({today}).")
        else:
            print(f"[STARTUP] ProductionPlan '{plan_id}' sudah ada.")

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



@app.get("/health", tags=["Health Check"])
async def health_check():
    return {
        "status": "healthy",
        "service": "Dimsum Manufacturing Traceability Backend",
        "version": "1.0.0"
    }
