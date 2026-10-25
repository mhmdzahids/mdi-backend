import os
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple, List
from sqlalchemy import select, update, desc
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status

from ..models import LotTracking, ScanSetting, ScanEvent, CustodyTransferLog

# Station definitions
STATION_METADATA = {
    "MEAT_STATION": {"name": "Daging / Giling", "target": "SEASONING_STATION"},
    "SEASONING_STATION": {"name": "Bumbu", "target": "MEAT_STATION"},
    "WRAPPER_STATION": {"name": "Kulit", "target": "ASSEMBLY_STATION"},
    "SAUCE_STATION": {"name": "Saos", "target": "FINISHING_STATION"}, # Saos ke Packing
    "ASSEMBLY_STATION": {"name": "Meja Cetak", "target": "FINISHING_STATION"},
    "FINISHING_STATION": {"name": "Meja Packing", "target": "FREEZER_STORAGE"},
}

def normalize_status(raw_status: Optional[str]) -> str:
    """
    Menormalkan status lot ke 4 model status utama:
    DICETAK -> DI_STOK -> KELUAR -> DITERIMA
    serta terminal status CONSUMED & CLOSED.
    """
    if not raw_status:
        return "DICETAK"
    s = raw_status.strip().upper()
    if s in ["PRINTED_PENDING_STOCK_IN", "DICETAK"]:
        return "DICETAK"
    if s.startswith("READY_IN_") or s == "DI_STOK":
        return "DI_STOK"
    if s.startswith("IN_TRANSIT_TO_") or s == "KELUAR":
        return "KELUAR"
    if s in [
        "IN_ASSEMBLY_STATION",
        "IN_FINISHING_STATION",
        "IN_MEAT_MIXING_STATION",
        "IN_SEASONING_STATION",
        "DITERIMA"
    ]:
        return "DITERIMA"
    if s in ["CONSUMED", "CLOSED"]:
        return s
    return s


def get_station_for_lot(lot: LotTracking) -> str:
    """
    Menentukan stasiun asal berdasarkan tipe lot atau current_station.
    """
    if lot.current_station and lot.current_station in STATION_METADATA:
        return lot.current_station
    lot_type = (lot.lot_type or "").upper()
    if lot_type == "DAGING":
        return "MEAT_STATION"
    elif lot_type == "BUMBU":
        return "SEASONING_STATION"
    elif lot_type == "KULIT":
        return "WRAPPER_STATION"
    elif lot_type == "SAOS":
        return "SAUCE_STATION"
    elif lot_type == "ADONAN":
        return "MEAT_STATION"
    elif lot_type in ["FG_MIKA", "FG_VAKUM"]:
        return "FINISHING_STATION"
    return "MEAT_STATION"


def get_target_station_for_lot(lot: LotTracking, origin_station: str) -> str:
    """
    Menentukan stasiun tujuan kiriman berdasarkan tipe lot.
    Saos WAJIB ke FINISHING_STATION (Packing), bukan Cetak!
    """
    lot_type = (lot.lot_type or "").upper()
    if lot_type == "SAOS" or lot.lot_qr_code.startswith("SA-"):
        return "FINISHING_STATION"
    if lot_type == "KULIT" or lot.lot_qr_code.startswith("KL-"):
        return "ASSEMBLY_STATION"
    if lot_type == "ADONAN" or lot.lot_qr_code.startswith("AD-"):
        return "ASSEMBLY_STATION"
    if lot_type == "BUMBU" or lot.lot_qr_code.startswith("BM-"):
        return "MEAT_STATION"
    if lot_type == "DAGING" or lot.lot_qr_code.startswith("DG-"):
        return "SEASONING_STATION"
    return STATION_METADATA.get(origin_station, {}).get("target", "FINISHING_STATION")


async def get_effective_scan_mode(db: AsyncSession, station_id: str) -> str:
    """
    Membaca mode scan stasiun langsung dari database pada setiap request (tanpa cache memori).
    Prioritas: Override Stasiun -> Global -> Default ('1X').
    """
    # 1. Cek override stasiun
    stmt_st = select(ScanSetting).where(ScanSetting.station_id == station_id)
    res_st = await db.execute(stmt_st)
    st_row = res_st.scalar_one_or_none()
    if st_row and st_row.scan_mode in ["1X", "2X"]:
        return st_row.scan_mode

    # 2. Cek default global
    stmt_gl = select(ScanSetting).where(ScanSetting.station_id == "GLOBAL")
    res_gl = await db.execute(stmt_gl)
    gl_row = res_gl.scalar_one_or_none()
    if gl_row and gl_row.scan_mode in ["1X", "2X"]:
        return gl_row.scan_mode

    return "1X"


async def execute_scan_transition(
    db: AsyncSession,
    lot_code: str,
    operator_station_id: str,
    user_id: str,
    user_role: str,
    notes: Optional[str] = None
) -> Dict[str, Any]:
    """
    Eksekutor tunggal transisi state machine scan:
    - Membaca mode scan langsung dari DB per request.
    - Idempotensi 3 detik (mencegah double scan cepat).
    - Memvalidasi transisi DICETAK -> DI_STOK -> KELUAR -> DITERIMA.
    - Menjalankan perubahan status dan pencatatan scan_events dalam 1 transaksi DB.
    """
    clean_code = (lot_code or "").strip().upper()
    if not clean_code:
        raise HTTPException(status_code=400, detail="Kode QR barcode tidak boleh kosong.")

    # 1. Ambil lot data
    stmt = select(LotTracking).where(LotTracking.lot_qr_code == clean_code)
    res = await db.execute(stmt)
    lot = res.scalar_one_or_none()
    if not lot:
        raise HTTPException(
            status_code=404,
            detail=f"Lot barcode '{clean_code}' tidak ditemukan di sistem."
        )

    # 2. IDEMPOTENSI: Cek apakah user & stasiun ini baru saja men-scan lot ini dalam 3 detik terakhir
    three_seconds_ago = datetime.utcnow() - timedelta(seconds=3)
    stmt_idem = select(ScanEvent).where(
        ScanEvent.lot_qr_code == clean_code,
        ScanEvent.station_id == operator_station_id,
        ScanEvent.user_id == user_id,
        ScanEvent.action != "UNDO",
        ScanEvent.timestamp >= three_seconds_ago
    ).order_by(desc(ScanEvent.event_id)).limit(1)

    res_idem = await db.execute(stmt_idem)
    last_event = res_idem.scalar_one_or_none()
    if last_event:
        # Kembalikan hasil scan sebelumnya (idempotent, no-op)
        return {
            "success": True,
            "is_duplicate": True,
            "lot_code": clean_code,
            "action": last_event.action,
            "previous_status": last_event.previous_status,
            "current_status": last_event.new_status,
            "mode": last_event.mode_applied,
            "message": f"Scan duplikat terdeteksi (diabaikan): Status '{last_event.new_status}'.",
            "can_undo": True,
            "undo_expires_in_sec": max(1, 15 - int((datetime.utcnow() - last_event.timestamp).total_seconds()))
        }

    # 3. Tentukan stasiun asal, tujuan, dan mode scan aktif
    origin_station = get_station_for_lot(lot)
    target_station = get_target_station_for_lot(lot, origin_station)
    mode = await get_effective_scan_mode(db, origin_station)
    current_norm = normalize_status(lot.current_status)

    target_name = STATION_METADATA.get(target_station, {}).get("name", target_station)
    origin_name = STATION_METADATA.get(origin_station, {}).get("name", origin_station)

    # 4. Evaluasi State Machine Sesuai Mode & Status Saat Ini
    # Aturan Wajib:
    # - Mode 1x: DICETAK -> KELUAR
    # - Mode 2x: DICETAK -> DI_STOK. Scan berikutnya DI_STOK -> KELUAR.
    # - Scan pada DI_STOK menghasilkan KELUAR di kedua mode (mencegah alur putus).
    new_status = None
    action_type = None
    feedback_message = None
    semantic_variant = "info" # 'neutral' | 'info' | 'warning' | 'danger' | 'success'

    if current_norm == "DICETAK":
        if mode == "1X":
            # Mode 1x: Langsung keluar
            new_status = f"IN_TRANSIT_TO_{target_station.replace('_STATION', '')}" if not target_station.startswith("IN_TRANSIT") else "KELUAR"
            # Format status seragam untuk interoperabilitas
            if new_status.startswith("IN_TRANSIT_TO_FINISHING"):
                new_status = "IN_TRANSIT_TO_FINISHING"
            elif new_status.startswith("IN_TRANSIT_TO_ASSEMBLY"):
                new_status = "IN_TRANSIT_TO_ASSEMBLY"
            elif new_status.startswith("IN_TRANSIT_TO_MEAT"):
                new_status = "IN_TRANSIT_TO_MEAT_MIXING"
            elif new_status.startswith("IN_TRANSIT_TO_SEASONING"):
                new_status = "IN_TRANSIT_TO_SEASONING"
            else:
                new_status = "KELUAR"

            action_type = "SCAN_OUT"
            feedback_message = f"Keluar: Barang dikirim langsung menuju {target_name} (Mode 1x)."
            semantic_variant = "warning"
        else:
            # Mode 2x: Masuk stok fisik stasiun terlebih dahulu
            new_status = f"READY_IN_{origin_station}" if not origin_station.startswith("READY_IN_") else "DI_STOK"
            action_type = "CONFIRM_STOCK"
            feedback_message = f"Masuk stok: Fisik barang terkonfirmasi ada di {origin_name} (Mode 2x)."
            semantic_variant = "info"

    elif current_norm == "DI_STOK":
        # Baik Mode 1x maupun Mode 2x: Scan DI_STOK -> KELUAR
        if target_station == "FINISHING_STATION":
            new_status = "IN_TRANSIT_TO_FINISHING"
        elif target_station == "ASSEMBLY_STATION":
            new_status = "IN_TRANSIT_TO_ASSEMBLY"
        elif target_station == "MEAT_STATION":
            new_status = "IN_TRANSIT_TO_MEAT_MIXING"
        elif target_station == "SEASONING_STATION":
            new_status = "IN_TRANSIT_TO_SEASONING"
        else:
            new_status = "KELUAR"

        action_type = "SCAN_OUT"
        feedback_message = f"Keluar: Barang dikirim menuju {target_name}."
        semantic_variant = "warning"

    elif current_norm == "KELUAR":
        # Jika stasiun tujuan (atau operator penerima) melakukan scan: Masuk DITERIMA
        is_receiving = (
            operator_station_id == target_station or
            (target_station == "FINISHING_STATION" and operator_station_id in ["FINISHING_STATION", "PROD_2"]) or
            (target_station == "ASSEMBLY_STATION" and operator_station_id in ["ASSEMBLY_STATION", "PROD_2"]) or
            (target_station == "MEAT_STATION" and operator_station_id in ["MEAT_STATION", "GILING"])
        )

        if is_receiving:
            if target_station == "FINISHING_STATION":
                new_status = "IN_FINISHING_STATION"
                lot.current_station = "FINISHING_STATION"
            elif target_station == "ASSEMBLY_STATION":
                new_status = "IN_ASSEMBLY_STATION"
                lot.current_station = "ASSEMBLY_STATION"
            elif target_station == "MEAT_STATION":
                new_status = "IN_MEAT_MIXING_STATION"
                lot.current_station = "MEAT_STATION"
            elif target_station == "SEASONING_STATION":
                new_status = "IN_SEASONING_STATION"
                lot.current_station = "SEASONING_STATION"
            else:
                new_status = "DITERIMA"
                lot.current_station = target_station

            action_type = "SCAN_IN"
            feedback_message = f"Diterima: Bahan telah diterima di {target_name}."
            semantic_variant = "success"
        else:
            # Stasiun asal scan ulang barang yang sudah dikirim keluar
            raise HTTPException(
                status_code=400,
                detail=f"Ditolak: Barang sudah berstatus KELUAR menuju {target_name}. Menunggu scan terima di stasiun tujuan."
            )

    elif current_norm == "DITERIMA":
        raise HTTPException(
            status_code=400,
            detail=f"Ditolak: Barang sudah DITERIMA dan aktif di stasiun {lot.current_station}."
        )

    elif current_norm in ["CONSUMED", "CLOSED"]:
        raise HTTPException(
            status_code=400,
            detail=f"Ditolak: Barang sudah selesai diproses ({current_norm})."
        )
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Ditolak: Status barang '{lot.current_status}' tidak valid untuk scan ini."
        )

    # 5. Eksekusi Atomic Transaction (Perubahan status + logging scan_event + custody_log)
    prev_status = lot.current_status
    lot.current_status = new_status

    scan_event = ScanEvent(
        lot_qr_code=lot.lot_qr_code,
        station_id=operator_station_id,
        user_id=user_id,
        action=action_type,
        mode_applied=mode,
        previous_status=prev_status,
        new_status=new_status,
        notes=notes or feedback_message
    )
    db.add(scan_event)

    custody_log = CustodyTransferLog(
        lot_qr_code=lot.lot_qr_code,
        action_type=action_type,
        station_id=operator_station_id,
        target_station=target_station if action_type == "SCAN_OUT" else None,
        user_id=user_id,
        user_role_active=user_role,
        notes=notes or feedback_message
    )
    db.add(custody_log)

    await db.commit()
    await db.refresh(lot)

    return {
        "success": True,
        "is_duplicate": False,
        "lot_code": clean_code,
        "action": action_type,
        "previous_status": prev_status,
        "current_status": new_status,
        "normalized_status": normalize_status(new_status),
        "mode": mode,
        "message": feedback_message,
        "semantic_variant": semantic_variant,
        "can_undo": True,
        "undo_expires_in_sec": 15
    }


async def execute_undo_scan(
    db: AsyncSession,
    lot_code: str,
    user_id: str
) -> Dict[str, Any]:
    """
    Membatalkan scan terakhir suatu lot:
    - Maksimal 15 detik setelah scan.
    - Hanya oleh user yang sama.
    - Menuliskan event UNDO ke scan_events.
    - Ditolak jika status lot sudah berubah lagi setelahnya.
    """
    clean_code = (lot_code or "").strip().upper()
    if not clean_code:
        raise HTTPException(status_code=400, detail="Kode QR barcode tidak boleh kosong.")

    stmt = select(LotTracking).where(LotTracking.lot_qr_code == clean_code)
    res = await db.execute(stmt)
    lot = res.scalar_one_or_none()
    if not lot:
        raise HTTPException(status_code=404, detail=f"Lot '{clean_code}' tidak ditemukan.")

    # Cari event scan terakhir bukan UNDO
    stmt_ev = select(ScanEvent).where(
        ScanEvent.lot_qr_code == clean_code,
        ScanEvent.action != "UNDO"
    ).order_by(desc(ScanEvent.event_id)).limit(1)

    res_ev = await db.execute(stmt_ev)
    last_event = res_ev.scalar_one_or_none()

    if not last_event:
        raise HTTPException(status_code=400, detail="Tidak ada riwayat scan untuk dibatalkan.")

    # Validasi 1: User harus sama
    if last_event.user_id != user_id:
        raise HTTPException(
            status_code=403,
            detail=f"Undo ditolak: Hanya operator yang melakukan scan ({last_event.user_id}) yang dapat membatalkannya."
        )

    # Validasi 2: Waktu maksimal 15 detik
    now = datetime.utcnow()
    elapsed = (now - last_event.timestamp).total_seconds()
    if elapsed > 15.0:
        raise HTTPException(
            status_code=400,
            detail=f"Batas waktu pembatalan (15 detik) telah habis ({int(elapsed)} detik yang lalu)."
        )

    # Validasi 3: Status lot saat ini harus masih sama dengan new_status event tersebut
    if lot.current_status != last_event.new_status:
        raise HTTPException(
            status_code=400,
            detail="Barang telah berpindah status lebih lanjut, scan tidak dapat dibatalkan lagi."
        )

    # Eksekusi Revert
    reverted_status = last_event.previous_status
    prev_status = lot.current_status
    lot.current_status = reverted_status

    undo_event = ScanEvent(
        lot_qr_code=clean_code,
        station_id=last_event.station_id,
        user_id=user_id,
        action="UNDO",
        mode_applied=last_event.mode_applied,
        previous_status=prev_status,
        new_status=reverted_status,
        notes=f"Undo scan {last_event.action} oleh {user_id}"
    )
    db.add(undo_event)

    custody_undo = CustodyTransferLog(
        lot_qr_code=clean_code,
        action_type="SCAN_UNDO",
        station_id=last_event.station_id,
        user_id=user_id,
        user_role_active="OPERATOR",
        notes=f"Membatalkan {last_event.action}"
    )
    db.add(custody_undo)

    await db.commit()
    await db.refresh(lot)

    return {
        "success": True,
        "lot_code": clean_code,
        "message": f"Scan berhasil dibatalkan. Status dikembalikan ke '{reverted_status}'.",
        "current_status": reverted_status,
        "normalized_status": normalize_status(reverted_status)
    }


async def get_station_stock_counts(db: AsyncSession, station_id: str) -> Dict[str, Any]:
    """
    Fungsi terpusat menghitung stok sesuai mode scan yang berlaku di stasiun:
    - Mode 1x: Stok = DICETAK + DI_STOK.
    - Mode 2x: Stok = hanya DI_STOK. Item DICETAK dihitung terpisah sebagai 'menunggu_konfirmasi'.
    """
    mode = await get_effective_scan_mode(db, station_id)

    # Ambil semua lot aktif stasiun
    stmt = select(LotTracking).where(
        LotTracking.current_station == station_id,
        LotTracking.current_status.notin_(["CONSUMED", "CLOSED"])
    )
    res = await db.execute(stmt)
    lots = res.scalars().all()

    printed_count = 0
    stock_count = 0
    in_transit_count = 0

    for lot in lots:
        norm = normalize_status(lot.current_status)
        if norm == "DICETAK":
            printed_count += 1
        elif norm == "DI_STOK":
            stock_count += 1
        elif norm == "KELUAR":
            in_transit_count += 1

    if mode == "1X":
        # Mode 1x: DICETAK langsung diakui sebagai stok aktif
        active_stock = printed_count + stock_count
        pending_confirmation = 0
    else:
        # Mode 2x: Hanya DI_STOK yang diakui stok, DICETAK menunggu scan
        active_stock = stock_count
        pending_confirmation = printed_count

    return {
        "station_id": station_id,
        "scan_mode": mode,
        "active_stock_count": active_stock,
        "pending_confirmation_count": pending_confirmation,
        "in_transit_count": in_transit_count,
        "details": {
            "dicetak": printed_count,
            "di_stok": stock_count,
            "keluar": in_transit_count
        }
    }
