"""电量分摊与对账纯函数引擎（仅标准库，Decimal 精确计算）。

口径：
- 电表累计读数按 (meter_serial, ts) 排序，相邻同表读数构成一个计量区间。
- 区间耗电 = 后读数 - 前读数；换表回零、负差、同时间戳、缺口均隔离待核对。
- 区间耗电按与片段 [start_ts, end_ts] 的重叠时长比例分摊到片段；
  未被任何片段覆盖的耗电记为“未归属”。
- 残差采用最大份额尾差校正，保证恒等式：
      Σ片段分摊 + 未归属 = 表计增量（区间级与账期级都成立）。

引擎不接触数据库与 HTTP，便于离线单测与确定性复算。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN, getcontext
from itertools import combinations

getcontext().prec = 40

ZERO = Decimal("0")
# 电量与时长统一保留 4 位小数；内部用高精度 Decimal，落库前量化。
Q = Decimal("0.0001")

# 异常类型（与 constants/enums.py 的 EnergyAnomaly 保持一致）
RESET = "meter_reset"          # 换表回零或读数下降
DUP_TS = "duplicate_timestamp"  # 同表同时间戳多读数
GAP = "reading_gap"            # 读数缺口（相邻间隔超阈值，或首尾无法覆盖）
OVERLAP = "segment_overlap"    # 片段交叠
BAD_SEGMENT = "bad_segment"    # 片段零/负时长或时间非法


def D(value) -> Decimal:
    """把 int/float/str 安全转 Decimal（float 先经 str，避免二进制尾巴）。"""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def q(value: Decimal) -> Decimal:
    return value.quantize(Q, rounding=ROUND_HALF_EVEN)


def _anom(kind, scope, message, **ref):
    return {"kind": kind, "scope": scope, "message": message, "ref": ref}


def validate_readings(readings):
    """规范化读数并检测换表/回零/重复/缺口。返回 (clean, anomalies, ordered)。"""
    anomalies = []
    ordered = []
    for r in readings:
        try:
            meter = str(r["meter_serial"])
            ts = D(r["ts"])
            val = D(r["value"])
            rid = r.get("id")
        except (KeyError, TypeError, ValueError, ArithmeticError):
            anomalies.append(_anom(GAP, "reading", "读数缺少表号/时间/数值或格式非法", raw=r))
            continue
        if not ts.is_finite() or not val.is_finite() or val < 0:
            anomalies.append(_anom(RESET, "reading", "读数时间或数值非法（含负读数）", raw=r))
            continue
        ordered.append({"id": rid, "meter_serial": meter, "ts": ts, "value": val})

    ordered.sort(key=lambda x: (x["meter_serial"], x["ts"], x["id"] or 0))

    # 同表同时间戳
    seen = set()
    for o in ordered:
        key = (o["meter_serial"], o["ts"])
        if key in seen:
            anomalies.append(_anom(DUP_TS, "reading", "同一电表出现重复时间戳读数",
                                   meter_serial=o["meter_serial"], ts=float(o["ts"])))
        seen.add(key)
    return ordered, anomalies


def build_intervals(ordered, max_gap_seconds=None):
    """把同表相邻读数转成计量区间，识别回零/负差/缺口。"""
    intervals = []
    anomalies = []
    by_meter = {}
    for o in ordered:
        by_meter.setdefault(o["meter_serial"], []).append(o)

    gap_limit = D(max_gap_seconds) if max_gap_seconds else None
    for meter, rows in by_meter.items():
        rows.sort(key=lambda x: (x["ts"], x["id"] or 0))
        for prev, cur in zip(rows, rows[1:]):
            if prev["ts"] == cur["ts"]:
                # 重复时间戳区间不可用，隔离
                anomalies.append(_anom(DUP_TS, "interval", "同时间戳相邻读数无法构成区间",
                                       meter_serial=meter, ts=float(cur["ts"])))
                continue
            duration = cur["ts"] - prev["ts"]
            delta = cur["value"] - prev["value"]
            flags = []
            if delta < 0:
                flags.append(RESET)
                anomalies.append(_anom(RESET, "interval",
                                       "读数下降，疑似换表回零或接线异常，区间隔离待核对",
                                       meter_serial=meter,
                                       start_ts=float(prev["ts"]), end_ts=float(cur["ts"]),
                                       prev_value=float(prev["value"]), cur_value=float(cur["value"])))
            if gap_limit is not None and duration > gap_limit:
                flags.append(GAP)
                anomalies.append(_anom(GAP, "interval",
                                       "相邻读数间隔超过核对阈值，存在读数缺口，区间隔离",
                                       meter_serial=meter,
                                       start_ts=float(prev["ts"]), end_ts=float(cur["ts"]),
                                       gap_seconds=float(duration)))
            intervals.append({
                "meter_serial": meter,
                "start_ts": prev["ts"], "end_ts": cur["ts"],
                "start_value": prev["value"], "end_value": cur["value"],
                "duration": duration,
                "delta": delta if delta >= 0 else ZERO,
                "raw_delta": delta,
                "isolated": bool(flags),
                "flags": flags,
                "prev_id": prev["id"], "cur_id": cur["id"],
            })
    intervals.sort(key=lambda x: (x["start_ts"], x["meter_serial"]))
    return intervals, anomalies


def validate_segments(segments):
    clean, anomalies = [], []
    for s in segments:
        try:
            sid = s.get("id")
            start = D(s["start_ts"])
            end = D(s["end_ts"])
            label = str(s.get("label") or s.get("name") or (sid if sid is not None else "片段"))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            anomalies.append(_anom(BAD_SEGMENT, "segment", "片段缺少起止时间或格式非法", raw=s))
            continue
        if not start.is_finite() or not end.is_finite() or end <= start:
            anomalies.append(_anom(BAD_SEGMENT, "segment", "片段起止时间非法或时长为零/负，已隔离",
                                   segment_id=sid, start_ts=float(start) if start.is_finite() else None,
                                   end_ts=float(end) if end.is_finite() else None))
            continue
        clean.append({"id": sid, "label": label, "start_ts": start, "end_ts": end})

    clean.sort(key=lambda x: (x["start_ts"], x["end_ts"]))
    # 片段交叠（半开区间 [start,end)：首尾相接不算交叠）
    for a, b in combinations(clean, 2):
        if a["start_ts"] < b["end_ts"] and b["start_ts"] < a["end_ts"]:
            anomalies.append(_anom(OVERLAP, "segment", "片段时间范围交叠，相关片段隔离待核对",
                                   segment_a=a.get("id"), segment_b=b.get("id"),
                                   a_start=float(a["start_ts"]), b_start=float(b["start_ts"])))
    return clean, anomalies


def overlap_seconds(interval, segment):
    lo = max(interval["start_ts"], segment["start_ts"])
    hi = min(interval["end_ts"], segment["end_ts"])
    return hi - lo if hi > lo else ZERO


def allocate(readings, segments, max_gap_seconds=None):
    """主入口：返回账期试算结果（不写库）。

    返回 dict：intervals / entries / unassigned / anomalies / totals / identity_ok
    """
    ordered, ra = validate_readings(readings)
    intervals, ia = build_intervals(ordered, max_gap_seconds)
    clean_segments, sa = validate_segments(segments)
    anomalies = ra + ia + sa

    # 交叠/非法片段不参与分摊
    overlap_ids = {a["ref"].get("segment_a") for a in sa if a["kind"] == OVERLAP}
    overlap_ids |= {a["ref"].get("segment_b") for a in sa if a["kind"] == OVERLAP}
    bad_ids = {a["ref"].get("segment_id") for a in sa if a["kind"] == BAD_SEGMENT}
    eligible = [s for s in clean_segments if s.get("id") not in overlap_ids | bad_ids]

    entries = []          # 片段-区间 分摊明细
    unassigned = []       # 区间级未归属
    meter_delta_total = ZERO
    allocated_total = ZERO
    unassigned_total = ZERO

    for iv in intervals:
        if iv["isolated"]:
            # 隔离区间不参与分摊，其 delta 不进正常账，留待人工核对
            continue
        delta = q(iv["delta"])
        meter_delta_total += delta
        if iv["duration"] == ZERO or delta == ZERO:
            if delta > ZERO:
                unassigned.append({"meter_serial": iv["meter_serial"],
                                   "start_ts": float(iv["start_ts"]), "end_ts": float(iv["end_ts"]),
                                   "amount": float(delta), "reason": "zero_duration_interval"})
                unassigned_total += delta
            continue

        weights = []
        for seg in eligible:
            ov = overlap_seconds(iv, seg)
            if ov > ZERO:
                weights.append((seg, ov))
        weight_sum = sum((w for _, w in weights), ZERO)

        if weight_sum == ZERO:
            amount = delta
            unassigned.append({"meter_serial": iv["meter_serial"],
                               "start_ts": float(iv["start_ts"]), "end_ts": float(iv["end_ts"]),
                               "amount": float(amount), "reason": "no_segment_coverage"})
            unassigned_total += amount
            continue

        # 比例分摊 + 最大份额尾差校正，确保逐项之和严格等于区间增量
        raw = [(seg, q(delta * w / weight_sum)) for seg, w in weights]
        accounted = sum((a for _, a in raw), ZERO)
        remainder = delta - accounted
        if remainder != ZERO and raw:
            # 权重最大者吸收舍入残差
            idx = max(range(len(raw)), key=lambda i: (weights[i][1], i))
            seg0, amt0 = raw[idx]
            raw[idx] = (seg0, amt0 + remainder)

        for seg, amount in raw:
            entries.append({
                "segment_id": seg.get("id"), "segment_label": seg["label"],
                "meter_serial": iv["meter_serial"],
                "start_ts": float(iv["start_ts"]), "end_ts": float(iv["end_ts"]),
                "overlap_seconds": float(q(overlap_seconds(iv, seg))),
                "amount": float(amount),
                "amount_exact": str(amount),
            })
            allocated_total += amount

    # 片段级汇总
    by_segment = {}
    for e in entries:
        s = by_segment.setdefault(e["segment_id"], {
            "segment_id": e["segment_id"], "segment_label": e["segment_label"],
            "amount": ZERO, "amount_exact": "0", "intervals": 0})
        s["amount"] += D(e["amount_exact"])
        s["intervals"] += 1
    segment_totals = []
    for v in by_segment.values():
        v["amount_exact"] = str(q(v["amount"]))
        v["amount"] = float(q(v["amount"]))
        segment_totals.append(v)
    segment_totals.sort(key=lambda x: (x["segment_id"] is None, x["segment_id"]))

    identity_ok = (q(allocated_total + unassigned_total) == q(meter_delta_total))
    return {
        "intervals": [{
            "meter_serial": i["meter_serial"], "start_ts": float(i["start_ts"]),
            "end_ts": float(i["end_ts"]), "delta": float(q(i["raw_delta"])),
            "isolated": i["isolated"], "flags": i["flags"],
        } for i in intervals],
        "entries": entries,
        "segment_totals": segment_totals,
        "unassigned": unassigned,
        "anomalies": anomalies,
        "totals": {
            "meter_delta": float(q(meter_delta_total)),
            "allocated": float(q(allocated_total)),
            "unassigned": float(q(unassigned_total)),
            "entry_count": len(entries),
            "interval_count": len(intervals),
            "isolated_interval_count": sum(1 for i in intervals if i["isolated"]),
        },
        "identity_ok": identity_ok,
        "has_anomaly": bool(anomalies),
    }


def revision_delta(original_entries, revised_entries):
    """对比原账与修订账的片段分摊，输出逐片段差异（原账/简报不被改写）。"""
    def index(entries):
        out = {}
        for e in entries:
            sid = e["segment_id"]
            out[sid] = out.get(sid, ZERO) + D(str(e.get("amount_exact", e.get("amount"))))
        return out

    old, new = index(original_entries), index(revised_entries)
    diffs = []
    for sid in sorted(set(old) | set(new), key=lambda x: (x is None, x)):
        o = q(old.get(sid, ZERO))
        n = q(new.get(sid, ZERO))
        diffs.append({"segment_id": sid, "original": float(o), "revised": float(n),
                      "delta": float(n - o)})
    net = sum((D(str(d["delta"])) for d in diffs), ZERO)
    return {"diffs": diffs, "net_delta": float(q(net))}
