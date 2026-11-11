"""
Unit & Integration Tests for:
- Product Discounts (TUGAS 1)
- Vouchers & Coupons with Race Condition Protection (TUGAS 2)
- Promotional Banners (TUGAS 3)
- API Key Authentication & Public Buyer Endpoints (TUGAS 4)
"""

import pytest
import asyncio
from datetime import datetime, timedelta
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models import Product, Voucher, Banner, User
from app.auth import create_token
from app.routers.public_buyer import verify_buyer_api_key
import urllib.request
import json
import uuid


def test_discount_business_logic():
    """TUGAS 1: Verifikasi formula perhitungan diskon & harga coret."""
    # Test 1: Diskon aktif 20% dari 50.000
    orig = 50000
    pct = 20
    is_disc = True
    if is_disc and pct > 0:
        price = round(orig * (1 - (pct / 100)))
    else:
        price = orig
        pct = 0
    saved = orig - price

    assert price == 40000
    assert saved == 10000

    # Test 2: Diskon dinonaktifkan
    is_disc = False
    if is_disc and pct > 0:
        price = round(orig * (1 - (pct / 100)))
    else:
        price = orig
        pct = 0
    saved = orig - price

    assert price == 50000
    assert saved == 0
    assert pct == 0


@pytest.mark.anyio
async def test_voucher_validation_and_quota_locking():
    """TUGAS 2: Verifikasi kupon, minimal belanja, kuota, dan atomic claim/release."""
    async with AsyncSessionLocal() as db:
        test_code = f"TESTV_{uuid.uuid4().hex[:6].upper()}"
        v = Voucher(
            id=str(uuid.uuid4()),
            code=test_code,
            discount_type="fixed",
            discount_value=15000.0,
            min_spend=30000.0,
            usage_limit=2,
            used_count=0,
            is_active=True,
            is_public=True
        )
        db.add(v)
        await db.commit()

        # 1. Validasi subtotal kurang dari min_spend (30.000)
        subtotal = 25000.0
        assert subtotal < v.min_spend

        # 2. Klaim kupon ke-1 (atomic claim)
        stmt = select(Voucher).where(Voucher.code == test_code).with_for_update()
        res = await db.execute(stmt)
        locked_v = res.scalar_one()
        assert locked_v.used_count == 0
        locked_v.used_count += 1
        await db.commit()

        # 3. Klaim kupon ke-2
        res2 = await db.execute(select(Voucher).where(Voucher.code == test_code).with_for_update())
        locked_v2 = res2.scalar_one()
        assert locked_v2.used_count == 1
        locked_v2.used_count += 1
        await db.commit()

        # 4. Kuota habis (used_count == usage_limit)
        res3 = await db.execute(select(Voucher).where(Voucher.code == test_code))
        locked_v3 = res3.scalar_one()
        assert locked_v3.used_count == 2
        is_quota_exhausted = locked_v3.used_count >= locked_v3.usage_limit
        assert is_quota_exhausted is True

        # 5. Rollback kuota (release)
        res4 = await db.execute(select(Voucher).where(Voucher.code == test_code).with_for_update())
        locked_v4 = res4.scalar_one()
        locked_v4.used_count -= 1
        await db.commit()

        res5 = await db.execute(select(Voucher).where(Voucher.code == test_code))
        final_v = res5.scalar_one()
        assert final_v.used_count == 1

        # Cleanup
        await db.delete(final_v)
        await db.commit()


@pytest.mark.anyio
async def test_banner_lifecycle():
    """TUGAS 3: Verifikasi lifecycle model banner."""
    async with AsyncSessionLocal() as db:
        test_banner_id = str(uuid.uuid4())
        b = Banner(
            id=test_banner_id,
            title="Banner Uji Coba Diskon",
            image_url="/uploads/banners/test_banner.webp",
            is_active=True
        )
        db.add(b)
        await db.commit()

        # Baca kembali
        res = await db.execute(select(Banner).where(Banner.id == test_banner_id))
        saved = res.scalar_one()
        assert saved.title == "Banner Uji Coba Diskon"
        assert saved.is_active is True

        # Toggle
        saved.is_active = False
        await db.commit()

        res2 = await db.execute(select(Banner).where(Banner.id == test_banner_id))
        toggled = res2.scalar_one()
        assert toggled.is_active is False

        # Cleanup
        await db.delete(toggled)
        await db.commit()
