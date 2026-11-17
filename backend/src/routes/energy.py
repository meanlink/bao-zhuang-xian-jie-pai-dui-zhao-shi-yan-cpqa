"""电量对账封账路由。读取对所有登录角色开放；写入限 admin/analyst；封账/批准限 admin。"""
import os
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select

from src.config.database import get_db
from src.config.settings import ARTIFACT_DIR
from src.models import (EnergyPeriod, EnergyCloseBook, EnergyEntry, EnergyAnomaly,
                       EnergyRevision, EnergyCorrection)
from src.schemas.energy import (PeriodInput, IngestInput, WatermarkInput, RevisionInput,
                                CorrectionInput, EnergyApproveInput)
from src.middlewares.auth import current_user, can_write, admin_only
from src.services import energy
from src.services.audit import record
from src.utils.serialization import (energy_period_dict, energy_book_dict, energy_revision_dict,
                                     energy_anomaly_dict, energy_entry_dict)

router = APIRouter(prefix="/energy")


# ---------------- 账期 ----------------
@router.get("/periods")
def list_periods(db=Depends(get_db), u=Depends(current_user)):
    ps = db.scalars(select(EnergyPeriod).order_by(EnergyPeriod.id.desc())).all()
    return [energy_period_dict(p) for p in ps]


@router.post("/periods", status_code=201)
def create_period(data: PeriodInput, db=Depends(get_db), u=Depends(can_write)):
    p = energy.create_period(db, data)
    record(db, u.email, "energy.period.create", {"id": p.id, "key": p.period_key})
    db.commit()
    return energy_period_dict(p)


@router.get("/periods/{pid}")
def period_detail(pid: int, db=Depends(get_db), u=Depends(current_user)):
    p = db.get(EnergyPeriod, pid)
    if not p:
        raise HTTPException(404, "电量账期不存在")
    return energy_period_dict(p)


# ---------------- 接入与水位 ----------------
@router.post("/periods/{pid}/ingest")
def ingest(pid: int, data: IngestInput, db=Depends(get_db), u=Depends(can_write)):
    out = energy.ingest(db, pid, data, u)
    record(db, u.email, "energy.ingest", {"period_id": pid, "stream": data.stream, **out})
    db.commit()
    return out


@router.post("/periods/{pid}/watermark")
def watermark(pid: int, data: WatermarkInput, db=Depends(get_db), u=Depends(can_write)):
    view = energy.confirm_watermark(db, pid, data, u)
    record(db, u.email, "energy.watermark", view)
    db.commit()
    return view


@router.get("/periods/{pid}/preview")
def preview(pid: int, db=Depends(get_db), u=Depends(current_user)):
    return energy.preview(db, pid)


@router.post("/periods/{pid}/close")
def close(pid: int, db=Depends(get_db), u=Depends(admin_only)):
    book, meta = energy.close_period(db, pid, u)
    record(db, u.email, "energy.close", {"period_id": pid, "book_id": book.id, **meta})
    db.commit()
    return {**energy_book_dict(book), **meta}


# ---------------- 原账 / 明细 / 异常 ----------------
@router.get("/periods/{pid}/book")
def get_book(pid: int, db=Depends(get_db), u=Depends(current_user)):
    b = db.scalar(select(EnergyCloseBook).where(EnergyCloseBook.period_id == pid))
    if not b:
        raise HTTPException(404, "该账期尚无封账原账")
    return energy_book_dict(b)


@router.get("/periods/{pid}/entries")
def get_entries(pid: int, db=Depends(get_db), u=Depends(current_user)):
    rows = db.scalars(select(EnergyEntry).where(EnergyEntry.period_id == pid).order_by(EnergyEntry.id)).all()
    return [energy_entry_dict(e) for e in rows]


@router.get("/periods/{pid}/anomalies")
def get_anomalies(pid: int, db=Depends(get_db), u=Depends(current_user)):
    rows = db.scalars(select(EnergyAnomaly).where(EnergyAnomaly.period_id == pid).order_by(EnergyAnomaly.id.desc())).all()
    return [energy_anomaly_dict(a) for a in rows]


# ---------------- 修订账 ----------------
@router.get("/periods/{pid}/revisions")
def list_revisions(pid: int, db=Depends(get_db), u=Depends(current_user)):
    rows = db.scalars(select(EnergyRevision).where(EnergyRevision.period_id == pid).order_by(EnergyRevision.id)).all()
    return [energy_revision_dict(r) for r in rows]


@router.post("/periods/{pid}/revisions", status_code=201)
def open_revision(pid: int, data: RevisionInput, db=Depends(get_db), u=Depends(can_write)):
    r = energy.open_revision(db, pid, data, u)
    record(db, u.email, "energy.revision.open", {"id": r.id, "period_id": pid})
    db.commit()
    return energy_revision_dict(r)


@router.post("/revisions/{rid}/corrections")
def add_correction(rid: int, data: CorrectionInput, db=Depends(get_db), u=Depends(can_write)):
    out = energy.add_correction(db, rid, data, u)
    record(db, u.email, "energy.revision.correction", out)
    db.commit()
    return out


@router.post("/revisions/{rid}/process")
def process_revision(rid: int, db=Depends(get_db), u=Depends(can_write)):
    r, meta = energy.process_revision(db, rid, u)
    record(db, u.email, "energy.revision.process", {"id": rid, "status": r.status, **meta})
    db.commit()
    return {**energy_revision_dict(r), **meta}


@router.get("/revisions/{rid}/corrections")
def list_corrections(rid: int, db=Depends(get_db), u=Depends(current_user)):
    rows = db.scalars(select(EnergyCorrection).where(EnergyCorrection.revision_id == rid).order_by(EnergyCorrection.id)).all()
    return [{"id": c.id, "stream": c.stream, "idempotency_key": c.idempotency_key,
             "processed": c.processed, "payload": c.payload, "created_at": c.created_at} for c in rows]


# ---------------- 固定简报下载 ----------------
@router.get("/briefing/{kind}/{ref_id}")
def download_briefing(kind: str, ref_id: int, db=Depends(get_db), u=Depends(current_user)):
    if kind not in ("book", "revision"):
        raise HTTPException(404, "简报类型不存在")
    if kind == "book":
        b = db.get(EnergyCloseBook, ref_id)
        filename = b.briefing_filename if b else None
    else:
        r = db.get(EnergyRevision, ref_id)
        filename = r.briefing_filename if r else None
    if not filename:
        raise HTTPException(404, "简报尚未生成或账未完成")
    path = os.path.join(ARTIFACT_DIR, "energy", filename)
    if not os.path.realpath(path).startswith(os.path.realpath(os.path.join(ARTIFACT_DIR, "energy"))):
        raise HTTPException(400, "非法简报路径")
    if not os.path.exists(path):
        raise HTTPException(404, "简报文件缺失")
    return FileResponse(path, filename=filename, media_type="application/json")


# ---------------- 关联分析：封账后批准 ----------------
@router.post("/approve-analysis")
def approve_analysis(data: EnergyApproveInput, db=Depends(get_db), u=Depends(admin_only)):
    j = energy.approve_with_energy(db, data.period_id, data.job_id, u)
    record(db, u.email, "energy.analysis.approve", {"job_id": j.id, "period_id": data.period_id})
    db.commit()
    from src.utils.serialization import job_dict
    return job_dict(j)
