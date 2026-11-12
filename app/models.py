import uuid
from datetime import datetime
from sqlalchemy import Column, String, Numeric, Boolean, Date, DateTime, Integer, BigInteger, ForeignKey, Text, UniqueConstraint
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


# 2. PRODUCTION PLANNING & BATCHES
class ProductionPlan(Base):
    __tablename__ = "production_plans"

    plan_id = Column(String(50), primary_key=True)
    plan_date = Column(Date, nullable=False)
    target_gilingan = Column(Integer, nullable=False, default=0)
    target_mika_pax = Column(Integer, nullable=False, default=0)
    target_vakum_pax = Column(Integer, nullable=False, default=0)
    status = Column(String(20), default="DRAFT") # 'DRAFT', 'PUBLISHED', 'ACTIVE'
    created_by = Column(String(50), nullable=False)
    version = Column(Integer, default=1, nullable=False)

    # Kolom rincian input rencana PPIC
    order_mika = Column(Integer, default=0)
    order_vakum = Column(Integer, default=0)
    bumper_mika = Column(Integer, default=0)
    bumper_vakum = Column(Integer, default=0)
    manual_gilingan = Column(Integer, nullable=True)
    manual_bumbu = Column(Integer, nullable=True)
    target_kulit_bal = Column(Integer, default=0)
    target_kulit_9cm = Column(Integer, default=0)
    target_kulit_7cm = Column(Integer, default=0)
    target_saos_bal = Column(Integer, default=0)
    target_saos_mika = Column(Integer, default=0)
    target_saos_vakum = Column(Integer, default=0)
    published_at = Column(DateTime, nullable=True)
    published_by = Column(String(50), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    lots = relationship("LotTracking", back_populates="plan")
    discrepancies = relationship("DiscrepancyLog", back_populates="plan")
    batches = relationship("ProductionBatch", back_populates="plan", cascade="all, delete-orphan", order_by="ProductionBatch.batch_no")


class ProductionBatch(Base):
    __tablename__ = "production_batches"
    __table_args__ = (
        UniqueConstraint("plan_id", "batch_no", name="uq_plan_batch_no"),
    )

    batch_id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    plan_id = Column(String(50), ForeignKey("production_plans.plan_id"), nullable=False, index=True)
    batch_no = Column(Integer, nullable=False, default=1)
    plan_date = Column(Date, nullable=False, index=True)
    status = Column(String(20), default="ACTIVE", nullable=False) # 'ACTIVE', 'VOID'
    created_by = Column(String(50), nullable=False)
    reason = Column(Text, nullable=True) # e.g. 'Pesanan mendadak', 'Stok kurang'
    idempotency_key = Column(String(100), nullable=True, index=True)
    counts_json = Column(Text, nullable=True) # JSON string counts: {"adonan": 10, "bumbu": 1, ...}
    created_at = Column(DateTime, default=datetime.utcnow)

    plan = relationship("ProductionPlan", back_populates="batches")
    lots = relationship("LotTracking", back_populates="batch")


# 3. QR LOT TRACKING & STATE MACHINE
class LotTracking(Base):
    __tablename__ = "lot_trackings"

    lot_qr_code = Column(String(100), primary_key=True)
    plan_id = Column(String(50), ForeignKey("production_plans.plan_id"), nullable=True)
    batch_no = Column(Integer, default=1, nullable=False)
    batch_id = Column(BigInteger().with_variant(Integer, "sqlite"), ForeignKey("production_batches.batch_id"), nullable=True)
    lot_type = Column(String(20), nullable=False) # 'DAGING', 'BUMBU', 'KULIT', 'ADONAN', 'FG_MIKA', 'FG_VAKUM'
    current_status = Column(String(50), nullable=False) # 'READY_IN_MEAT_STATION', 'IN_TRANSIT_TO_SEASONING', 'IN_SEASONING_STATION', 'READY_IN_SEASONING_STATION', 'IN_TRANSIT_TO_ASSEMBLY', 'READY_IN_WRAPPER_STATION', 'IN_ASSEMBLY_STATION', 'CONSUMED', 'CLOSED', 'VOID'
    current_station = Column(String(50), nullable=False)
    quantity = Column(Numeric(10, 2), nullable=False)
    unit = Column(String(20), nullable=False)
    photo_evidence = Column(Text, nullable=True) # Bukti foto laporan nampan
    created_at = Column(DateTime, default=datetime.utcnow)

    plan = relationship("ProductionPlan", back_populates="lots")
    batch = relationship("ProductionBatch", back_populates="lots")
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


# 8. SUPPLIER INBOUND (PENERIMAAN BARANG DARI SUPPLIER LUAR)
class SupplierInbound(Base):
    __tablename__ = "supplier_inbounds"

    inbound_id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    inbound_date = Column(Date, nullable=False, default=datetime.utcnow)
    category = Column(String(50), nullable=False)  # 'KULIT', 'DAGING', 'BUMBU', 'SAOS', 'KEMASAN', 'LAINNYA'
    item_name = Column(String(100), nullable=False)
    quantity = Column(Numeric(10, 2), nullable=False)
    unit = Column(String(20), nullable=False)  # 'BAL', 'KG', 'KARUNG', 'DUS', 'PACK', 'PCS'
    supplier_name = Column(String(100), nullable=False)
    document_no = Column(String(100), nullable=True)  # No Surat Jalan / Nota
    notes = Column(Text, nullable=True)
    received_by = Column(String(50), nullable=False)
    has_qr = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


# 8b. SUPPLIER OUTBOUND / PENGELUARAN STOK LOGISTIK PPIC
class SupplierOutbound(Base):
    __tablename__ = "supplier_outbounds"

    outbound_id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    inbound_id = Column(BigInteger().with_variant(Integer, "sqlite"), ForeignKey("supplier_inbounds.inbound_id"), nullable=True)
    outbound_date = Column(Date, nullable=False, default=datetime.utcnow)
    lot_qr_code = Column(String(100), nullable=False, index=True)
    category = Column(String(50), nullable=False)
    item_name = Column(String(100), nullable=False)
    quantity = Column(Numeric(10, 2), nullable=False)
    unit = Column(String(20), nullable=False)
    target_station = Column(String(50), nullable=True)  # 'ASSEMBLY_STATION', 'MEAT_STATION', 'FINISHING_STATION', etc.
    target_station_name = Column(String(100), nullable=True)
    dispatched_by = Column(String(50), nullable=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    inbound = relationship("SupplierInbound", backref="outbounds")


# 9. DIVISION DAILY NOTES & REJECT RECORD (CATATAN REKAP PPIC)
class DivisionDailyNote(Base):
    __tablename__ = "division_daily_notes"

    note_id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    record_date = Column(Date, nullable=False, default=datetime.utcnow)
    division = Column(String(50), nullable=False)  # 'DAGING', 'BUMBU', 'KULIT', 'SAOS', 'CETAK', 'PACKING'
    reject_count = Column(Integer, default=0)
    reject_notes = Column(Text, nullable=True)
    operational_notes = Column(Text, nullable=True)
    created_by = Column(String(50), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# 10. RBAC: ROLES, PERMISSIONS, USERS & AUDIT LOGS

class Permission(Base):
    __tablename__ = "permissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(100), unique=True, nullable=False, index=True)  # e.g. "plan:view", "inbound:export"
    name = Column(String(100), nullable=False)
    feature = Column(String(50), nullable=False, index=True)
    action = Column(String(50), nullable=False)
    description = Column(String(255), nullable=True)

    roles = relationship("Role", secondary="role_permissions", back_populates="permissions", lazy="selectin")


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id = Column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    permission_id = Column(Integer, ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True)


class Role(Base):
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), unique=True, nullable=False, index=True)
    description = Column(String(255), nullable=True)
    is_system = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    permissions = relationship("Permission", secondary="role_permissions", back_populates="roles", lazy="selectin")
    users = relationship("User", secondary="user_roles", back_populates="roles", lazy="selectin")


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id = Column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    operator_id = Column(String(50), nullable=True)
    full_name = Column(String(100), nullable=False)
    division = Column(String(100), nullable=True)
    default_stage = Column(String(20), default="PROD_1")
    default_station = Column(String(50), default="MEAT")
    default_prod2_tab = Column(String(50), default="INBOUND")
    is_active = Column(Boolean, default=True, nullable=False)
    is_superadmin = Column(Boolean, default=False, nullable=False)
    must_change_password = Column(Boolean, default=False, nullable=False)
    last_login_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    roles = relationship("Role", secondary="user_roles", back_populates="users", lazy="selectin")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    waktu = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    user_id = Column(Integer, nullable=True)
    nama_user = Column(String(100), nullable=False)
    aksi = Column(String(50), nullable=False, index=True)
    objek_tipe = Column(String(50), nullable=False)
    objek_id = Column(String(100), nullable=True)
    ringkasan = Column(Text, nullable=False)
    data_sebelum = Column(Text, nullable=True)
    data_sesudah = Column(Text, nullable=True)
    ip = Column(String(50), nullable=True)
    user_agent = Column(Text, nullable=True)
    hasil = Column(String(20), default="SUCCESS", nullable=False)


class Product(Base):
    __tablename__ = "products"

    id = Column(String(50), primary_key=True) # e.g. "FG_MIKA", "FG_VAKUM"
    name = Column(String(100), nullable=False)
    sku = Column(String(100), nullable=True) # e.g. "DIMSUM-MINI-PAX-25"
    description = Column(Text, nullable=True)
    spec = Column(String(50), nullable=True) # e.g. "50 Pcs / Mika"
    price = Column(Integer, nullable=True) # integer rupiah, e.g. 45000 (harga jual final)
    original_price = Column(Numeric(12, 2), nullable=True) # harga normal (harga coret)
    is_discount = Column(Boolean, default=False, nullable=False)
    discount_percent = Column(Integer, default=0, nullable=False) # 1-100%
    stock_online = Column(Integer, default=0, nullable=False) # alokasi stok untuk customer website
    image_url = Column(String(255), nullable=True)
    thumbnail_url = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ==========================================
# POS (POINT OF SALE) & MULTI-CHANNEL MODELS
# ==========================================

class POSOrder(Base):
    __tablename__ = "pos_orders"

    id = Column(String(50), primary_key=True) # e.g. "ORD-20261111-0001" or "WEB-1042"
    order_no = Column(String(50), index=True, nullable=False)
    channel = Column(String(20), nullable=False, default="OFFLINE") # 'OFFLINE', 'SHOPEEFOOD', 'GOFOOD', 'GRABFOOD', 'WEBSITE'
    customer_name = Column(String(100), nullable=True)
    status = Column(String(20), default="PENDING_SCAN", nullable=False) # 'PENDING_SCAN', 'COMPLETED', 'CANCELLED'
    total_items = Column(Integer, default=0, nullable=False) # Total packs target
    total_scanned = Column(Integer, default=0, nullable=False) # Total packs scanned
    total_amount = Column(Integer, default=0, nullable=False) # Total rupiah
    payment_method = Column(String(20), default="CASH") # 'CASH', 'QRIS', 'TRANSFER', 'EXTERNAL_PAID'
    amount_paid = Column(Integer, default=0, nullable=False)
    payment_ref = Column(String(100), nullable=True) # e.g. Paywuz transaction ID / reference
    cashier_id = Column(String(50), nullable=True)
    cashier_name = Column(String(100), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    items = relationship("POSOrderItem", back_populates="order", cascade="all, delete-orphan", lazy="selectin")
    scanned_lots = relationship("POSOrderScannedLot", back_populates="order", cascade="all, delete-orphan", lazy="selectin")


class POSOrderItem(Base):
    __tablename__ = "pos_order_items"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    order_id = Column(String(50), ForeignKey("pos_orders.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = Column(String(50), ForeignKey("products.id"), nullable=False)
    product_name = Column(String(100), nullable=False)
    qty_ordered = Column(Integer, default=1, nullable=False)
    qty_scanned = Column(Integer, default=0, nullable=False)
    unit_price = Column(Integer, default=0, nullable=False)
    subtotal = Column(Integer, default=0, nullable=False)

    order = relationship("POSOrder", back_populates="items")


class POSOrderScannedLot(Base):
    __tablename__ = "pos_order_scanned_lots"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    order_id = Column(String(50), ForeignKey("pos_orders.id", ondelete="CASCADE"), nullable=False, index=True)
    lot_qr_code = Column(String(100), ForeignKey("lot_trackings.lot_qr_code"), nullable=False, index=True)
    product_id = Column(String(50), nullable=False)
    product_name = Column(String(100), nullable=False)
    scanned_at = Column(DateTime, default=datetime.utcnow)
    scanned_by = Column(String(50), nullable=True)

    order = relationship("POSOrder", back_populates="scanned_lots")


class POSApiKey(Base):
    __tablename__ = "pos_api_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False) # e.g. "Website E-Commerce Dimsum"
    key_prefix = Column(String(16), nullable=False) # e.g. "mdi_live_83b1"
    key_hash = Column(String(128), nullable=False, unique=True, index=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_by = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)


# ==========================================
# PROMOTIONS & MARKETING MODELS (VOUCHER & BANNER)
# ==========================================

class Voucher(Base):
    __tablename__ = "vouchers"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code = Column(String(50), unique=True, nullable=False, index=True)
    discount_type = Column(String(20), default="fixed", nullable=False) # 'fixed' or 'percent'
    discount_value = Column(Numeric(12, 2), nullable=False)
    min_spend = Column(Numeric(12, 2), default=0.00, nullable=False)
    max_discount = Column(Numeric(12, 2), nullable=True)
    start_date = Column(DateTime, nullable=True)
    end_date = Column(DateTime, nullable=True)
    usage_limit = Column(Integer, nullable=True)
    used_count = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    is_public = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class Banner(Base):
    __tablename__ = "banners"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title = Column(String(100), nullable=True)
    image_url = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)





