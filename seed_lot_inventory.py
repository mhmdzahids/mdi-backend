"""
Seed physical lot data for Finished Goods (Dimsum Mika & Dimsum Vakum):
- IN_SALES_STOCK: Lots that were already verified (ACC) by Sales -> ready to sell at POS
- PACKED_VERIFIED: Lots from Packing/Kitchen waiting for Sales to verify (ACC)
- SOLD: Lots sold at POS today
"""
import asyncio
from datetime import datetime, timezone
from sqlalchemy import select, delete
from app.database import AsyncSessionLocal
from app.models import LotTracking, Product

async def seed_lots():
    async with AsyncSessionLocal() as db:
        # Ensure products exist with realistic online stock
        mika = await db.get(Product, "FG_MIKA")
        if not mika:
            mika = Product(
                id="FG_MIKA",
                name="Dimsum Mika",
                spec="50 Pcs / Mika",
                price=45000,
                stock_online=50,
                is_active=True
            )
            db.add(mika)
        else:
            mika.price = 45000
            mika.spec = "50 Pcs / Mika"
            mika.stock_online = 50
            mika.is_active = True

        vakum = await db.get(Product, "FG_VAKUM")
        if not vakum:
            vakum = Product(
                id="FG_VAKUM",
                name="Dimsum Vakum",
                spec="25 Pcs / Pack",
                price=50000,
                stock_online=35,
                is_active=True
            )
            db.add(vakum)
        else:
            vakum.price = 50000
            vakum.spec = "25 Pcs / Pack"
            vakum.stock_online = 35
            vakum.is_active = True

        # Clean existing finished goods lots for idempotent seeding
        await db.execute(delete(LotTracking).where(LotTracking.lot_type.in_(["FG_MIKA", "FG_VAKUM"])))
        await db.commit()

        now = datetime.now(timezone.utc)
        lots_to_add = []

        # 1. Dimsum Mika:
        # - 25 IN_SALES_STOCK (Siap Jual di Freezer)
        # - 10 PACKED_VERIFIED (Menunggu ACC dari Dapur/Packing)
        # - 5 SOLD (Terjual Hari Ini di POS)
        for i in range(1, 26):
            lots_to_add.append(LotTracking(
                lot_qr_code=f"LOT-MIKA-ACC-{i:03d}",
                lot_type="FG_MIKA",
                current_status="IN_SALES_STOCK",
                current_station="SALES_STATION",
                quantity=1.0,
                unit="MIKA",
                created_at=now
            ))
        for i in range(1, 11):
            lots_to_add.append(LotTracking(
                lot_qr_code=f"LOT-MIKA-STG-{i:03d}",
                lot_type="FG_MIKA",
                current_status="PACKED_VERIFIED",
                current_station="PACKING_STATION",
                quantity=1.0,
                unit="MIKA",
                created_at=now
            ))
        for i in range(1, 6):
            lots_to_add.append(LotTracking(
                lot_qr_code=f"LOT-MIKA-SLD-{i:03d}",
                lot_type="FG_MIKA",
                current_status="SOLD",
                current_station="SALES_STATION",
                quantity=1.0,
                unit="MIKA",
                created_at=now
            ))

        # 2. Dimsum Vakum:
        # - 20 IN_SALES_STOCK (Siap Jual di Freezer)
        # - 8 PACKED_VERIFIED (Menunggu ACC dari Dapur/Packing)
        # - 4 SOLD (Terjual Hari Ini di POS)
        for i in range(1, 21):
            lots_to_add.append(LotTracking(
                lot_qr_code=f"LOT-VAKUM-ACC-{i:03d}",
                lot_type="FG_VAKUM",
                current_status="IN_SALES_STOCK",
                current_station="SALES_STATION",
                quantity=1.0,
                unit="PACK",
                created_at=now
            ))
        for i in range(1, 9):
            lots_to_add.append(LotTracking(
                lot_qr_code=f"LOT-VAKUM-STG-{i:03d}",
                lot_type="FG_VAKUM",
                current_status="PACKED_VERIFIED",
                current_station="PACKING_STATION",
                quantity=1.0,
                unit="PACK",
                created_at=now
            ))
        for i in range(1, 5):
            lots_to_add.append(LotTracking(
                lot_qr_code=f"LOT-VAKUM-SLD-{i:03d}",
                lot_type="FG_VAKUM",
                current_status="SOLD",
                current_station="SALES_STATION",
                quantity=1.0,
                unit="PACK",
                created_at=now
            ))

        db.add_all(lots_to_add)
        await db.commit()
        print(f"Berhasil seeding {len(lots_to_add)} finished goods lots:")
        print(f"  - FG_MIKA: 25 Siap Jual, 10 Menunggu ACC, 5 Terjual, Online: 50")
        print(f"  - FG_VAKUM: 20 Siap Jual, 8 Menunggu ACC, 4 Terjual, Online: 35")

if __name__ == "__main__":
    asyncio.run(seed_lots())
