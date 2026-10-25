from datetime import datetime
from sqlalchemy import Column, String, Numeric, Boolean, Date, DateTime, Integer, BigInteger, ForeignKey, Text
from sqlalchemy.orm import relationship
from .database import Base

# 1. MASTER RECIPE / BOM (CONFIGURABLE)
class MasterRecipe(Base):
    __tablename__ = "master_recipes"

    recipe_id = Column(String(50), primary_key=True)
    recipe_name = Column(String(100), nullable=False)
    target_unit = Column(String(50), default="1_GILINGAN_15KG")
    meat_input_gram = Column(Numeric(10, 2), default=15000.00)
    expected_dough_yield_gram = Column(Numeric(10, 2), default=19000.00)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    items = relationship("MasterRecipeItem", back_populates="recipe", cascade="all, delete-orphan")

class MasterRecipeItem(Base):
    __tablename__ = "master_recipe_items"

    item_id = Column(String(50), primary_key=True)
    recipe_id = Column(String(50), ForeignKey("master_recipes.recipe_id"), nullable=False)
    ingredient_name = Column(String(100), nullable=False)
    quantity_gram = Column(Numeric(10, 2), nullable=False)
    unit = Column(String(20), default="GRAM")

    recipe = relationship("MasterRecipe", back_populates="items")


# 2. PRODUCTION PLANNING
class ProductionPlan(Base):
    __tablename__ = "production_plans"

    plan_id = Column(String(50), primary_key=True)
    plan_date = Column(Date, nullable=False)
    target_gilingan = Column(Integer, nullable=False)
    target_mika_pax = Column(Integer, nullable=False)
    target_vakum_pax = Column(Integer, nullable=False)
    status = Column(String(20), default="PLANNED")
    created_by = Column(String(50), nullable=False)

    lots = relationship("LotTracking", back_populates="plan")
    discrepancies = relationship("DiscrepancyLog", back_populates="plan")


# 3. QR LOT TRACKING & STATE MACHINE
class LotTracking(Base):
    __tablename__ = "lot_trackings"

    lot_qr_code = Column(String(100), primary_key=True)
    plan_id = Column(String(50), ForeignKey("production_plans.plan_id"), nullable=True)
    lot_type = Column(String(20), nullable=False) # 'DAGING', 'BUMBU', 'KULIT', 'ADONAN', 'FG_MIKA', 'FG_VAKUM'
    current_status = Column(String(50), nullable=False) # 'READY_IN_MEAT_STATION', 'IN_TRANSIT_TO_SEASONING', 'IN_SEASONING_STATION', 'READY_IN_SEASONING_STATION', 'IN_TRANSIT_TO_ASSEMBLY', 'READY_IN_WRAPPER_STATION', 'IN_ASSEMBLY_STATION', 'CONSUMED', 'CLOSED'
    current_station = Column(String(50), nullable=False)
    quantity = Column(Numeric(10, 2), nullable=False)
    unit = Column(String(20), nullable=False)
    photo_evidence = Column(Text, nullable=True) # Bukti foto laporan nampan
    created_at = Column(DateTime, default=datetime.utcnow)

    plan = relationship("ProductionPlan", back_populates="lots")
    custody_logs = relationship("CustodyTransferLog", back_populates="lot")


# 4. CUSTODY TRANSFER AUDIT LOG
class CustodyTransferLog(Base):
    __tablename__ = "custody_transfer_logs"

    log_id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    lot_qr_code = Column(String(100), ForeignKey("lot_trackings.lot_qr_code"), nullable=False)
    action_type = Column(String(50), nullable=False) # 'LOT_GENERATED', 'SCAN_OUT', 'SCAN_IN', 'MIXING_COMPLETED', 'WRAPPER_PREPARED', 'TRIPLE_VALIDATION'
    station_id = Column(String(50), nullable=False)
    target_station = Column(String(50), nullable=True) # Stasiun tujuan saat SCAN_OUT
    user_id = Column(String(50), nullable=False)
    user_role_active = Column(String(50), nullable=False)
    notes = Column(Text, nullable=True) # Catatan opsional / override supervisor
    timestamp = Column(DateTime, default=datetime.utcnow)

    lot = relationship("LotTracking", back_populates="custody_logs")


# 5. DISCREPANCY LOGS
class DiscrepancyLog(Base):
    __tablename__ = "discrepancy_logs"

    discrepancy_id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    plan_id = Column(String(50), ForeignKey("production_plans.plan_id"), nullable=True)
    expected_qty = Column(Numeric(10, 2), nullable=False)
    actual_qty = Column(Numeric(10, 2), nullable=False)
    variance = Column(Numeric(10, 2), nullable=False)
    unit = Column(String(20), nullable=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    plan = relationship("ProductionPlan", back_populates="discrepancies")


# 6. SCAN OPERATIONAL SETTINGS
class ScanSetting(Base):
    __tablename__ = "scan_settings"

    station_id = Column(String(50), primary_key=True)  # 'GLOBAL', 'MEAT_STATION', 'SEASONING_STATION', 'WRAPPER_STATION', 'SAUCE_STATION', 'ASSEMBLY_STATION', 'FINISHING_STATION'
    scan_mode = Column(String(10), nullable=False, default="1X")  # '1X', '2X', or 'INHERIT'
    updated_by = Column(String(50), nullable=False, default="SYSTEM")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# 7. SCAN EVENTS (APPEND-ONLY AUDIT & IDEMPOTENCY LOG)
class ScanEvent(Base):
    __tablename__ = "scan_events"

    event_id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    lot_qr_code = Column(String(100), ForeignKey("lot_trackings.lot_qr_code"), nullable=False, index=True)
    station_id = Column(String(50), nullable=False)
    user_id = Column(String(50), nullable=False)
    action = Column(String(50), nullable=False)  # 'CONFIRM_STOCK', 'SCAN_OUT', 'SCAN_IN', 'UNDO'
    mode_applied = Column(String(10), nullable=False)  # '1X' or '2X'
    previous_status = Column(String(50), nullable=False)
    new_status = Column(String(50), nullable=False)
    notes = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)

    lot = relationship("LotTracking", backref="scan_events")

