"""
Permission Registry - Dimsum MES & Traceability
Satu sumber kebenaran (single source of truth) untuk seluruh izin akses (permissions)
dan template role sistem pada aplikasi.
"""

from typing import List, Dict, Any

# 1. CENTRAL PERMISSIONS LIST
# Format kode standar: "feature:action"
PERMISSIONS: List[Dict[str, str]] = [
    # --- Rencana Produksi (Plan) ---
    {
        "code": "plan:view",
        "name": "Lihat Rencana Produksi",
        "feature": "plan",
        "action": "view",
        "description": "Melihat detail target dan rencana produksi harian"
    },
    {
        "code": "plan:create",
        "name": "Buat Rencana Produksi",
        "feature": "plan",
        "action": "create",
        "description": "Membuat draft rencana produksi harian baru"
    },
    {
        "code": "plan:update",
        "name": "Ubah Rencana Produksi",
        "feature": "plan",
        "action": "update",
        "description": "Mengedit target order, bumper, dan takaran draft rencana"
    },
    {
        "code": "plan:publish",
        "name": "Publikasikan Rencana Produksi",
        "feature": "plan",
        "action": "publish",
        "description": "Mempublikasikan dan mengunci status rencana produksi harian"
    },
    {
        "code": "plan:add_batch",
        "name": "Tambah Batch Susulan",
        "feature": "plan",
        "action": "add_batch",
        "description": "Menambahkan batch susulan pada rencana produksi aktif"
    },
    {
        "code": "plan:void_batch",
        "name": "Batalkan / Void Batch",
        "feature": "plan",
        "action": "void_batch",
        "description": "Membatalkan (void) nomor batch produksi yang aktif"
    },

    # --- Barcode & Label QR ---
    {
        "code": "barcode:generate",
        "name": "Terbitkan Barcode Lot",
        "feature": "barcode",
        "action": "generate",
        "description": "Menerbitkan kode lot QR untuk daging, bumbu, kulit, saos, dan finishing"
    },

    # --- Rekap Catatan & Reject PPIC ---
    {
        "code": "ppic_recap:view",
        "name": "Lihat Rekap PPIC",
        "feature": "ppic_recap",
        "action": "view",
        "description": "Melihat rangkuman harian, status stasiun, dan catatan reject divisi"
    },
    {
        "code": "ppic_recap:notes",
        "name": "Kelola Catatan & Reject Divisi",
        "feature": "ppic_recap",
        "action": "notes",
        "description": "Menulis dan mengedit catatan operasional serta jumlah reject divisi"
    },
    {
        "code": "ppic_recap:export",
        "name": "Ekspor Laporan Rekap Excel",
        "feature": "ppic_recap",
        "action": "export",
        "description": "Mengekspor laporan rekap harian atau periode ke file Excel"
    },

    # --- Logistik Inbound (Barang Masuk Supplier) ---
    {
        "code": "inbound:view",
        "name": "Lihat Barang Masuk",
        "feature": "inbound",
        "action": "view",
        "description": "Melihat riwayat penerimaan barang masuk dari supplier"
    },
    {
        "code": "inbound:create",
        "name": "Input Barang Masuk",
        "feature": "inbound",
        "action": "create",
        "description": "Mencatat surat jalan dan penerimaan material dari supplier luar"
    },
    {
        "code": "inbound:export",
        "name": "Ekspor Barang Masuk Excel",
        "feature": "inbound",
        "action": "export",
        "description": "Mengekspor riwayat barang masuk ke format Excel"
    },

    # --- Logistik Outbound (Barang Keluar ke Lantai Produksi) ---
    {
        "code": "outbound:view",
        "name": "Lihat Barang Keluar",
        "feature": "outbound",
        "action": "view",
        "description": "Melihat riwayat pengeluaran material ke stasiun produksi"
    },
    {
        "code": "outbound:create",
        "name": "Input Barang Keluar",
        "feature": "outbound",
        "action": "create",
        "description": "Mencatat mutasi pengeluaran bahan baku ke ruangan produksi"
    },
    {
        "code": "outbound:export",
        "name": "Ekspor Barang Keluar Excel",
        "feature": "outbound",
        "action": "export",
        "description": "Mengekspor riwayat barang keluar ke format Excel"
    },

    # --- Inventory Bahan Baku ---
    {
        "code": "inventory:view",
        "name": "Lihat Stok Inventory",
        "feature": "inventory",
        "action": "view",
        "description": "Melihat sisa saldo stok bahan baku (kulit, bumbu, daging, saos)"
    },
    {
        "code": "inventory:export",
        "name": "Ekspor Inventory Excel",
        "feature": "inventory",
        "action": "export",
        "description": "Mengekspor laporan stok persediaan gudang ke Excel"
    },

    # --- Divisi Produksi 1: Daging (Giling & Mix) ---
    {
        "code": "meat:view",
        "name": "Lihat Stasiun Daging",
        "feature": "meat",
        "action": "view",
        "description": "Melihat monitor kerja dan wadah stasiun giling daging"
    },
    {
        "code": "meat:operate",
        "name": "Operasi Giling & Mixing Adonan",
        "feature": "meat",
        "action": "operate",
        "description": "Membuat wadah gilingan, mencampur bumbu, dan memproduksi adonan"
    },
    {
        "code": "meat:scan",
        "name": "Scan Masuk / Keluar Daging",
        "feature": "meat",
        "action": "scan",
        "description": "Melakukan scan transfer daging atau penerimaan bumbu"
    },

    # --- Divisi Produksi 1: Bumbu Racik ---
    {
        "code": "seasoning:view",
        "name": "Lihat Stasiun Bumbu",
        "feature": "seasoning",
        "action": "view",
        "description": "Melihat monitor kerja racikan bumbu Sparta"
    },
    {
        "code": "seasoning:operate",
        "name": "Operasi Racik Bumbu",
        "feature": "seasoning",
        "action": "operate",
        "description": "Membuat kemasan karung/pouch bumbu dan konfirmasi stok"
    },
    {
        "code": "seasoning:scan",
        "name": "Scan Masuk / Keluar Bumbu",
        "feature": "seasoning",
        "action": "scan",
        "description": "Melakukan scan transfer bumbu ke stasiun mixer"
    },

    # --- Divisi Produksi 1: Kulit Pangsit ---
    {
        "code": "wrapper:view",
        "name": "Lihat Stasiun Kulit",
        "feature": "wrapper",
        "action": "view",
        "description": "Melihat monitor kerja stasiun kulit pangsit"
    },
    {
        "code": "wrapper:operate",
        "name": "Operasi Siap Kulit Pangsit",
        "feature": "wrapper",
        "action": "operate",
        "description": "Mempersiapkan bal kulit 7cm dan 9cm untuk meja cetak"
    },
    {
        "code": "wrapper:scan",
        "name": "Scan Keluar Kulit",
        "feature": "wrapper",
        "action": "scan",
        "description": "Melakukan scan transfer bal kulit ke Produksi 2"
    },

    # --- Divisi Produksi 1: Saos Dimsum ---
    {
        "code": "sauce:view",
        "name": "Lihat Stasiun Saos",
        "feature": "sauce",
        "action": "view",
        "description": "Melihat monitor kerja stasiun saos dimsum"
    },
    {
        "code": "sauce:operate",
        "name": "Operasi Siap Saos",
        "feature": "sauce",
        "action": "operate",
        "description": "Mempersiapkan bal saos botol/sachet untuk finishing packing"
    },
    {
        "code": "sauce:scan",
        "name": "Scan Keluar Saos",
        "feature": "sauce",
        "action": "scan",
        "description": "Melakukan scan transfer bal saos ke Produksi 2"
    },

    # --- Produksi 2: Cetak & Assembly ---
    {
        "code": "prod2_assembly:view",
        "name": "Lihat Meja Cetak & Assembly",
        "feature": "prod2_assembly",
        "action": "view",
        "description": "Melihat status material masuk dan nampan di meja cetak"
    },
    {
        "code": "prod2_assembly:operate",
        "name": "Operasi Meja Cetak",
        "feature": "prod2_assembly",
        "action": "operate",
        "description": "Menerima material (adonan/kulit) dan menyelesaikan cetak nampan"
    },

    # --- Produksi 2: Packing & Checker ---
    {
        "code": "prod2_packing:view",
        "name": "Lihat Meja Packing",
        "feature": "prod2_packing",
        "action": "view",
        "description": "Melihat antrean nampan masuk dan verifikasi packing"
    },
    {
        "code": "prod2_packing:operate",
        "name": "Operasi Meja Packing",
        "feature": "prod2_packing",
        "action": "operate",
        "description": "Menerima nampan, memverifikasi pack mika/vakum, dan menerbitkan finished good"
    },

    # --- Gudang Freezer & Sales (POS) ---
    {
        "code": "freezer_sales:view",
        "name": "Lihat Stok Freezer & POS",
        "feature": "freezer_sales",
        "action": "view",
        "description": "Melihat stok barang jadi di freezer dan serah terima penjualan"
    },
    {
        "code": "freezer_sales:update",
        "name": "Ubah Produk POS",
        "feature": "freezer_sales",
        "action": "update",
        "description": "Mengubah nama, deskripsi, foto, atau status aktif produk POS"
    },
    {
        "code": "freezer_sales:view_price",
        "name": "Lihat Harga Produk",
        "feature": "freezer_sales",
        "action": "view_price",
        "description": "Melihat informasi harga produk pada katalog POS"
    },
    {
        "code": "freezer_sales:edit_price",
        "name": "Ubah Harga Produk",
        "feature": "freezer_sales",
        "action": "edit_price",
        "description": "Mengubah nominal harga produk pada katalog POS"
    },
    {
        "code": "freezer_sales:delete",
        "name": "Hapus Produk POS",
        "feature": "freezer_sales",
        "action": "delete",
        "description": "Menghapus produk dari katalog master POS & Website"
    },
    {
        "code": "freezer_sales:operate",
        "name": "Operasi Penerimaan Sales",
        "feature": "freezer_sales",
        "action": "operate",
        "description": "Memverifikasi serah terima fisik pack mika/vakum di freezer"
    },

    # --- POS (Point of Sale & Multi-Channel) ---
    {
        "code": "pos:view",
        "name": "Lihat Kasir & POS",
        "feature": "pos",
        "action": "view",
        "description": "Membuka halaman POS kasir dan melihat ringkasan penjualan"
    },
    {
        "code": "pos:operate",
        "name": "Operasi Penjualan & Scan POS",
        "feature": "pos",
        "action": "operate",
        "description": "Melakukan scan kemasan dan memproses transaksi kasir langsung"
    },
    {
        "code": "pos:manage_orders",
        "name": "Kelola Antrean Ojol & Web",
        "feature": "pos",
        "action": "manage_orders",
        "description": "Menerima dan memproses scan pemenuhan order ShopeeFood, GrabFood, dan Website"
    },
    {
        "code": "pos:view_revenue",
        "name": "Lihat Omzet & Rekap Finansial POS",
        "feature": "pos",
        "action": "view_revenue",
        "description": "Melihat total omzet rupiah, rekap keuangan kasir, dan laporan penjualan per channel (Standar Loss Prevention)"
    },
    {
        "code": "pos:api_keys",
        "name": "Kelola API Key Website",
        "feature": "pos",
        "action": "api_keys",
        "description": "Membuat dan mencabut API Key koneksi website eksternal"
    },

    # --- Promosi Website (Voucher & Banner) ---
    {
        "code": "promotions:view",
        "name": "Lihat Voucher & Banner Promosi",
        "feature": "promotions",
        "action": "view",
        "description": "Melihat daftar kupon voucher dan banner promosi website buyer"
    },
    {
        "code": "promotions:manage",
        "name": "Kelola Voucher & Banner Promosi",
        "feature": "promotions",
        "action": "manage",
        "description": "Membuat, mengedit, dan menghapus voucher kupon serta banner website buyer"
    },


    # --- Scanner Operasional (1X / 2X Mode) ---
    {
        "code": "scan:operate",
        "name": "Gunakan Scanner Barcode",
        "feature": "scan",
        "action": "operate",
        "description": "Melakukan scan masuk, keluar, atau undo status barcode lot"
    },
    {
        "code": "scan:settings",
        "name": "Ubah Mode Scan (1X / 2X)",
        "feature": "scan",
        "action": "settings",
        "description": "Mengatur konfigurasi scanner dinamis global maupun per stasiun"
    },

    # --- Log Perpindahan & Tracking ---
    {
        "code": "logs:view",
        "name": "Lihat Log Audit Barcode",
        "feature": "logs",
        "action": "view",
        "description": "Melihat jejak custody transfer, event scan, dan selisih (discrepancy)"
    },

    # --- Profil Pengguna ---
    {
        "code": "profile:view",
        "name": "Lihat Profil Sendiri",
        "feature": "profile",
        "action": "view",
        "description": "Melihat ringkasan informasi dan masa aktif akun login"
    },
    {
        "code": "profile:change_password",
        "name": "Ubah Password Sendiri",
        "feature": "profile",
        "action": "change_password",
        "description": "Mengubah password akun pribadi dengan verifikasi keamanan"
    },

    # --- Manajemen Super Admin: Pengguna (Users) ---
    {
        "code": "admin_users:view",
        "name": "Lihat Daftar Pengguna",
        "feature": "admin_users",
        "action": "view",
        "description": "Melihat seluruh akun pegawai, status aktif, dan role"
    },
    {
        "code": "admin_users:create",
        "name": "Tambah Akun Pengguna",
        "feature": "admin_users",
        "action": "create",
        "description": "Menambahkan akun pegawai baru ke dalam sistem"
    },
    {
        "code": "admin_users:update",
        "name": "Ubah Akun Pengguna",
        "feature": "admin_users",
        "action": "update",
        "description": "Mengubah data nama, divisi, role, status aktif, atau reset password pengguna"
    },
    {
        "code": "admin_users:delete",
        "name": "Hapus Akun Pengguna",
        "feature": "admin_users",
        "action": "delete",
        "description": "Menghapus atau menonaktifkan akun pegawai dari sistem"
    },

    # --- Manajemen Super Admin: Peran (Roles) & Izin Akses ---
    {
        "code": "admin_roles:view",
        "name": "Lihat Role & Izin Akses",
        "feature": "admin_roles",
        "action": "view",
        "description": "Melihat daftar role dan pemetaan izin akses sistem"
    },
    {
        "code": "admin_roles:create",
        "name": "Buat Role Baru",
        "feature": "admin_roles",
        "action": "create",
        "description": "Membuat peran pengguna kustom baru beserta paket izinnya"
    },
    {
        "code": "admin_roles:update",
        "name": "Ubah Role & Izin",
        "feature": "admin_roles",
        "action": "update",
        "description": "Mengubah nama, deskripsi, dan daftar izin akses pada role"
    },
    {
        "code": "admin_roles:delete",
        "name": "Hapus Role Kustom",
        "feature": "admin_roles",
        "action": "delete",
        "description": "Menghapus role kustom yang tidak memiliki relasi akun aktif"
    },

    # --- Manajemen Super Admin: Audit Aktivitas ---
    {
        "code": "admin_audit_logs:view",
        "name": "Lihat Log Audit Aktivitas",
        "feature": "admin_audit_logs",
        "action": "view",
        "description": "Melihat rekaman audit seluruh login, mutasi user/role, dan akses ditolak"
    },

    # --- Developer & Pemeliharaan Sistem ---
    {
        "code": "system:dev_wipe",
        "name": "Wipe Database Developer",
        "feature": "system",
        "action": "dev_wipe",
        "description": "Fungsi pembersihan data pengujian khusus developer"
    }
]

# Quick code lookup set
ALL_PERMISSION_CODES = {p["code"] for p in PERMISSIONS}


# 2. INITIAL ROLE TEMPLATES
# Diberi flag is_system = True untuk melindungi integritas sistem
SYSTEM_ROLE_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "Super Admin": {
        "description": "Akses tertinggi sistem dengan kewenangan penuh atas seluruh modul dan pengaturan.",
        "is_system": True,
        # Super admin role memiliki semua izin
        "permissions": list(ALL_PERMISSION_CODES)
    },
    "PPIC": {
        "description": "Perencanaan produksi, penerbitan barcode lot, kontrol persediaan, dan rekapitulasi harian.",
        "is_system": True,
        "permissions": [
            "plan:view", "plan:create", "plan:update", "plan:publish", "plan:add_batch", "plan:void_batch",
            "barcode:generate",
            "ppic_recap:view", "ppic_recap:notes", "ppic_recap:export",
            "inbound:view", "inbound:create", "inbound:export",
            "outbound:view", "outbound:create", "outbound:export",
            "inventory:view", "inventory:export",
            "meat:view", "seasoning:view", "wrapper:view", "sauce:view",
            "prod2_assembly:view", "prod2_packing:view", "freezer_sales:view", "freezer_sales:view_price",
            "scan:operate", "scan:settings",
            "logs:view",
            "profile:view", "profile:change_password"
        ]
    },
    "Gudang / Penerima Barang": {
        "description": "Logistik penerimaan barang masuk supplier, pengeluaran bahan ke lantai produksi, dan stok freezer.",
        "is_system": True,
        "permissions": [
            "inbound:view", "inbound:create", "inbound:export",
            "outbound:view", "outbound:create", "outbound:export",
            "inventory:view", "inventory:export",
            "freezer_sales:view", "freezer_sales:update", "freezer_sales:view_price", "freezer_sales:edit_price", "freezer_sales:delete", "freezer_sales:operate",
            "scan:operate",
            "logs:view",
            "profile:view", "profile:change_password"
        ]
    },
    "Kasir": {
        "description": "Operasional kasir, penjualan langsung, scan-out pesanan ojek online (ShopeeFood, GrabFood, GoFood), dan website. Tidak memiliki akses ke total omzet kumulatif (standar blind cash control).",
        "is_system": True,
        "permissions": [
            "pos:view", "pos:operate", "pos:manage_orders",
            "freezer_sales:view_price",
            "profile:view", "profile:change_password"
        ]
    },
    "PIC Kasir": {
        "description": "Supervisor kasir, rekap omzet harian POS, rekonsiliasi kas (Z-Report), audit penjualan lintas channel, serta wewenang pengaturan harga dan diskon produk.",
        "is_system": True,
        "permissions": [
            "pos:view", "pos:operate", "pos:manage_orders", "pos:view_revenue",
            "freezer_sales:view", "freezer_sales:update", "freezer_sales:view_price", "freezer_sales:edit_price",
            "profile:view", "profile:change_password"
        ]
    },
    "Produksi Giling & Mix": {
        "description": "Operasional penggilingan daging ayam, mixing adonan bumbu, dan transfer wadah gilingan.",
        "is_system": True,
        "permissions": [
            "meat:view", "meat:operate", "meat:scan",
            "scan:operate",
            "logs:view",
            "profile:view", "profile:change_password"
        ]
    },
    "Produksi Bumbu": {
        "description": "Operasional peracikan bumbu Sparta, kemasan karung/pouch, dan serah terima ke mixer.",
        "is_system": True,
        "permissions": [
            "seasoning:view", "seasoning:operate", "seasoning:scan",
            "scan:operate",
            "logs:view",
            "profile:view", "profile:change_password"
        ]
    },
    "Produksi Kulit Dimsum": {
        "description": "Persiapan dan penyerahan bal kulit pangsit 7cm & 9cm untuk meja cetak.",
        "is_system": True,
        "permissions": [
            "wrapper:view", "wrapper:operate", "wrapper:scan",
            "scan:operate",
            "logs:view",
            "profile:view", "profile:change_password"
        ]
    },
    "Produksi Saos": {
        "description": "Persiapan dan penyerahan bal saos dimsum botol/sachet untuk meja packing.",
        "is_system": True,
        "permissions": [
            "sauce:view", "sauce:operate", "sauce:scan",
            "scan:operate",
            "logs:view",
            "profile:view", "profile:change_password"
        ]
    },
    "Meja Cetak": {
        "description": "Penerimaan adonan dan kulit, proses pencetakan dimsum ke nampan stainless.",
        "is_system": True,
        "permissions": [
            "prod2_assembly:view", "prod2_assembly:operate",
            "scan:operate",
            "logs:view",
            "profile:view", "profile:change_password"
        ]
    },
    "Meja Packing": {
        "description": "Pemeriksaan nampan, packing pack mika & vakum, dan penerbitan produk jadi (FG).",
        "is_system": True,
        "permissions": [
            "prod2_packing:view", "prod2_packing:operate",
            "scan:operate",
            "logs:view",
            "profile:view", "profile:change_password"
        ]
    },
    "Viewer": {
        "description": "Akses baca saja (read-only) untuk memantau aktivitas produksi di seluruh lini pabrik.",
        "is_system": True,
        "permissions": [
            "plan:view",
            "ppic_recap:view",
            "inbound:view",
            "outbound:view",
            "inventory:view",
            "meat:view",
            "seasoning:view",
            "wrapper:view",
            "sauce:view",
            "prod2_assembly:view",
            "prod2_packing:view",
            "freezer_sales:view",
            "freezer_sales:view_price",
            "logs:view",
            "profile:view"
        ]
    }
}
