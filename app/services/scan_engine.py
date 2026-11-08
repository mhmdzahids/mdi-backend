import os
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple, List
from sqlalchemy import select, update, desc
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status

from ..models import LotTracking, ScanSetting, ScanEvent, CustodyTransferLog

# Station definitions
STATION_METADATA = {
    "MEAT_STATION": {"name": "Daging / Giling", "target": "ASSEMBLY_STATION"},
    "SEASONING_STATION": {"name": "Bumbu Racik", "target": "MEAT_STATION"},
    "WRAPPER_STATION": {"name": "Kulit Dimsum", "target": "ASSEMBLY_STATION"},
    "SAUCE_STATION": {"name": "Saos Dimsum", "target": "FINISHING_STATION"},
    "ASSEMBLY_STATION": {"name": "Meja Cetak", "target": "FINISHING_STATION"},
    "FINISHING_STATION": {"name": "Meja Packing", "target": "FREEZER_STORAGE"},
}


def normalize_station_id(station_id: Optional[str]) -> str:
    """Normalisasi alias stasiun dari frontend / payload ke format stasiun baku."""
    if not station_id:
        return "MEAT_STATION"
    s = station_id.strip().upper()
    if s in ["MEAT", "GILING", "MEAT_STATION", "MEAT_MIXING_STATION"]:
        return "MEAT_STATION"
    if s in ["SEASONING", "BUMBU", "SEASONING_STATION"]:
        return "SEASONING_STATION"
    if s in ["WRAPPER", "KULIT", "WRAPPER_STATION"]:
        return "WRAPPER_STATION"
    if s in ["SAUCE", "SAOS", "SAUCE_STATION"]:
        return "SAUCE_STATION"
    if s in ["ASSEMBLY", "CETAK", "ASSEMBLY_STATION", "PROD_2"]:
        return "ASSEMBLY_STATION"
    if s in ["FINISHING", "PACKING", "FINISHING_STATION", "PACKING_STATION"]:
        return "FINISHING_STATION"
    return s


def normalize_status(raw_status: Optional[str]) -> str:
    """
    Menormalkan status lot ke 4 model status utama:
    DICETAK -> DI_STOK -> KELUAR -> DITERIMA
    serta terminal status CONSUMED & CLOSED.
    """
    if not raw_status:
        return "DICETAK"
    s = raw_status.strip().upper()
    if s in [
        "PRINTED_PENDING_STOCK_IN",
        "DICETAK",
        "PRINTED",
        "DOUGH_MIXED",
        "WRAPPER_READY",
        "SAUCE_READY",
        "READY_IN_ASSEMBLY_STATION"
    ]:
        return "DICETAK"
    if s.startswith("READY_IN_") or s == "DI_STOK" or s == "STOCKED_IN":
        return "DI_STOK"
    if s.startswith("IN_TRANSIT_TO_") or s == "KELUAR":
        return "KELUAR"
    if s in [
        "IN_ASSEMBLY_STATION",
        "IN_FINISHING_STATION",
        "IN_MEAT_MIXING_STATION",
        "IN_SEASONING_STATION",
        "DITERIMA",
        "PACKED_VERIFIED",
        "IN_SALES_STOCK"
    ]:
        return "DITERIMA"
    if s in ["CONSUMED", "CLOSED", "VOID"]:
        return s
    return s


def get_station_for_lot(lot: LotTracking) -> str:
    """
    Menentukan stasiun asal berdasarkan tipe lot atau kode barcode QR.
    """
    qr = (lot.lot_qr_code or "").upper()
    lot_type = (lot.lot_type or "").upper()

    if lot_type == "SAOS" or qr.startswith("SS-") or qr.startswith("SA-"):
        return "SAUCE_STATION"
    if lot_type == "KULIT" or qr.startswith("KL-"):
        return "WRAPPER_STATION"
    if lot_type == "BUMBU" or qr.startswith("BM-") or qr.startswith("SP-"):
        return "SEASONING_STATION"
    if lot_type in ["DAGING", "ADONAN"] or qr.startswith("DG-") or qr.startswith("MEAT-") or qr.startswith("AD-"):
        return "MEAT_STATION"
    if lot_type in ["FG_MIKA", "FG_VAKUM"] or qr.startswith("FG-"):
        return "FINISHING_STATION"

    if lot.current_station:
        return normalize_station_id(lot.current_station)
    return "MEAT_STATION"


def get_target_station_for_lot(lot: LotTracking, origin_station: str) -> str:
    """
    Menentukan stasiun tujuan kiriman berdasarkan tipe lot.
    - Saos -> FINISHING_STATION (Packing)
    - Kulit -> ASSEMBLY_STATION (Cetak)
    - Adonan -> ASSEMBLY_STATION (Cetak)
    - Bumbu -> MEAT_STATION (Giling/Mixer)
    - Daging -> MEAT_STATION (Mixer) / ASSEMBLY_STATION
    """
    qr = (lot.lot_qr_code or "").upper()
    lot_type = (lot.lot_type or "").upper()

    if lot_type == "SAOS" or qr.startswith("SS-") or qr.startswith("SA-"):
        return "FINISHING_STATION"
    if lot_type == "KULIT" or qr.startswith("KL-"):
        return "ASSEMBLY_STATION"
    if lot_type == "ADONAN" or qr.startswith("AD-"):
        return "ASSEMBLY_STATION"
    if lot_type == "BUMBU" or qr.startswith("BM-") or qr.startswith("SP-"):
        return "MEAT_STATION"
    if lot_type == "DAGING" or qr.startswith("DG-") or qr.startswith("MEAT-"):
        return "SEASONING_STATION"
    return STATION_METADATA.get(origin_station, {}).get("target", "ASSEMBLY_STATION")


async def get_effective_scan_mode(db: AsyncSession, station_id: str) -> str:
    """
    Membaca mode scan stasiun langsung dari database pada setiap request (tanpa cache memori).
    Prioritas: Override Stasiun -> Global -> Default ('1X').
    """
    st_norm = normalize_station_id(station_id)
    # 1. Cek override stasiun
    stmt_st = select(ScanSetting).where(ScanSetting.station_id.in_([station_id, st_norm]))
    res_st = await db.execute(stmt_st)
    for st_row in res_st.scalars().all():
        if st_row and st_row.scan_mode in ["1X", "2X"]:
            return st_row.scan_mode

    # 2. Cek default global
    stmt_gl = select(ScanSetting).where(ScanSetting.station_id == "GLOBAL")
    res_gl = await db.execute(stmt_gl)
    gl_row = res_gl.scalar_one_or_none()
    if gl_row and gl_row.scan_mode in ["1X", "2X"]:
        return gl_row.scan_mode

    return "1X"


def get_in_transit_status_name(target_station: str) -> str:
    """Format status seragam untuk interoperabilitas stasiun."""
    t = normalize_station_id(target_station)
    if t == "FINISHING_STATION":
        return "IN_TRANSIT_TO_FINISHING"
    elif t == "ASSEMBLY_STATION":
        return "IN_TRANSIT_TO_ASSEMBLY"
    elif t == "MEAT_STATION":
        return "IN_TRANSIT_TO_MEAT_MIXING"
    elif t == "SEASONING_STATION":
        return "IN_TRANSIT_TO_SEASONING"
    return "KELUAR"


async def execute_scan_transition(
    db: AsyncSession,
    lot_code: str,
    operator_station_id: str,
    user_id: str,
    user_role: str,
    intent: Optional[str] = None,  # "SCAN_IN" | "SCAN_OUT" | None
    notes: Optional[str] = None
) -> Dict[str, Any]:
    """
    Eksekutor tunggal transisi state machine scan:
    - Membaca mode scan langsung dari DB per request.
    - Idempotensi: scan pada state tujuan = info no-op tanpa duplikasi event.
    - Memvalidasi transisi sesuai mode (1x vs 2x) dan niat (Scan In vs Scan Out).
    - Menjalankan perubahan status dan pencatatan scan_events/custody_logs atomik 1 transaksi.
    """
    clean_code = (lot_code or "").strip().upper()
    if not clean_code:
        raise HTTPException(status_code=400, detail="Kode QR barcode tidak boleh kosong.")

    # 1. Ambil data lot
    stmt = select(LotTracking).where(LotTracking.lot_qr_code == clean_code)
    res = await db.execute(stmt)
    lot = res.scalar_one_or_none()
    if not lot:
        raise HTTPException(
            status_code=404,
            detail="QR tidak dikenal di sistem."
        )

    # Validasi jika lot sudah dibatalkan (VOID)
    if (lot.current_status or "").upper() == "VOID":
        raise HTTPException(
            status_code=400,
            detail="Barcode ini sudah dibatalkan (VOID) dan tidak dapat diproses lagi."
        )

    # 2. Normalisasi Stasiun & Kepemilikan Divisi
    op_st = normalize_station_id(operator_station_id)
    origin_st = normalize_station_id(get_station_for_lot(lot))
    target_st = normalize_station_id(get_target_station_for_lot(lot, origin_st))
    current_norm = normalize_status(lot.current_status)

    target_name = STATION_METADATA.get(target_st, {}).get("name", target_st)
    origin_name = STATION_METADATA.get(origin_st, {}).get("name", origin_st)

    lot_type = (lot.lot_type or "").upper().strip()
    is_meat_adonan = lot_type in ["ADONAN", "DAGING"]
    is_bumbu = lot_type == "BUMBU"

    # Validasi kepemilikan divisi dan tipe lot
    clean_intent = (intent or "").strip().upper() if intent else None

    # ========================================================
    # ATURAN KHUSUS STASIUN DAGING & MIXER (MEAT_STATION)
    # ========================================================
    if op_st == "MEAT_STATION":
        # 1. Jenis lain (kulit, saos, kemasan produk jadi, dll) ditolak untuk kedua niat
        if not (is_meat_adonan or is_bumbu):
            jenis_label = "lain"
            if lot_type == "KULIT":
                jenis_label = "Kulit"
            elif lot_type == "SAOS":
                jenis_label = "Saos"
            elif lot_type in ["FG_MIKA", "FG_VAKUM"]:
                jenis_label = "Kemasan produk jadi"
            elif lot_type:
                jenis_label = lot_type.capitalize()
            raise HTTPException(
                status_code=400,
                detail=f"QR ini bukan untuk bagian ini (jenis: {jenis_label})."
            )

        # 2. Bumbu di Stasiun Daging:
        if is_bumbu:
            # Bumbu SELALU ditolak untuk Scan Out di stasiun Daging
            if clean_intent == "SCAN_OUT":
                raise HTTPException(
                    status_code=400,
                    detail="QR bumbu tidak bisa di-scan out di bagian ini."
                )

            # Scan In Bumbu:
            if current_norm in ["DICETAK", "DI_STOK"]:
                raise HTTPException(
                    status_code=400,
                    detail="Bumbu belum dikirim dari bagian Bumbu."
                )
            elif current_norm == "DITERIMA":
                # Idempoten: bumbu sudah diterima di mixer
                mode_bumbu = await get_effective_scan_mode(db, origin_st)
                return {
                    "success": True,
                    "is_duplicate": True,
                    "lot_code": clean_code,
                    "action": "SCAN_IN",
                    "previous_status": lot.current_status,
                    "current_status": lot.current_status,
                    "normalized_status": "DITERIMA",
                    "mode": mode_bumbu,
                    "message": "Sudah diterima",
                    "semantic_variant": "info",
                    "can_undo": False
                }
            elif current_norm != "KELUAR":
                raise HTTPException(
                    status_code=400,
                    detail=f"Status bumbu '{lot.current_status}' tidak valid untuk diterima."
                )

    # Validasi stasiun umum lainnya
    else:
        if clean_intent == "SCAN_OUT":
            if op_st != origin_st and op_st != "FINISHING_STATION":
                raise HTTPException(
                    status_code=400,
                    detail="QR bukan milik divisi ini."
                )
        elif clean_intent == "SCAN_IN":
            if current_norm == "KELUAR":
                # Barang transit harus diterima di stasiun tujuan
                if op_st != target_st and not (target_st == "ASSEMBLY_STATION" and op_st == "ASSEMBLY_STATION"):
                    raise HTTPException(
                        status_code=400,
                        detail=f"QR sedang dalam transit menuju {target_name}, bukan divisi ini."
                    )
            elif current_norm in ["DICETAK", "DI_STOK"]:
                # Scan in barang stasiun asal
                if op_st != origin_st:
                    raise HTTPException(
                        status_code=400,
                        detail="QR bukan milik divisi ini."
                    )

    # 3. Mode Scan Efektif
    mode = await get_effective_scan_mode(db, origin_st)

    # 4. Idempotensi Waktu Singkat (2-3 detik debounce lapis kedua server)
    three_seconds_ago = datetime.utcnow() - timedelta(seconds=3)
    idem_actions = []
    if clean_intent == "SCAN_IN":
        idem_actions = ["SCAN_IN", "CONFIRM_STOCK"]
    elif clean_intent == "SCAN_OUT":
        idem_actions = ["SCAN_OUT"]

    stmt_idem = select(ScanEvent).where(
        ScanEvent.lot_qr_code == clean_code,
        ScanEvent.station_id == operator_station_id,
        ScanEvent.user_id == user_id,
        ScanEvent.action != "UNDO",
        ScanEvent.timestamp >= three_seconds_ago
    )
    if idem_actions:
        stmt_idem = stmt_idem.where(ScanEvent.action.in_(idem_actions))
    stmt_idem = stmt_idem.order_by(desc(ScanEvent.event_id)).limit(1)

    res_idem = await db.execute(stmt_idem)
    last_event = res_idem.scalar_one_or_none()
    if last_event:
        action_msg = "Sudah scan in" if last_event.action in ["CONFIRM_STOCK", "SCAN_IN"] else "Sudah scan out" if last_event.action == "SCAN_OUT" else f"Status '{last_event.new_status}'"
        return {
            "success": True,
            "is_duplicate": True,
            "lot_code": clean_code,
            "action": last_event.action,
            "previous_status": last_event.previous_status,
            "current_status": last_event.new_status,
            "normalized_status": normalize_status(last_event.new_status),
            "mode": last_event.mode_applied,
            "message": f"{action_msg} (scan duplikat diabaikan).",
            "semantic_variant": "info",
            "can_undo": True,
            "undo_expires_in_sec": max(1, 15 - int((datetime.utcnow() - last_event.timestamp).total_seconds()))
        }

    # 5. Evaluasi State Machine Berdasarkan Niat (Intent) & Mode
    new_status = None
    action_type = None
    feedback_message = None
    semantic_variant = "info"

    # ========================================================
    # NIAT EKSPLISIT: SCAN IN
    # ========================================================
    if clean_intent == "SCAN_IN":
        # Kasus A: Menerima Barang Masuk dari Transit
        if current_norm == "KELUAR" and op_st == target_st:
            if target_st == "ASSEMBLY_STATION":
                new_status = "IN_ASSEMBLY_STATION"
                lot.current_station = "ASSEMBLY_STATION"
            elif target_st == "MEAT_STATION":
                new_status = "IN_MEAT_MIXING_STATION"
                lot.current_station = "MEAT_STATION"
                action_type = "SCAN_IN"
                feedback_message = f"Bumbu diterima: {lot.lot_qr_code} telah diterima di Mixer."
                semantic_variant = "success"
            elif target_st == "FINISHING_STATION":
                new_status = "IN_FINISHING_STATION"
                lot.current_station = "FINISHING_STATION"
                action_type = "SCAN_IN"
                feedback_message = f"Diterima: Bahan telah diterima di {target_name}."
                semantic_variant = "success"
            else:
                new_status = "DITERIMA"
                lot.current_station = target_st
                action_type = "SCAN_IN"
                feedback_message = f"Diterima: Bahan telah diterima di {target_name}."
                semantic_variant = "success"

        # Kasus B: Barang Transit Sudah Diterima Sebelumnya (Idempoten)
        elif current_norm == "DITERIMA" and op_st == target_st:
            return {
                "success": True,
                "is_duplicate": True,
                "lot_code": clean_code,
                "action": "SCAN_IN",
                "previous_status": lot.current_status,
                "current_status": lot.current_status,
                "normalized_status": "DITERIMA",
                "mode": mode,
                "message": "Sudah diterima",
                "semantic_variant": "info",
                "can_undo": False
            }

        # Kasus C: Barang Produksi Sendiri di Stasiun Asal
        elif op_st == origin_st:
            if mode == "2X":
                if current_norm == "DICETAK":
                    # Transisi masuk stok fisik
                    new_status = f"READY_IN_{origin_st}"
                    action_type = "CONFIRM_STOCK"
                    if op_st == "MEAT_STATION":
                        feedback_message = f"Adonan scan in: {lot.lot_qr_code} terkonfirmasi masuk stok."
                    else:
                        feedback_message = f"Masuk stok: Fisik barang terkonfirmasi ada di {origin_name}."
                    semantic_variant = "info"
                elif current_norm == "DI_STOK":
                    if op_st == "MEAT_STATION" or is_meat_adonan:
                        raise HTTPException(
                            status_code=400,
                            detail="Adonan tidak bisa di-scan in, melainkan di-scan out untuk dikirim ke bagian Cetak."
                        )
                    # Idempoten: barang sudah scan in
                    return {
                        "success": True,
                        "is_duplicate": True,
                        "lot_code": clean_code,
                        "action": "CONFIRM_STOCK",
                        "previous_status": lot.current_status,
                        "current_status": lot.current_status,
                        "normalized_status": "DI_STOK",
                        "mode": mode,
                        "message": "Sudah scan in",
                        "semantic_variant": "info",
                        "can_undo": False
                    }
                elif current_norm == "KELUAR":
                    raise HTTPException(
                        status_code=400,
                        detail="Barang sudah discan out / dikirim ke divisi berikutnya."
                    )
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Barang sudah dalam status '{lot.current_status}'."
                    )
            else:
                # Mode 1x: Barang produksi sendiri tidak bisa di-scan in, melainkan langsung di-scan out
                if op_st == "MEAT_STATION" or is_meat_adonan:
                    detail_msg = "Adonan tidak bisa di-scan in, melainkan di-scan out untuk dikirim ke bagian Cetak."
                else:
                    detail_msg = f"{origin_name} tidak bisa di-scan in. Silakan gunakan tombol Scan Out untuk mengirim ke {target_name}."
                raise HTTPException(
                    status_code=400,
                    detail=detail_msg
                )
        else:
            raise HTTPException(
                status_code=400,
                detail="QR bukan milik divisi ini."
            )

    # ========================================================
    # NIAT EKSPLISIT: SCAN OUT
    # ========================================================
    elif clean_intent == "SCAN_OUT":
        if current_norm == "DICETAK":
            if mode == "2X":
                # Mode 2x: Wajib scan in terlebih dahulu!
                raise HTTPException(
                    status_code=400,
                    detail="Belum scan in. Lakukan Scan In terlebih dahulu."
                )
            else:
                # Mode 1x: Boleh langsung keluar dari Belum discan
                new_status = get_in_transit_status_name(target_st)
                action_type = "SCAN_OUT"
                if op_st == "MEAT_STATION":
                    feedback_message = f"Adonan scan out: {lot.lot_qr_code} dikirim menuju {target_name}."
                else:
                    feedback_message = f"Keluar: Barang dikirim langsung menuju {target_name} (Mode 1x)."
                semantic_variant = "warning"

        elif current_norm == "DI_STOK":
            # Baik Mode 1x maupun 2x: dari Sudah scan in -> Scan Out berhasil
            new_status = get_in_transit_status_name(target_st)
            action_type = "SCAN_OUT"
            if op_st == "MEAT_STATION":
                feedback_message = f"Adonan scan out: {lot.lot_qr_code} dikirim menuju {target_name}."
            else:
                feedback_message = f"Keluar: Barang dikirim menuju {target_name}."
            semantic_variant = "warning"

        elif current_norm == "KELUAR":
            # Idempoten: Barang sudah berstatus keluar
            return {
                "success": True,
                "is_duplicate": True,
                "lot_code": clean_code,
                "action": "SCAN_OUT",
                "previous_status": lot.current_status,
                "current_status": lot.current_status,
                "normalized_status": "KELUAR",
                "mode": mode,
                "message": "Sudah scan out",
                "semantic_variant": "info",
                "can_undo": False
            }

        elif current_norm in ["DITERIMA", "CONSUMED", "CLOSED", "VOID"]:
            raise HTTPException(
                status_code=400,
                detail=f"Barang sudah diproses atau selesai ({lot.current_status})."
            )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Status '{lot.current_status}' tidak valid untuk Scan Out."
            )

    # ========================================================
    # FALLBACK / BACKWARD COMPATIBILITY (Jika intent tidak dikirim)
    # ========================================================
    else:
        if current_norm == "DICETAK":
            if mode == "1X":
                new_status = get_in_transit_status_name(target_st)
                action_type = "SCAN_OUT"
                feedback_message = f"Keluar: Barang dikirim langsung menuju {target_name} (Mode 1x)."
                semantic_variant = "warning"
            else:
                new_status = f"READY_IN_{origin_st}"
                action_type = "CONFIRM_STOCK"
                feedback_message = f"Masuk stok: Fisik barang terkonfirmasi ada di {origin_name} (Mode 2x)."
                semantic_variant = "info"

        elif current_norm == "DI_STOK":
            new_status = get_in_transit_status_name(target_st)
            action_type = "SCAN_OUT"
            feedback_message = f"Keluar: Barang dikirim menuju {target_name}."
            semantic_variant = "warning"

        elif current_norm == "KELUAR":
            is_receiving = (
                op_st == target_st or
                (target_st == "FINISHING_STATION" and op_st in ["FINISHING_STATION", "PROD_2"]) or
                (target_st == "ASSEMBLY_STATION" and op_st in ["ASSEMBLY_STATION", "PROD_2"]) or
                (target_st == "MEAT_STATION" and op_st in ["MEAT_STATION", "GILING"])
            )
            if is_receiving:
                if target_st == "FINISHING_STATION":
                    new_status = "IN_FINISHING_STATION"
                    lot.current_station = "FINISHING_STATION"
                elif target_st == "ASSEMBLY_STATION":
                    new_status = "IN_ASSEMBLY_STATION"
                    lot.current_station = "ASSEMBLY_STATION"
                elif target_st == "MEAT_STATION":
                    new_status = "IN_MEAT_MIXING_STATION"
                    lot.current_station = "MEAT_STATION"
                elif target_st == "SEASONING_STATION":
                    new_status = "IN_SEASONING_STATION"
                    lot.current_station = "SEASONING_STATION"
                else:
                    new_status = "DITERIMA"
                    lot.current_station = target_st

                action_type = "SCAN_IN"
                feedback_message = f"Diterima: Bahan telah diterima di {target_name}."
                semantic_variant = "success"
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"Ditolak: Barang sudah berstatus KELUAR menuju {target_name}. Menunggu scan terima di stasiun tujuan."
                )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Ditolak: Status barang '{lot.current_status}' tidak valid untuk scan ini."
            )

    # 6. Eksekusi Atomic Transaction (Status + ScanEvent + CustodyTransferLog)
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
        target_station=target_st if action_type == "SCAN_OUT" else None,
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

    stmt_ev = select(ScanEvent).where(
        ScanEvent.lot_qr_code == clean_code,
        ScanEvent.action != "UNDO"
    ).order_by(desc(ScanEvent.event_id)).limit(1)

    res_ev = await db.execute(stmt_ev)
    last_event = res_ev.scalar_one_or_none()

    if not last_event:
        raise HTTPException(status_code=400, detail="Tidak ada riwayat scan untuk dibatalkan.")

    if last_event.user_id != user_id:
        raise HTTPException(
            status_code=403,
            detail=f"Undo ditolak: Hanya operator yang melakukan scan ({last_event.user_id}) yang dapat membatalkannya."
        )

    now = datetime.utcnow()
    elapsed = (now - last_event.timestamp).total_seconds()
    if elapsed > 15.0:
        raise HTTPException(
            status_code=400,
            detail=f"Batas waktu pembatalan (15 detik) telah habis ({int(elapsed)} detik yang lalu)."
        )

    if lot.current_status != last_event.new_status:
        raise HTTPException(
            status_code=400,
            detail="Barang telah berpindah status lebih lanjut, scan tidak dapat dibatalkan lagi."
        )

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
    st_norm = normalize_station_id(station_id)

    stmt = select(LotTracking).where(
        LotTracking.current_station.in_([station_id, st_norm]),
        LotTracking.current_status.notin_(["CONSUMED", "CLOSED", "VOID"])
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
        active_stock = printed_count + stock_count
        pending_confirmation = 0
    else:
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
