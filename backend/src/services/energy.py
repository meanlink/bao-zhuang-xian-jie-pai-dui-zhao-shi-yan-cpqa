"""电量对账服务：双水位封账、并发不漏计、修订账与固定简报。

并发与幂等约定：
- 行级锁锁定账期/修订，配合唯一约束保证封账与补传并发不漏计、重试不重复。
- 表计读数、片段、更正均带 idempotency_key；重复提交按已存记录幂等返回。
- 封账后原账（EnergyCloseBook）与简报文件冻结；迟到更正只进 EnergyRevision。
"""
from __future__ import annotations

import csv
import io
import json
import os
import time

from fastapi import HTTPException
from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError

from src.config.settings import ARTIFACT_DIR
from src.models import (EnergyPeriod, EnergyMeterReading, EnergySegment, EnergyCloseBook,
                       EnergyEntry, EnergyAnomaly, EnergyRevision, EnergyCorrection,
                       EnergyIdempotency, Job)
from src.services import energy_engine as eng
from src.utils.serialization import checksum

ENERGY_DIR = os.path.join(ARTIFACT_DIR, "energy")


# ----------------------------- 账期 -----------------------------
def create_period(db, data):
    if data.end_ts <= data.start_ts:
        raise HTTPException(422, "账期结束时间必须晚于开始时间")
    if db.scalar(select(EnergyPeriod).where(EnergyPeriod.period_key == data.period_key)):
        raise HTTPException(409, "账期编号已存在")
    p = EnergyPeriod(period_key=data.period_key, name=data.name, start_ts=data.start_ts,
                     end_ts=data.end_ts, max_gap_seconds=data.max_gap_seconds,
                     status="open", snapshot={}, created_at=time.time())
    db.add(p)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "账期编号已存在")
    return p


def _require_open_period(db, period_id, lock=False):
    p = db.get(EnergyPeriod, period_id, with_for_update=lock) if lock else db.get(EnergyPeriod, period_id)
    if not p:
        raise HTTPException(404, "电量账期不存在")
    return p


# ----------------------------- 接入（乱序、幂等） -----------------------------
def _idem_seen(db, scope, key):
    return db.scalar(select(EnergyIdempotency).where(
        EnergyIdempotency.scope == scope, EnergyIdempotency.idempotency_key == key))


def ingest(db, period_id, data, user):
    p = _require_open_period(db, period_id, lock=True)
    if p.status != "open":
        raise HTTPException(409, "账期已封账，迟到数据请以更正形式进入修订账，不再直接补传")
    scope = f"ingest:{data.stream}:{period_id}"
    accepted, skipped = 0, 0
    now = time.time()
    for raw in data.items:
        key = str(raw.get("idempotency_key", "")).strip()
        if not key:
            raise HTTPException(422, "每条数据必须带 idempotency_key")
        full_key = f"{scope}:{key}"
        if _idem_seen(db, "ingest", full_key):
            skipped += 1
            continue
        if data.stream == "meter":
            try:
                item = eng_view_meter(raw)
            except ValueError as e:
                raise HTTPException(422, f"表计读数非法：{e}")
            _ensure_in_period(p, item["reading_ts"], "读数时间")
            row = EnergyMeterReading(period_id=p.id, meter_serial=item["meter_serial"],
                                     reading_ts=item["reading_ts"], reading_value=item["reading_value"],
                                     idempotency_key=full_key, source_ref=item.get("source_ref"), created_at=now)
        else:
            try:
                item = eng_view_segment(raw)
            except ValueError as e:
                raise HTTPException(422, f"片段数据非法：{e}")
            _ensure_in_period(p, item["start_ts"], "片段开始")
            _ensure_in_period(p, item["end_ts"], "片段结束")
            row = EnergySegment(period_id=p.id, segment_ref=item["segment_ref"], label=item["label"],
                                start_ts=item["start_ts"], end_ts=item["end_ts"],
                                idempotency_key=full_key, source_ref=item.get("source_ref"), created_at=now)
        db.add(row)
        db.add(EnergyIdempotency(scope="ingest", idempotency_key=full_key, ref_id=None, created_at=now))
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            skipped += 1
            continue
        accepted += 1
    return {"accepted": accepted, "skipped_duplicate": skipped, "period_id": p.id, "status": p.status}


def eng_view_meter(raw):
    try:
        meter = str(raw["meter_serial"]).strip()
        ts = float(raw["reading_ts"])
        val = float(raw["reading_value"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("缺少 meter_serial/reading_ts/reading_value 或非数值")
    if not meter or val < 0 or not _finite(ts, val):
        raise ValueError("表号为空、读数为负或数值非法")
    return {"meter_serial": meter, "reading_ts": ts, "reading_value": val,
            "source_ref": raw.get("source_ref")}


def eng_view_segment(raw):
    try:
        ref = str(raw["segment_ref"]).strip()
        label = str(raw.get("label") or ref).strip()
        st, et = float(raw["start_ts"]), float(raw["end_ts"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("缺少 segment_ref/start_ts/end_ts 或非数值")
    if not ref or et <= st or not _finite(st, et):
        raise ValueError("片段号为空、起止非法或时长为零/负")
    return {"segment_ref": ref, "label": label, "start_ts": st, "end_ts": et,
            "source_ref": raw.get("source_ref")}


def _finite(*vals):
    return all(v == v and v not in (float("inf"), float("-inf")) for v in vals)


def _ensure_in_period(p, ts, label):
    if not (p.start_ts <= ts <= p.end_ts):
        raise HTTPException(422, f"{label} {ts} 超出账期 [{p.start_ts}, {p.end_ts}]")


# ----------------------------- 水位 -----------------------------
def confirm_watermark(db, period_id, data, user):
    p = _require_open_period(db, period_id, lock=True)
    if p.status != "open":
        raise HTTPException(409, "账期已封账，水位不可再改")
    if data.watermark_ts < p.start_ts or data.watermark_ts > p.end_ts:
        raise HTTPException(422, "确认水位必须落在账期时间范围内")
    if data.stream == "meter":
        p.meter_watermark_ts = data.watermark_ts
    else:
        p.segment_watermark_ts = data.watermark_ts
    db.flush()
    return _watermark_view(p)


def _watermark_view(p):
    return {"period_id": p.id, "meter_watermark_ts": p.meter_watermark_ts,
            "segment_watermark_ts": p.segment_watermark_ts, "status": p.status}


# ----------------------------- 试算 -----------------------------
def _collect(db, p):
    readings = [{"id": r.id, "meter_serial": r.meter_serial, "ts": r.reading_ts, "value": r.reading_value}
                for r in db.scalars(select(EnergyMeterReading).where(EnergyMeterReading.period_id == p.id))]
    segments = [{"id": s.segment_ref, "label": s.label, "start_ts": s.start_ts, "end_ts": s.end_ts}
                for s in db.scalars(select(EnergySegment).where(EnergySegment.period_id == p.id))]
    return readings, segments


def preview(db, period_id):
    p = _require_open_period(db, period_id)
    readings, segments = _collect(db, p)
    result = eng.allocate(readings, segments, p.max_gap_seconds)
    result["watermarks"] = _watermark_view(p)
    return result


# ----------------------------- 封账 -----------------------------
def close_period(db, period_id, user):
    p = _require_open_period(db, period_id, lock=True)
    existing = db.scalar(select(EnergyCloseBook).where(EnergyCloseBook.period_id == p.id))
    if existing:  # 封账幂等：重复/并发结案返回同一本原账
        return existing, {"idempotent": True}
    # 双水位：两路都确认且覆盖到账期末，任一路未齐拒绝结案
    if p.meter_watermark_ts is None or p.segment_watermark_ts is None:
        raise HTTPException(409, "两路确认水位未齐，拒绝结案")
    if p.meter_watermark_ts < p.end_ts or p.segment_watermark_ts < p.end_ts:
        raise HTTPException(409, "确认水位尚未覆盖账期末，拒绝结案")

    readings, segments = _collect(db, p)
    if len(readings) < 2:
        raise HTTPException(409, "表计读数不足，无法计算表计增量，拒绝结案")
    result = eng.allocate(readings, segments, p.max_gap_seconds)
    if result["has_anomaly"]:
        # 隔离待核对：记录异常、账期置 blocked，不出原账
        _persist_anomalies(db, p.id, None, None, result["anomalies"])
        p.status = "blocked"
        db.commit()
        raise HTTPException(409, "存在换表/缺口/交叠等隔离异常，已挂起待核对，拒绝封账")
    if not result["identity_ok"]:
        raise HTTPException(500, "分摊恒等式校验失败，拒绝封账")

    now = time.time()
    snapshot = _snapshot_payload(p, result)
    book = EnergyCloseBook(period_id=p.id, status="closed", totals=result["totals"],
                           identity_ok=True, meter_watermark_ts=p.meter_watermark_ts,
                           segment_watermark_ts=p.segment_watermark_ts,
                           checksum=checksum(snapshot), closed_at=now, closed_by=user.id)
    db.add(book)
    db.flush()
    _persist_entries(db, p.id, book.id, None, result)
    filename = _write_briefing("book", book.id, snapshot, book.checksum)
    book.briefing_filename = filename
    p.status = "closed"
    p.closed_at = now
    p.closed_by = user.id
    p.snapshot = snapshot
    db.commit()
    return book, {"idempotent": False}


def _persist_anomalies(db, period_id, book_id, revision_id, anomalies):
    now = time.time()
    for a in anomalies:
        db.add(EnergyAnomaly(period_id=period_id, book_id=book_id, revision_id=revision_id,
                             kind=a["kind"], scope=a["scope"], message=a["message"],
                             ref=a.get("ref", {}), resolved=False, created_at=now))


def _persist_entries(db, period_id, book_id, revision_id, result):
    for e in result["entries"]:
        db.add(EnergyEntry(period_id=period_id, book_id=book_id, revision_id=revision_id,
                           segment_ref=e["segment_id"], meter_serial=e["meter_serial"],
                           start_ts=e["start_ts"], end_ts=e["end_ts"],
                           overlap_seconds=e["overlap_seconds"], amount=e["amount"],
                           amount_exact=e["amount_exact"], is_unassigned=False))
    for u in result["unassigned"]:
        db.add(EnergyEntry(period_id=period_id, book_id=book_id, revision_id=revision_id,
                           segment_ref=None, meter_serial=u["meter_serial"],
                           start_ts=u["start_ts"], end_ts=u["end_ts"], overlap_seconds=0.0,
                           amount=u["amount"], amount_exact=str(eng.q(eng.D(u["amount"]))),
                           is_unassigned=True, unassigned_reason=u["reason"]))


def _snapshot_payload(p, result):
    return {"period": {"id": p.id, "period_key": p.period_key, "name": p.name,
                       "start_ts": p.start_ts, "end_ts": p.end_ts},
            "watermarks": {"meter": p.meter_watermark_ts, "segment": p.segment_watermark_ts},
            "totals": result["totals"], "segment_totals": result["segment_totals"],
            "entries": result["entries"], "unassigned": result["unassigned"],
            "intervals": result["intervals"], "identity_ok": result["identity_ok"],
            "closed_at": time.time()}


def _write_briefing(kind, ref_id, snapshot, digest):
    os.makedirs(ENERGY_DIR, exist_ok=True)
    base = f"{kind}-{ref_id}"
    json_name = f"briefing-{base}.json"
    with open(os.path.join(ENERGY_DIR, json_name), "w", encoding="utf-8") as f:
        json.dump({"checksum": digest, "snapshot": snapshot}, f, ensure_ascii=False,
                  allow_nan=False, sort_keys=True)
    csv_name = f"briefing-{base}.csv"
    out = io.StringIO()
    out.write("﻿")
    w = csv.writer(out)
    w.writerow(["片段", "电表", "区间起", "区间止", "重叠秒", "耗电", "未归属原因"])
    for e in snapshot.get("entries", []):
        w.writerow([e["segment_label"], e["meter_serial"], e["start_ts"], e["end_ts"],
                    e["overlap_seconds"], e["amount"], ""])
    for u in snapshot.get("unassigned", []):
        w.writerow(["【未归属】", u["meter_serial"], u["start_ts"], u["end_ts"], "",
                    u["amount"], u["reason"]])
    with open(os.path.join(ENERGY_DIR, csv_name), "w", encoding="utf-8") as f:
        f.write(out.getvalue())
    return json_name


# ----------------------------- 修订账 -----------------------------
def open_revision(db, period_id, data, user):
    p = _require_open_period(db, period_id, lock=True)
    book = db.scalar(select(EnergyCloseBook).where(EnergyCloseBook.period_id == p.id))
    if not book:
        raise HTTPException(409, "账期尚未封账，无需修订；请直接补传后封账")
    if db.scalar(select(EnergyRevision).where(EnergyRevision.idempotency_key == data.idempotency_key)):
        raise HTTPException(409, "修订请求幂等键已存在，请勿重复创建")
    rev = EnergyRevision(period_id=p.id, book_id=book.id, reason=data.reason,
                         status="in_progress", cursor={}, totals={}, diff={},
                         idempotency_key=data.idempotency_key, created_by=user.id, created_at=time.time())
    db.add(rev)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "修订请求幂等键已存在，请勿重复创建")
    return rev


def add_correction(db, revision_id, data, user):
    rev = db.get(EnergyRevision, revision_id, with_for_update=True)
    if not rev:
        raise HTTPException(404, "修订账不存在")
    if rev.status == "completed":
        raise HTTPException(409, "修订已完成，不可再追加更正")
    if db.scalar(select(EnergyCorrection).where(
            EnergyCorrection.revision_id == rev.id,
            EnergyCorrection.idempotency_key == data.idempotency_key)):
        return {"revision_id": rev.id, "idempotency_key": data.idempotency_key, "duplicate": True}
    # 校验负载，但不改原账
    if data.stream == "meter":
        eng_view_meter(data.payload)
    else:
        eng_view_segment(data.payload)
    c = EnergyCorrection(revision_id=rev.id, stream=data.stream,
                         idempotency_key=data.idempotency_key, payload=data.payload,
                         processed=False, created_at=time.time())
    db.add(c)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return {"revision_id": rev.id, "idempotency_key": data.idempotency_key, "duplicate": True}
    return {"revision_id": rev.id, "idempotency_key": data.idempotency_key, "duplicate": False}


def process_revision(db, revision_id, user):
    rev = db.get(EnergyRevision, revision_id, with_for_update=True)
    if not rev:
        raise HTTPException(404, "修订账不存在")
    if rev.status == "completed":  # 重试不重复记耗电
        return rev, {"idempotent": True}
    p = db.get(EnergyPeriod, rev.period_id, with_for_update=True)

    # 1) 从断点游标恢复：只处理未记位置的更正
    pending = list(db.scalars(select(EnergyCorrection).where(
        EnergyCorrection.revision_id == rev.id, EnergyCorrection.processed == False).order_by(EnergyCorrection.id)))
    cursor = dict(rev.cursor or {})
    done = cursor.get("processed_ids", [])
    try:
        for c in pending:
            if c.stream == "meter":
                eng_view_meter(c.payload)
            else:
                eng_view_segment(c.payload)
            done.append(c.id)
            c.processed = True
            cursor = {"last_correction_id": c.id, "processed_count": len(done), "processed_ids": done}
            rev.cursor = cursor
            db.flush()  # 中断也能保留已记位置
    except ValueError as e:
        rev.status = "failed"
        rev.error = f"更正数据非法，修订中断，可修复后从断点恢复：{e}"
        db.commit()
        raise HTTPException(422, rev.error)

    # 2) 合并原账数据与全部更正（更正按自然键确定性覆盖/新增）
    base_readings, base_segments = _collect(db, p)
    meter_map = {(r["meter_serial"], r["ts"]): r for r in base_readings}
    seg_map = {s["id"]: s for s in base_segments}
    corrections = list(db.scalars(select(EnergyCorrection).where(EnergyCorrection.revision_id == rev.id)))
    for c in corrections:
        if c.stream == "meter":
            m = eng_view_meter(c.payload)
            meter_map[(m["meter_serial"], m["reading_ts"])] = {
                "id": f"corr-{c.id}", "meter_serial": m["meter_serial"],
                "ts": m["reading_ts"], "value": m["reading_value"]}
        else:
            s = eng_view_segment(c.payload)
            seg_map[s["segment_ref"]] = {"id": s["segment_ref"], "label": s["label"],
                                         "start_ts": s["start_ts"], "end_ts": s["end_ts"]}
    result = eng.allocate(list(meter_map.values()), list(seg_map.values()), p.max_gap_seconds)
    if result["has_anomaly"]:
        _persist_anomalies(db, p.id, None, rev.id, result["anomalies"])
        rev.status = "failed"
        rev.error = "合并更正后仍存在隔离异常，修订未生效，原账保持不变"
        db.commit()
        raise HTTPException(409, rev.error)
    if not result["identity_ok"]:
        rev.status = "failed"
        rev.error = "修订分摊恒等式校验失败"
        db.commit()
        raise HTTPException(500, rev.error)

    # 3) 重建修订明细（幂等：先清后写），原账与原简报保留
    db.execute(delete(EnergyEntry).where(EnergyEntry.revision_id == rev.id))
    db.execute(delete(EnergyAnomaly).where(EnergyAnomaly.revision_id == rev.id))
    _persist_entries(db, p.id, None, rev.id, result)

    original_entries = [{"segment_id": e.segment_ref, "amount": e.amount,
                         "amount_exact": e.amount_exact}
                        for e in db.scalars(select(EnergyEntry).where(EnergyEntry.book_id == rev.book_id))]
    diff = eng.revision_delta(original_entries, result["entries"])
    snapshot = _snapshot_payload(p, result)
    snapshot["revision"] = {"id": rev.id, "reason": rev.reason, "diff": diff}
    digest = checksum(snapshot)
    rev.totals = result["totals"]
    rev.diff = diff
    rev.checksum = digest
    rev.briefing_filename = _write_briefing("revision", rev.id, snapshot, digest)
    rev.status = "completed"
    rev.completed_at = time.time()
    rev.error = None
    p.status = "revised"
    db.commit()
    return rev, {"idempotent": False, "diff": diff}


# ----------------------------- 关联分析批准（封账后才放行） -----------------------------
def approve_with_energy(db, period_id, job_id, user):
    p = _require_open_period(db, period_id)
    if p.status not in ("closed", "revised"):
        raise HTTPException(409, "关联分析须在电量账期封账后才能批准")
    j = db.get(Job, job_id)
    if not j:
        raise HTTPException(404, "分析任务不存在")
    if j.status != "completed":
        raise HTTPException(409, "仅已完成结果可以批准")
    j.approved = True
    db.commit()
    return j
