from typing import List, Dict, Any, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from ..database import get_db
from ..auth import get_current_user, is_global_supervisor
from ..models import LotTracking, ScanSetting, ScanEvent
from ..schemas import (
    ScanTransitionRequest,
    ScanBatchTransitionRequest,
    ScanUndoRequest,
    UpdateScanModeRequest,
    ScanModeSettingResponse,
)
from ..services.scan_engine import (
    execute_scan_transition,
    execute_undo_scan,
    get_station_stock_counts,
    get_effective_scan_mode,
    STATION_METADATA,
    normalize_status,
)

router = APIRouter(tags=["Scan Mode & Transitions"])


def verify_pic_or_admin(user: Dict[str, Any]):
    """
    Hanya PIC, Supervisor, atau Admin yang diizinkan mengubah mode scan operasional.
    """
    role = (user.get("role") or "").upper()
    op_id = (user.get("operator_id") or "").upper()
    username = (user.get("username") or user.get("sub") or "").lower()

    is_allowed = (
        is_global_supervisor(user)
        or role in ["STAKEHOLDER", "SUPERVISOR", "ADMIN", "MANAGER"]
        or "PIC" in op_id
        or "SUPERVISOR" in op_id
        or username.startswith("pic_")
    )
    if not is_allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akses ditolak: Hanya PIC atau Supervisor yang berhak mengubah konfigurasi Mode Scan."
        )


@router.get("/settings/scan-mode", response_model=ScanModeSettingResponse)
async def get_scan_mode_settings(
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Mendapatkan konfigurasi mode scan global, override stasiun, dan jumlah lot DICETAK yang terdampak.
    """
    # 1. Baca semua row scan_settings
    stmt = select(ScanSetting)
    res = await db.execute(stmt)
    all_settings = res.scalars().all()

    global_mode = "1X"
    station_modes: Dict[str, str] = {}
    last_updated_by = None
    last_updated_at = None

    for s in all_settings:
        if s.station_id == "GLOBAL":
            global_mode = s.scan_mode
            last_updated_by = s.updated_by
            last_updated_at = s.updated_at
        else:
            station_modes[s.station_id] = s.scan_mode

    # 2. Susun metadata stasiun dengan effective mode
    stations_list = []
    for s_id, s_meta in STATION_METADATA.items():
        st_mode = station_modes.get(s_id, "INHERIT")
        effective_mode = global_mode if st_mode == "INHERIT" else st_mode
        stations_list.append({
            "id": s_id,
            "name": s_meta["name"],
            "configured_mode": st_mode,
            "effective_mode": effective_mode
        })

    # 3. Hitung jumlah lot yang saat ini berstatus DICETAK (akan menunggu konfirmasi jika di mode 2x)
    stmt_lots = select(LotTracking).where(
        LotTracking.current_status.in_(["PRINTED_PENDING_STOCK_IN", "DICETAK"])
    )
    res_lots = await db.execute(stmt_lots)
    affected_count = len(res_lots.scalars().all())

    return ScanModeSettingResponse(
        global_mode=global_mode,
        station_modes=station_modes,
        stations=stations_list,
        affected_printed_lots_count=affected_count,
        updated_by=last_updated_by,
        updated_at=last_updated_at
    )


@router.post("/settings/scan-mode", response_model=ScanModeSettingResponse)
async def update_scan_mode_settings(
    payload: UpdateScanModeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Mengubah konfigurasi mode scan global atau stasiun tertentu.
    Hanya untuk PIC / Supervisor.
    """
    verify_pic_or_admin(current_user)

    target_global = payload.global_mode.upper()
    if target_global not in ["1X", "2X"]:
        raise HTTPException(status_code=400, detail="Mode global harus '1X' atau '2X'.")

    operator_tag = current_user.get("operator_id") or current_user.get("username") or "SUPERVISOR"
    now = datetime.utcnow()

    # 1. Update/Insert GLOBAL
    stmt_gl = select(ScanSetting).where(ScanSetting.station_id == "GLOBAL")
    res_gl = await db.execute(stmt_gl)
    gl_row = res_gl.scalar_one_or_none()
    if gl_row:
        gl_row.scan_mode = target_global
        gl_row.updated_by = operator_tag
        gl_row.updated_at = now
    else:
        gl_row = ScanSetting(
            station_id="GLOBAL",
            scan_mode=target_global,
            updated_by=operator_tag,
            updated_at=now
        )
        db.add(gl_row)

    # 2. Update/Insert station overrides
    for s_id, mode_val in payload.station_modes.items():
        clean_mode = (mode_val or "").upper()
        if clean_mode not in ["1X", "2X", "INHERIT"]:
            continue
        stmt_st = select(ScanSetting).where(ScanSetting.station_id == s_id)
        res_st = await db.execute(stmt_st)
        st_row = res_st.scalar_one_or_none()
        if st_row:
            st_row.scan_mode = clean_mode
            st_row.updated_by = operator_tag
            st_row.updated_at = now
        else:
            st_row = ScanSetting(
                station_id=s_id,
                scan_mode=clean_mode,
                updated_by=operator_tag,
                updated_at=now
            )
            db.add(st_row)

    await db.commit()

    # Return refreshed settings
    return await get_scan_mode_settings(db=db, current_user=current_user)


@router.post("/scan/process")
async def process_scan_endpoint(
    payload: ScanTransitionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Endpoint scan tunggal:
    - Membaca mode stasiun langsung dari DB per request
    - Idempotensi 3 detik
    - Transisi atomic DICETAK -> DI_STOK -> KELUAR -> DITERIMA
    """
    user_id = current_user.get("operator_id") or current_user.get("username") or "OPERATOR"
    user_role = current_user.get("role", "OPERATOR")

    result = await execute_scan_transition(
        db=db,
        lot_code=payload.lot_code,
        operator_station_id=payload.station_id,
        user_id=user_id,
        user_role=user_role,
        intent=payload.intent,
        notes=payload.notes
    )
    return result


@router.post("/scan/process-batch")
async def process_batch_scan_endpoint(
    payload: ScanBatchTransitionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Eksekusi batch scan in atau scan out untuk beberapa kode QR sekaligus dalam 1 transaksi DB.
    """
    user_id = current_user.get("operator_id") or current_user.get("username") or "OPERATOR"
    user_role = current_user.get("role", "OPERATOR")

    results = []
    for code in payload.lot_codes:
        res = await execute_scan_transition(
            db=db,
            lot_code=code,
            operator_station_id=payload.station_id,
            user_id=user_id,
            user_role=user_role,
            intent=payload.intent,
            notes=payload.notes
        )
        results.append(res)
    return results


@router.post("/scan/undo")
async def undo_scan_endpoint(
    payload: ScanUndoRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Endpoint undo scan terakhir:
    - Maksimal 15 detik setelah scan
    - Hanya oleh operator yang sama
    """
    user_id = current_user.get("operator_id") or current_user.get("username") or "OPERATOR"

    result = await execute_undo_scan(
        db=db,
        lot_code=payload.lot_code,
        user_id=user_id
    )
    return result


@router.get("/scan/stock-counts/{station_id}")
async def get_station_stock_endpoint(
    station_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Menghitung jumlah stok aktif dan barang menunggu konfirmasi sesuai mode stasiun.
    """
    return await get_station_stock_counts(db=db, station_id=station_id)
