"""电量分摊引擎离线单测：仅标准库，可直接 `python test_energy_engine.py`。

覆盖：基础比例分摊与恒等式、未归属、换表回零隔离、读数缺口隔离、
片段交叠隔离、乱序到达、零耗电区间、修订差异、幂等复算确定性。
"""
import sys, os
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.services import energy_engine as eng  # noqa: E402

failures = []

def check(name, cond, detail=""):
    if cond:
        print("PASS", name)
    else:
        failures.append((name, detail))
        print("FAIL", name, detail)

# 1) 基础：一块表，4 个读数，两个片段覆盖整段；区间耗电按重叠时长比例分摊
readings = [
    {"id": 1, "meter_serial": "M1", "ts": 0, "value": 100},
    {"id": 2, "meter_serial": "M1", "ts": 10, "value": 110},   # +10, 时长10
    {"id": 3, "meter_serial": "M1", "ts": 30, "value": 130},   # +20, 时长20
    {"id": 4, "meter_serial": "M1", "ts": 40, "value": 135},   # +5,  时长10
]
segments = [
    {"id": 10, "label": "A", "start_ts": 0, "end_ts": 20},
    {"id": 11, "label": "B", "start_ts": 20, "end_ts": 40},
]
res = eng.allocate(readings, segments)
# 区间1[0,10]全在A(10)，区间2[10,30]: A占10(10-20),B占10(20-30)，区间3[30,40]全在B
# A = 10(区间1全额) + 20*(10/20)=10 => 20 ; B = 20*(10/20)=10 + 5 = 15
seg_amt = {s["segment_id"]: s["amount"] for s in res["segment_totals"]}
check("基础分摊 A=20", abs(seg_amt[10] - 20) < 1e-9, seg_amt)
check("基础分摊 B=15", abs(seg_amt[11] - 15) < 1e-9, seg_amt)
check("表计增量=35", abs(res["totals"]["meter_delta"] - 35) < 1e-9, res["totals"])
check("恒等式成立", res["identity_ok"] is True, res["totals"])
check("无异常", res["has_anomaly"] is False, res["anomalies"])

# 2) 未归属：片段未覆盖的区间耗电计入未归属，恒等式仍成立
segments_partial = [{"id": 10, "label": "A", "start_ts": 0, "end_ts": 10}]
res2 = eng.allocate(readings, segments_partial)
check("有未归属", res2["totals"]["unassigned"] > 0, res2["totals"])
check("未归属恒等式", eng.q(eng.D(res2["totals"]["allocated"]) + eng.D(res2["totals"]["unassigned"]))
      == eng.q(eng.D(res2["totals"]["meter_delta"])), res2["totals"])

# 3) 换表回零：读数下降 -> 隔离
reset_readings = [
    {"id": 1, "meter_serial": "M1", "ts": 0, "value": 100},
    {"id": 2, "meter_serial": "M1", "ts": 10, "value": 120},
    {"id": 3, "meter_serial": "M1", "ts": 20, "value": 5},     # 换表回零
    {"id": 4, "meter_serial": "M1", "ts": 30, "value": 8},
]
res3 = eng.allocate(reset_readings, segments)
kinds = {a["kind"] for a in res3["anomalies"]}
check("换表回零识别", "meter_reset" in kinds, kinds)
check("回零区间隔离", any(i["isolated"] for i in res3["intervals"]), res3["intervals"])

# 4) 读数缺口：间隔超阈值 -> 隔离
res4 = eng.allocate(readings, segments, max_gap_seconds=15)
check("缺口识别", any(a["kind"] == "reading_gap" for a in res4["anomalies"]), res4["anomalies"])
check("缺口区间隔离", any("reading_gap" in i["flags"] for i in res4["intervals"]))

# 5) 片段交叠 -> 隔离，异常存在
overlap_seg = [
    {"id": 10, "label": "A", "start_ts": 0, "end_ts": 25},
    {"id": 11, "label": "B", "start_ts": 20, "end_ts": 40},
]
res5 = eng.allocate(readings, overlap_seg)
check("片段交叠识别", any(a["kind"] == "segment_overlap" for a in res5["anomalies"]), res5["anomalies"])

# 6) 乱序到达：打乱输入顺序，结果一致
import random
rng = random.Random(7)
shuffled = readings[:]
rng.shuffle(shuffled)
res6a = eng.allocate(shuffled, segments)
res6b = eng.allocate(readings, segments)
check("乱序输入结果一致", res6a["totals"] == res6b["totals"] and
      sorted((e["segment_id"], e["amount_exact"]) for e in res6a["entries"]) ==
      sorted((e["segment_id"], e["segment_id"] and e["amount_exact"]) for e in res6b["entries"]))

# 7) 零耗电区间（读数持平）不产生分摊也不报错
flat = [
    {"id": 1, "meter_serial": "M1", "ts": 0, "value": 100},
    {"id": 2, "meter_serial": "M1", "ts": 10, "value": 100},
    {"id": 3, "meter_serial": "M1", "ts": 20, "value": 105},
]
res7 = eng.allocate(flat, segments)
check("零耗电无异常", res7["has_anomaly"] is False, res7["anomalies"])
check("零耗电恒等式", res7["identity_ok"] is True)

# 8) 舍入尾差：构造除不尽比例，逐项之和严格等于增量
awk = [
    {"id": 1, "meter_serial": "M1", "ts": 0, "value": 0},
    {"id": 2, "meter_serial": "M1", "ts": 3, "value": 10},  # 时长3，耗电10
]
awk_seg = [
    {"id": 1, "label": "A", "start_ts": 0, "end_ts": 1},
    {"id": 2, "label": "B", "start_ts": 1, "end_ts": 2},
    {"id": 3, "label": "C", "start_ts": 2, "end_ts": 3},
]
res8 = eng.allocate(awk, awk_seg)
s = eng.q(sum((eng.D(e["amount_exact"]) for e in res8["entries"]), eng.ZERO))
check("尾差校正后逐项和=增量", s == eng.q(eng.D(10)), (str(s), res8["entries"]))
check("尾差恒等式", res8["identity_ok"] is True)

# 9) 修订差异
base_entries = [{"segment_id": 1, "amount": 20, "amount_exact": "20.0000"},
                {"segment_id": 2, "amount": 15, "amount_exact": "15.0000"}]
rev_entries = [{"segment_id": 1, "amount": 22, "amount_exact": "22.0000"},
               {"segment_id": 2, "amount": 13, "amount_exact": "13.0000"},
               {"segment_id": 3, "amount": 1, "amount_exact": "1.0000"}]
diff = eng.revision_delta(base_entries, rev_entries)
dmap = {d["segment_id"]: d["delta"] for d in diff["diffs"]}
check("修订差异片段1=+2", abs(dmap[1] - 2) < 1e-9, dmap)
check("修订差异新增片段3", abs(dmap[3] - 1) < 1e-9, dmap)
check("修订净额=+1", abs(diff["net_delta"] - 1) < 1e-9, diff)

# 10) 多电表各自独立计量、分别识别
multi = readings + [
    {"id": 5, "meter_serial": "M2", "ts": 0, "value": 0},
    {"id": 6, "meter_serial": "M2", "ts": 40, "value": 7},
]
res10 = eng.allocate(multi, segments)
check("多电表增量合计", abs(res10["totals"]["meter_delta"] - 42) < 1e-9, res10["totals"])
check("多电表恒等式", res10["identity_ok"] is True)

print("\n%d 项失败" % len(failures))
sys.exit(1 if failures else 0)
