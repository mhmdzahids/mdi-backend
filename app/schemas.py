from typing import List, Optional, Dict, Any
from datetime import datetime, date
from pydantic import BaseModel, Field

# --- Schema Master Recipe ---
class RecipeItemSchema(BaseModel):
    item_id: str
    ingredient_name: str
    quantity_gram: float
    unit: str = "GRAM"

    class Config:
        from_attributes = True

class RecipeSchema(BaseModel):
    recipe_id: str
    recipe_name: str
    target_unit: str
    meat_input_gram: float
    expected_dough_yield_gram: float
    is_active: bool
    items: List[RecipeItemSchema] = []

    class Config:
        from_attributes = True

# --- Schema Produksi 1 Requests ---
class GenerateByGilinganRequest(BaseModel):
    plan_id: str = Field(..., example="PLAN-20261003-01")
    gilingan_count: int = Field(..., gt=0, example=5)
    operator_id: str = Field(..., example="OPR-01")
    operator_role: str = Field(default="OPERATOR_DAGING")

# Dual-Scan: Scan-OUT oleh Divisi Daging ke Bumbu
class ScanOutMeatLotRequest(BaseModel):
    lot_code: str = Field(..., example="DG-20261003-001")
    operator_id: str = Field(..., example="OPR-01")
    operator_role: str = Field(default="OPERATOR_DAGING")
    target_station: str = Field(default="SEASONING_STATION")

# Dual-Scan: Scan-IN oleh Divisi Bumbu (Menerima Daging)
class ScanInMeatLotRequest(BaseModel):
    lot_code: str = Field(..., example="DG-20261003-001")
    operator_id: str = Field(..., example="OPR-02")
    operator_role: str = Field(default="OPERATOR_BUMBU")
    allow_supervisor_bypass: bool = Field(default=False, description="Bypass darurat jika pengirim lupa scan-OUT")
    bypass_reason: Optional[str] = Field(default=None, description="Alasan otorisasi bypass supervisor")
    supervisor_pin: Optional[str] = Field(default=None, description="PIN Supervisor untuk otorisasi bypass")

# Backward-compatibility alias
PullScanInMeatLotRequest = ScanInMeatLotRequest

class CompleteMixDoughRequest(BaseModel):
    parent_meat_lot_code: str = Field(..., example="DG-20261003-001")
    dough_yield_kg: float = Field(..., gt=0, example=19.20)
    operator_id: str = Field(..., example="OPR-02")
    operator_role: str = Field(default="OPERATOR_BUMBU")

# Dual-Scan: Scan-OUT oleh Divisi Bumbu ke Assembly (Produksi 2)
class ScanOutDoughLotRequest(BaseModel):
    lot_code: str = Field(..., example="AD-20261003-001")
    operator_id: str = Field(..., example="OPR-02")
    operator_role: str = Field(default="OPERATOR_GILING_MIXER")
    target_station: str = Field(default="ASSEMBLY_STATION")

# Schema Bumbu Sparta (1 Karung Sak = N Porsi Gilingan, default 20 Porsi / Karung)
class GenerateSeasoningSacksRequest(BaseModel):
    plan_id: str = Field(..., example="PLAN-20261005-01")
    sack_count: int = Field(default=1, gt=0, example=2)
    portions_per_sack: int = Field(default=20, gt=0, example=20)
    operator_id: str = Field(..., example="STK-01")
    operator_role: str = Field(default="STAKEHOLDER")

class GenerateSeasoningPouchesRequest(BaseModel):
    plan_id: str = Field(..., example="PLAN-20261005-01")
    pouch_count: Optional[int] = Field(default=None, gt=0, example=40)
    sack_count: Optional[int] = Field(default=None, gt=0, example=2)
    pouches_per_bag: int = Field(default=20, gt=0, example=20)
    portions_per_sack: int = Field(default=20, gt=0, example=20)
    operator_id: str = Field(..., example="STK-01")
    operator_role: str = Field(default="STAKEHOLDER")

class ScanOutSeasoningBatchRequest(BaseModel):
    lot_codes: List[str] = Field(default=[], description="Daftar kode BM- yang dikirim. Kosong = semua yang ready.")
    operator_id: str = Field(..., example="OPR-01")
    operator_role: str = Field(default="OPERATOR_BUMBU")
    target_station: str = Field(default="MEAT_MIXING_STATION")

class ScanInSeasoningBatchRequest(BaseModel):
    lot_codes: List[str] = Field(default=[], description="Daftar kode BM- yang diterima. Kosong = semua yang in-transit.")
    operator_id: str = Field(..., example="OPR-02")
    operator_role: str = Field(default="OPERATOR_GILING_MIXER")

class ProduceDoughMixedRequest(BaseModel):
    plan_id: str = Field(..., example="PLAN-20261005-01")
    gilingan_count: int = Field(..., gt=0, example=1)
    dough_yield_kg: float = Field(default=20.00, gt=0, example=20.00)
    operator_id: str = Field(..., example="OPR-02")
    operator_role: str = Field(default="OPERATOR_GILING_MIXER")
    meat_lot_codes: List[str] = Field(default=[], description="Daftar wadah daging yang dimix")
    bumbu_lot_codes: List[str] = Field(default=[], description="Daftar bumbu sparta yang dimix")

class CreateWrapperLotRequest(BaseModel):
    plan_id: str = Field(..., example="PLAN-20261003-01")
    wrapper_type: str = Field(default="KULIT_9CM", example="KULIT_9CM") # 'KULIT_7CM' | 'KULIT_9CM' (alias: 'VAKUM_7CM' | 'MIKA_9CM')
    bal_qty: int = Field(default=1, ge=1, example=1, description="Jumlah Bal fisik (1 Bal fisik = 1 QR, 15 kg/bal)")
    weight_kg: Optional[float] = Field(default=15.0, gt=0, description="Bobot per bal (kg)")
    pack_qty: Optional[int] = Field(default=None, description="Sub-kemasan pack (opsional)")
    total_sheets: Optional[int] = Field(default=None, description="Estimasi lembar (opsional)")
    operator_id: str = Field(..., example="OPR-03")
    operator_role: str = Field(default="STAKEHOLDER")
    source_type: Optional[str] = Field(default="INTERNAL", description="'INTERNAL' | 'EXTERNAL'")
    notes: Optional[str] = Field(default=None, description="Catatan penerimaan supplier luar")

# Dual-Scan: Scan-OUT oleh Divisi Kulit ke Assembly (Produksi 2)
class ScanOutWrapperLotRequest(BaseModel):
    lot_code: str = Field(..., example="KL-20261003-001")
    operator_id: str = Field(..., example="OPR-03")
    operator_role: str = Field(default="OPERATOR_KULIT")
    target_station: str = Field(default="ASSEMBLY_STATION")

# Divisi Saos: Cetak QR Bal Saos (1 Bal Fisik = 1 QR Unik)
# Dimsum Mini: 1 Bal = 200 plastik saos (es mambo)
# Dimsum Reguler: 1 Bal = 50 pouch saos
class CreateSauceLotRequest(BaseModel):
    plan_id: str = Field(..., example="PLAN-20261006-01")
    sauce_type: str = Field(default="REGULER", example="REGULER") # 'REGULER' (50 pouch/bal) | 'MINI' (200 plastik/bal)
    bal_qty: int = Field(default=1, ge=1, example=1, description="Jumlah Bal fisik (1 Bal = 1 QR)")
    operator_id: str = Field(..., example="OPR-04")
    operator_role: str = Field(default="STAKEHOLDER")

# Dual-Scan: Scan-OUT oleh Divisi Saos ke Bagian Finishing
class ScanOutSauceLotRequest(BaseModel):
    lot_code: str = Field(..., example="SA-20261006-001")
    operator_id: str = Field(..., example="OPR-04")
    operator_role: str = Field(default="OPERATOR_SAOS")
    target_station: str = Field(default="FINISHING_STATION")

# Konfirmasi Penempelan QR & Stock-In Fisik Produk oleh Pegawai Lapangan
class ConfirmStockInRequest(BaseModel):
    lot_codes: List[str] = Field(..., description="Daftar kode lot (BM-, DG-, KL-, SA-) yang ditempel stiker & dikonfirmasi masuk stok")
    operator_id: str = Field(..., example="OPR-01")
    operator_role: str = Field(default="OPERATOR", example="OPERATOR_BUMBU")
    actual_weight_kg: Optional[float] = Field(default=None, description="Opsional: Timbangan aktual saat ditempel")
    notes: Optional[str] = Field(default=None, description="Catatan opsional dari pegawai")

# Permintaan Cetak Batch QR Kemasan di Halaman Rencana (PPIC)
class GeneratePackagingBatchRequest(BaseModel):
    plan_id: Optional[str] = None
    product_type: str = Field(..., description="FG_MIKA atau FG_VAKUM")
    batch_qty: int = Field(..., ge=1, le=500, description="Jumlah pack yang ingin dicetak")
    operator_id: Optional[str] = Field(default="PPIC-AYU", description="ID operator/admin PPIC")

# --- Response Schemas ---
class LotResponse(BaseModel):
    lot_qr_code: str
    plan_id: Optional[str] = None
    lot_type: str
    current_status: str
    current_station: str
    quantity: float
    unit: str
    created_at: datetime
    photo_evidence: Optional[str] = None

    class Config:
        from_attributes = True

class CustodyLogResponse(BaseModel):
    log_id: int
    lot_qr_code: str
    action_type: str
    station_id: str
    target_station: Optional[str] = None
    user_id: str
    user_role_active: str
    notes: Optional[str] = None
    timestamp: datetime

    class Config:
        from_attributes = True


# --- Schema Production Plan & Batches ---
class ProductionBatchResponse(BaseModel):
    batch_id: int
    plan_id: str
    batch_no: int
    plan_date: date
    status: str
    created_by: str
    reason: Optional[str] = None
    counts: Optional[dict] = None
    created_at: datetime
    is_voidable: Optional[bool] = True

    class Config:
        from_attributes = True

class CreateProductionPlanRequest(BaseModel):
    plan_id: str = Field(..., example="PLAN-20261005-01")
    plan_date: date = Field(..., example="2026-10-05")
    target_gilingan: int = Field(..., ge=0, example=40)
    target_mika_pax: int = Field(default=0, ge=0, example=0)
    target_vakum_pax: int = Field(default=0, ge=0, example=0)
    created_by: str = Field(default="SYSTEM", example="SUPERVISOR-01")

class UpdatePlanDraftRequest(BaseModel):
    plan_id: Optional[str] = None
    version: int = 1
    order_mika: Optional[int] = 0
    order_vakum: Optional[int] = 0
    bumper_mika: Optional[int] = 0
    bumper_vakum: Optional[int] = 0
    manual_gilingan: Optional[int] = None
    manual_bumbu: Optional[int] = None
    target_gilingan: Optional[int] = None
    target_bumbu: Optional[int] = None
    target_kulit: Optional[int] = None
    target_kulit_9cm: Optional[int] = None
    target_kulit_7cm: Optional[int] = None
    target_saos: Optional[int] = None
    target_saos_mika: Optional[int] = None
    target_saos_vakum: Optional[int] = None
    operator_id: Optional[str] = "PPIC-AYU"

class ProductionPlanResponse(BaseModel):
    plan_id: str
    plan_date: date
    target_gilingan: int
    target_mika_pax: int
    target_vakum_pax: int
    status: str
    created_by: str
    version: int = 1
    order_mika: int = 0
    order_vakum: int = 0
    bumper_mika: int = 0
    bumper_vakum: int = 0
    manual_gilingan: Optional[int] = None
    manual_bumbu: Optional[int] = None
    target_kulit_bal: int = 0
    target_kulit_9cm: int = 0
    target_kulit_7cm: int = 0
    target_saos_bal: int = 0
    target_saos_mika: int = 0
    target_saos_vakum: int = 0
    published_at: Optional[datetime] = None
    published_by: Optional[str] = None
    updated_at: Optional[datetime] = None
    batches: List[ProductionBatchResponse] = []

    class Config:
        from_attributes = True


# --- Schema Produksi 2: Penerimaan Bahan Masuk (Inbound Receiving) ---
class ReceiveMaterialRequest(BaseModel):
    lot_code: str = Field(..., example="AD-20261006-001")
    operator_id: str = Field(..., example="OPR-02")
    operator_role: str = Field(default="OPERATOR_CETAK")
    target_station: Optional[str] = Field(default=None)

class BatchReceiveMaterialRequest(BaseModel):
    lot_codes: List[str] = Field(..., example=["AD-20261006-001", "KL-20261006-001"])
    operator_id: str = Field(..., example="OPR-02")
    operator_role: str = Field(default="OPERATOR_CETAK")


# --- Schema Produksi 2: Selesai Batch & Kemasan (Finished Goods) ---
class CompleteAssemblyBatchRequest(BaseModel):
    plan_id: str = Field(..., example="PLAN-20261007-01")
    dough_lot_codes: List[str] = Field(default=[], description="Lot Adonan yang dipakai")
    wrapper_lot_codes: List[str] = Field(default=[], description="Lot Kulit yang dipakai")
    mika_output_qty: int = Field(default=0, ge=0, description="Jumlah Mika Jadi (isi 50 butir)")
    vakum_output_qty: int = Field(default=0, ge=0, description="Jumlah Pack Vakum Jadi (isi 25 butir)")
    operator_id: str = Field(default="OPR-CETAK", example="OPR-CETAK")
    operator_role: str = Field(default="CHECKER", example="CHECKER")
    notes: Optional[str] = Field(default=None, description="Catatan opsional")

class DispatchFgToFreezerRequest(BaseModel):
    lot_codes: List[str] = Field(..., description="Daftar kode FG (FG-MIKA-, FG-VAKUM-) yang dikirim ke freezer")
    operator_id: str = Field(default="OPR-CETAK", example="OPR-CETAK")
    operator_role: str = Field(default="CHECKER", example="CHECKER")

class DispatchFgToDistributionRequest(BaseModel):
    lot_codes: List[str] = Field(..., description="Daftar kode FG (FG-MIKA-, FG-VAKUM-) yang langsung dikirim")
    operator_id: str = Field(default="OPR-CETAK", example="OPR-CETAK")
    operator_role: str = Field(default="CHECKER", example="CHECKER")
    notes: Optional[str] = Field(default="Kirim Langsung", description="Catatan opsional pengiriman")

class VerifyPackingRequest(BaseModel):
    lot_codes: List[str] = Field(..., description="Daftar kode FG yang sudah selesai dipacking")
    operator_id: str = Field(default="OPR-CHECKER", example="OPR-CHECKER")
    operator_role: str = Field(default="CHECKER", example="CHECKER")
    notes: Optional[str] = Field(default="Laporan packing selesai", description="Catatan opsional")

class ReceiveSalesStockRequest(BaseModel):
    lot_codes: List[str] = Field(..., description="Daftar kode FG yang diterima bagian penjualan")
    operator_id: str = Field(default="OPR-SALES", example="OPR-SALES")
    operator_role: str = Field(default="SALES", example="SALES")
    notes: Optional[str] = Field(default="Masuk stok POS", description="Catatan opsional")


# --- Schema Produksi 2: Transfer Wadah / Tray (Cetak -> Packing) ---
class DispatchTraysRequest(BaseModel):
    plan_id: str = Field(..., example="PLAN-20261012-01")
    product_type: str = Field(default="FG_MIKA", description="Tipe target kemasan: FG_MIKA atau FG_VAKUM")
    tray_count: int = Field(..., gt=0, example=5, description="Jumlah nampan/wadah yang dikirim ke packing")
    photo_evidence: Optional[str] = Field(None, description="Bukti foto laporan nampan (base64 image)")
    dough_lot_codes: List[str] = Field(default=[], description="Lot Adonan yang dipakai cetak")
    wrapper_lot_codes: List[str] = Field(default=[], description="Lot Kulit yang dipakai cetak")
    operator_id: str = Field(default="OPR-CETAK", example="OPR-CETAK")
    operator_role: str = Field(default="OPERATOR", example="OPERATOR")
    notes: Optional[str] = Field(default=None, description="Catatan opsional pengiriman wadah")

class ReceiveTraysRequest(BaseModel):
    tray_lot_codes: List[str] = Field(..., description="Daftar lot wadah yang diterima di packing")
    line_type: Optional[str] = Field(None, description="Line packing: MIKA atau VAKUM")
    operator_id: str = Field(default="OPR-PACKING", example="OPR-PACKING")
    operator_role: str = Field(default="OPERATOR", example="OPERATOR")
    notes: Optional[str] = Field(default=None, description="Catatan opsional penerimaan wadah")

class CompletePackingToFgRequest(BaseModel):
    plan_id: str = Field(..., example="PLAN-20261012-01")
    tray_lot_codes: List[str] = Field(default=[], description="Daftar lot wadah (opsional)")
    dough_lot_codes: List[str] = Field(default=[], description="Lot Adonan spesifik (opsional, default: auto-FIFO dari meja)")
    wrapper_lot_codes: List[str] = Field(default=[], description="Lot Kulit spesifik (opsional, default: auto-FIFO dari meja)")
    mika_output_qty: int = Field(default=0, ge=0, description="Jumlah Mika (isi 50 butir)")
    vakum_output_qty: int = Field(default=0, ge=0, description="Jumlah Pack Vakum (isi 25 butir)")
    reject_pcs: int = Field(default=0, ge=0, description="Jumlah butir reject/rusak")
    sauce_lot_codes: List[str] = Field(default=[], description="Lot Saos yang dipakai jika ada")
    operator_id: str = Field(default="OPR-PACKING", example="OPR-PACKING")
    operator_role: str = Field(default="OPERATOR", example="OPERATOR")
    notes: Optional[str] = Field(default=None, description="Catatan opsional hasil kemas")


class AdjustMaterialStockRequest(BaseModel):
    lot_code: str = Field(..., description="Kode QR lot bahan yang disesuaikan/digunakan")
    action_type: str = Field(default="CONSUME_ALL", description="Tipe penyesuaian: CONSUME_ALL atau CONSUME_PARTIAL")
    quantity_used: Optional[float] = Field(None, ge=0, description="Jumlah yang dikurangi jika parsial (kg/bal)")
    notes: Optional[str] = Field(None, description="Catatan alasan penggunaan atau pengurangan stok")
    operator_id: str = Field(default="OPR-PROD2", example="OPR-CETAK")
    operator_role: str = Field(default="OPERATOR", example="OPERATOR")

class ProduceBatchUnifiedRequest(BaseModel):
    plan_id: str = Field(..., example="PLAN-20261014-01")
    gilingan_count: int = Field(..., gt=0, example=5, description="Jumlah gilingan yang diproses")
    dough_yield_kg: float = Field(default=20.00, gt=0, example=20.00, description="Estimasi hasil yield per wadah adonan (kg)")
    bumbu_lot_codes: List[str] = Field(default=[], description="Kode lot bumbu yang digunakan")
    operator_id: str = Field(..., example="OPR-GILING")
    operator_role: str = Field(default="OPERATOR", example="OPERATOR")
    notes: Optional[str] = Field(default=None, description="Catatan operasional giling dan mixing")


class MaterialUsageRecordRequest(BaseModel):
    lot_code: str = Field(..., description="Kode QR lot bahan (bumbu karung atau bal kulit)")
    quantity_sent: float = Field(..., gt=0, description="Kuantitas awal bahan dikirim ke ruangan/meja")
    quantity_used: float = Field(..., ge=0, description="Kuantitas bahan yang riil terpakai")
    remaining_action: str = Field(default="STORE_IN_ROOM", description="'STORE_IN_ROOM' (simpan di meja) atau 'RETURN_TO_FRONTDESK' (retur)")
    notes: Optional[str] = Field(default=None, description="Catatan alasan pemakaian atau deviasi takaran")
    operator_id: str = Field(default="OPR-PROD", example="OPR-GILING")
    operator_role: str = Field(default="OPERATOR", example="OPERATOR")


class SalesStagedVerificationRequest(BaseModel):
    plan_id: str = Field(..., example="PLAN-20261014-01")
    product_id: str = Field(..., description="Tipe produk: 'FG_MIKA' atau 'FG_VAKUM'")
    actual_qty: int = Field(..., ge=0, description="Kuantitas fisik pack yang dihitung oleh bagian penjualan")
    verification_status: str = Field(default="ACC", description="Status verifikasi: 'ACC' (diterima) atau 'ONHOLD' (tertahan audit)")
    notes: Optional[str] = Field(default=None, description="Catatan fisik, nomor freezer, atau keterangan selisih")
    operator_id: str = Field(default="OPR-SALES", example="OPR-SALES")
    operator_role: str = Field(default="SALES", example="SALES")


class SalesStagedVerificationResponse(BaseModel):
    success: bool
    product_id: str
    verified_qty: int
    status: str
    notes: Optional[str] = None
    timestamp: datetime
    message: str

# --- Auth Schemas ---
class LoginRequest(BaseModel):
    username: str = Field(..., example="opr_daging")
    password: str = Field(..., example="password123")

class QuickSwitchRequest(BaseModel):
    username: str = Field(..., example="opr_bumbu")

class AuthUserResponse(BaseModel):
    username: str
    operator_id: str
    full_name: str
    role: str
    division: str
    default_stage: str = "PROD_1"
    default_station: str = "MEAT"
    default_prod2_tab: str = "INBOUND"

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: AuthUserResponse


# --- Schemas Mode Scan Dinamis (1x vs 2x) & State Machine ---
class ScanTransitionRequest(BaseModel):
    lot_code: str = Field(..., example="DG-20261022-001")
    station_id: str = Field(..., example="MEAT_STATION")
    intent: Optional[str] = Field(default=None, example="SCAN_IN")  # 'SCAN_IN' | 'SCAN_OUT'
    notes: Optional[str] = Field(default=None)


class ScanBatchTransitionRequest(BaseModel):
    lot_codes: List[str] = Field(..., min_length=1, example=["AD-20261023-001", "AD-20261023-002"])
    station_id: str = Field(..., example="MEAT_STATION")
    intent: str = Field(..., example="SCAN_OUT")  # 'SCAN_IN' | 'SCAN_OUT'
    notes: Optional[str] = Field(default=None)


class ScanUndoRequest(BaseModel):
    lot_code: str = Field(..., example="DG-20261022-001")


class UpdateScanModeRequest(BaseModel):
    global_mode: str = Field(..., example="1X")  # '1X' | '2X'
    station_modes: Dict[str, str] = Field(
        default_factory=dict,
        example={"MEAT_STATION": "2X", "SAUCE_STATION": "INHERIT"}
    )


class ScanModeSettingResponse(BaseModel):
    global_mode: str
    station_modes: Dict[str, str]
    stations: List[Dict[str, str]]
    affected_printed_lots_count: int
    updated_by: Optional[str] = None
    updated_at: Optional[datetime] = None


class VoidLotsRequest(BaseModel):
    lot_codes: List[str] = Field(..., min_length=1, example=["AD-20261023-006", "AD-20261023-007"])
    reason: Optional[str] = Field(default="Sisa rencana harian tidak terealisasi")
    operator_id: Optional[str] = Field(default="PIC")
    operator_role: Optional[str] = Field(default="STAKEHOLDER")


class SupplierInboundCreate(BaseModel):
    category: str = Field(..., example="KULIT")  # KULIT, DAGING, BUMBU, SAOS, KEMASAN, LAINNYA
    item_name: str = Field(..., example="Kulit Dimsum 9 cm (Mika)")
    quantity: float = Field(..., gt=0, example=5.0)
    unit: str = Field(default="BAL", example="BAL")
    supplier_name: str = Field(..., example="Sumber Kulit Rejeki")
    document_no: Optional[str] = Field(default=None, example="SJ-20261029-01")
    notes: Optional[str] = Field(default=None, example="Kemasan utuh dan bersih")
    received_by: Optional[str] = Field(default="PPIC-AYU")
    inbound_date: Optional[date] = None


class SupplierInboundResponse(BaseModel):
    inbound_id: int
    inbound_date: date
    category: str
    item_name: str
    quantity: float
    unit: str
    supplier_name: str
    document_no: Optional[str] = None
    notes: Optional[str] = None
    received_by: str
    created_at: datetime

    class Config:
        from_attributes = True


class SupplierOutboundCreate(BaseModel):
    inbound_id: Optional[int] = None
    lot_qr_code: Optional[str] = None
    category: str = Field(..., example="KULIT")
    item_name: str = Field(..., example="Kulit Dimsum 7 cm (Vakum)")
    quantity: float = Field(..., gt=0, example=1.0)
    unit: str = Field(default="BAL", example="BAL")
    target_station: Optional[str] = Field(default=None, example="ASSEMBLY_STATION")
    target_station_name: Optional[str] = Field(default=None, example="Meja Cetak")
    dispatched_by: Optional[str] = Field(default="PPIC-AYU")
    notes: Optional[str] = None
    outbound_date: Optional[date] = None


class SupplierOutboundResponse(BaseModel):
    outbound_id: int
    inbound_id: Optional[int] = None
    outbound_date: date
    lot_qr_code: str
    category: str
    item_name: str
    quantity: float
    unit: str
    target_station: Optional[str] = None
    target_station_name: Optional[str] = None
    dispatched_by: str
    notes: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class DivisionDailyNoteCreate(BaseModel):
    division: str = Field(..., example="DAGING")
    reject_count: int = Field(default=0, ge=0)
    reject_notes: Optional[str] = Field(default=None)
    operational_notes: Optional[str] = Field(default=None)
    created_by: Optional[str] = Field(default="PPIC-AYU")


class DivisionDailyNoteResponse(BaseModel):
    note_id: int
    record_date: date
    division: str
    reject_count: int
    reject_notes: Optional[str] = None
    operational_notes: Optional[str] = None
    created_by: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class GenerateDailyLotsAllRequest(BaseModel):
    plan_id: Optional[str] = None
    operator_id: Optional[str] = "PPIC-AYU"
    operator_role: Optional[str] = "STAKEHOLDER"
    idempotency_key: Optional[str] = None
    version: Optional[int] = None
    reason: Optional[str] = None
    gilingan_count: Optional[int] = None
    sack_count: Optional[int] = None
    wrapper_bal: Optional[int] = None
    sauce_bal: Optional[int] = None
    wrapper_9cm_bal: Optional[int] = None
    wrapper_7cm_bal: Optional[int] = None
    sauce_mika_bal: Optional[int] = None
    sauce_vakum_bal: Optional[int] = None


class AddProductionBatchPreviewRequest(BaseModel):
    plan_id: Optional[str] = None
    add_mika_packs: Optional[int] = 0
    add_vakum_packs: Optional[int] = 0
    manual_add_gilingan: Optional[int] = None
    manual_add_bumbu: Optional[int] = None
    manual_add_kulit: Optional[int] = None
    manual_add_saos: Optional[int] = None


class AddProductionBatchPreviewResponse(BaseModel):
    next_batch_no: int
    delta: dict
    cumulative_targets: dict
    already_issued_active: dict
    message: str


class AddProductionBatchRequest(BaseModel):
    plan_id: Optional[str] = None
    operator_id: Optional[str] = "PPIC-AYU"
    operator_role: Optional[str] = "STAKEHOLDER"
    idempotency_key: Optional[str] = None
    reason: Optional[str] = None
    add_mika_packs: Optional[int] = 0
    add_vakum_packs: Optional[int] = 0
    confirmed_delta_gilingan: Optional[int] = None
    confirmed_delta_bumbu: Optional[int] = None
    confirmed_delta_kulit: Optional[int] = None
    confirmed_delta_saos: Optional[int] = None




