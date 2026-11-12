"""
Seed physical lot data for Finished Goods (Dimsum Mika & Dimsum Vakum):
- IN_SALES_STOCK: Lots that were already verified (ACC) by Sales -> ready to sell at POS
- PACKED_VERIFIED: Lots from Packing/Kitchen waiting for Sales to verify (ACC)
"""
import asyncio
from datetime import datetime, timezone
from sqlalchemy import select, delete
from app.database import AsyncSessionLocal
from app.models import LotTracking, Product

async def seed_lots():
    async with AsyncSessionLocal() as db:
        # Ensure products exist
        for prod_id, name, spec, price in [
            ("FG_MIKA", "Dimsum Mika", "50 Pcs / Mika", 45000),
            ("FG_VAKUM", "Dimsum Vakum", "25 Pcs / Pack", 50000)
        ]:
            p = await db.get(Product, prod_id)
            if not p:
                db.add(Product(
                    id=prod_id,
                    name=name,
                    spec=spec,
                    price=price,
                    stock_online=100,
                    is_active=True
                ))
        await db.commit()

        # Check existing lots
        res = await db.execute(select(LotTracking))
        existing_lots = res.scalars().all()
        if len(existing_lots) > 0:
            print(f"Existing lots count: {len(existing_lots)}. Keeping existing data.")
            return

        now = datetime.now(timezone.utc)
        lots_to_add = []

        # 1. Dimsum Mika: 20 IN_SALES_STOCK, 10 PACKED_VERIFIED
        for i in range(1, 21):
            lots_to_add.append(LotTracking(
                lot_qr_code=f"LOT-MIKA-ACC-{i:03d}",
                lot_type="FG_MIKA",
                current_status="IN_SALES_STOCK",
                current_station="SALES_STATION",
                quantity=1.0,
                unit="PACK",
                created_at=now
            ))
        for i in range(1, 11):
            lots_to_add.append(LotTracking(
                lot_qr_code=f"LOT-MIKA-STG-{i:03d}",
                lot_type="FG_MIKA",
                current_status="PACKED_VERIFIED",
                current_station="PACKING_STATION",
                quantity=1.0,
                unit="PACK",
                created_at=now
            ))

        # 2. Dimsum Vakum: 15 IN_SALES_STOCK, 8 PACKED_VERIFIED
        for i in range(1, 16):
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

        db.add_all(lots_to_add)
        await db.commit()
        print(f"Seeded {len(lots_to_add)} finished goods packaging lots successfully.")

if __name__ == "__main__":
    asyncio.run(seed_lots())
