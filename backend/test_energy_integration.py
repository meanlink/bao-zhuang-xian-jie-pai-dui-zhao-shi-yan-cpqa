"""电量模块端到端集成测试：SQLite + FastAPI TestClient（无需 MySQL/Redis）。

运行：PYTHONPATH=/tmp/energy-deps:. python3 -I test_energy_integration.py
覆盖：乱序接入、双水位封账、恒等式、异常隔离拒绝结案、封账后拒绝补传、
修订账（断点/幂等/原账保留）、简报固定、封账门控批准、并发补传不漏计。
"""
import os
import sys
import tempfile
import threading

# 隔离环境：使用临时 sqlite 与临时产物目录，-I 下 PYTHONPATH 失效，故显式加入测试依赖
for _p in ("/tmp/energy-deps", os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 隔离环境：使用临时 sqlite 与临时产物目录，-I 隔离外部包路径由 PYTHONPATH 提供
tmp = tempfile.mkdtemp(prefix="energy-it-")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tmp, 'it.db')}"
os.environ["ARTIFACT_DIR"] = os.path.join(tmp, "artifacts")
os.environ["JWT_SECRET"] = "it-secret"
os.environ["REDIS_URL"] = "redis://127.0.0.1:6399/0"  # 无 redis，限流中间件静默兜底

sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient  # noqa: E402
from migrations.initial import migrate  # noqa: E402
from src.config.database import Session  # noqa: E402
from src.models import User, EnergyPeriod, EnergyMeterReading  # noqa: E402
from src.utils.security import hash_password  # noqa: E402
from src.main import app  # noqa: E402

migrate()
with Session() as db:
    for role in ("admin", "analyst", "viewer"):
        if not db.query(User).filter_by(email=f"{role}@demo.local").first():
            db.add(User(email=f"{role}@demo.local", name=role, role=role,
                        password=hash_password("Demo123!")))
    db.commit()

client = TestClient(app)
failures = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, "" if cond else detail)
    if not cond:
        failures.append(name)


def token(role):
    r = client.post("/auth/login", json={"email": f"{role}@demo.local", "password": "Demo123!"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


admin = {"Authorization": f"Bearer {token('admin')}"}
analyst = {"Authorization": f"Bearer {token('analyst')}"}
viewer = {"Authorization": f"Bearer {token('viewer')}"}

# ---- 建账期 ----
r = client.post("/energy/periods", headers=analyst, json={
    "period_key": "P-2026W1", "name": "第1周电量对账", "start_ts": 0, "end_ts": 100,
    "max_gap_seconds": 60})
check("建账期", r.status_code == 201, r.text)
pid = r.json()["id"]

# viewer 不可写
check("viewer 只读", client.post(f"/energy/periods/{pid}/watermark", headers=viewer,
                                 json={"stream": "meter", "watermark_ts": 100}).status_code == 403)

# ---- 乱序接入表计读数（故意打乱顺序）----
readings = [
    {"meter_serial": "M1", "reading_ts": 40, "reading_value": 135, "idempotency_key": "r4"},
    {"meter_serial": "M1", "reading_ts": 0, "reading_value": 100, "idempotency_key": "r1"},
    {"meter_serial": "M1", "reading_ts": 20, "reading_value": 130, "idempotency_key": "r3"},
    {"meter_serial": "M1", "reading_ts": 10, "reading_value": 110, "idempotency_key": "r2"},
]
r = client.post(f"/energy/periods/{pid}/ingest", headers=analyst,
                json={"stream": "meter", "items": readings})
check("接入4读数", r.status_code == 200 and r.json()["accepted"] == 4, r.text)
# 重试（相同幂等键）不重复
r2 = client.post(f"/energy/periods/{pid}/ingest", headers=analyst,
                 json={"stream": "meter", "items": readings})
check("重试幂等不重复", r2.json()["accepted"] == 0 and r2.json()["skipped_duplicate"] == 4, r2.text)

segments = [
    {"segment_ref": "S-A", "label": "A 原动作", "start_ts": 0, "end_ts": 20, "idempotency_key": "s1"},
    {"segment_ref": "S-B", "label": "B 改动作", "start_ts": 20, "end_ts": 40, "idempotency_key": "s2"},
]
r = client.post(f"/energy/periods/{pid}/ingest", headers=analyst,
                json={"stream": "segment", "items": segments})
check("接入2片段", r.status_code == 200 and r.json()["accepted"] == 2, r.text)

# ---- 试算恒等式 ----
r = client.get(f"/energy/periods/{pid}/preview", headers=viewer)
prev = r.json()
check("试算无异常", prev["has_anomaly"] is False, prev.get("anomalies"))
check("试算恒等式", prev["identity_ok"] is True, prev["totals"])
check("表计增量35", abs(prev["totals"]["meter_delta"] - 35) < 1e-9, prev["totals"])

# ---- 水位未齐拒绝结案 ----
client.post(f"/energy/periods/{pid}/watermark", headers=analyst,
            json={"stream": "meter", "watermark_ts": 100})
r = client.post(f"/energy/periods/{pid}/close", headers=admin)
check("单路水位拒绝结案", r.status_code == 409, r.text)
# analyst 不能封账
client.post(f"/energy/periods/{pid}/watermark", headers=analyst,
            json={"stream": "segment", "watermark_ts": 100})
r = client.post(f"/energy/periods/{pid}/close", headers=analyst)
check("analyst 不能封账", r.status_code == 403, r.text)

# ---- 正式封账 ----
r = client.post(f"/energy/periods/{pid}/close", headers=admin)
check("封账成功", r.status_code == 200 and r.json()["status"] == "closed", r.text)
book_id = r.json()["id"]
check("封账恒等式标记", r.json()["identity_ok"] is True)
# 封账幂等：重复结案返回同本
r = client.post(f"/energy/periods/{pid}/close", headers=admin)
check("重复封账幂等", r.status_code == 200 and r.json().get("idempotent") is True
      and r.json()["id"] == book_id, r.text)

# ---- 封账后拒绝直接补传 ----
r = client.post(f"/energy/periods/{pid}/ingest", headers=analyst,
                json={"stream": "meter", "items": [
                    {"meter_serial": "M1", "reading_ts": 50, "reading_value": 140,
                     "idempotency_key": "late1"}]})
check("封账后拒绝补传", r.status_code == 409, r.text)

# ---- 明细恒等式核对 ----
r = client.get(f"/energy/periods/{pid}/entries", headers=viewer)
entries = r.json()
from decimal import Decimal as D
seg_sum = sum((D(e["amount_exact"]) for e in entries if not e["is_unassigned"]), D("0"))
un_sum = sum((D(e["amount_exact"]) for e in entries if e["is_unassigned"]), D("0"))
check("明细恒等式 片段+未归属=增量", seg_sum + un_sum == D("35.0000"),
      (str(seg_sum), str(un_sum)))

# ---- 固定简报可下载且含校验指纹 ----
r = client.get(f"/energy/briefing/book/{book_id}", headers=viewer)
check("简报下载", r.status_code == 200 and "checksum" in r.json(), r.status_code)
check("简报指纹=原账", r.json()["checksum"] == client.get(
    f"/energy/periods/{pid}/book", headers=viewer).json()["checksum"])

# ---- 关联分析：封账后才能批准 ----
# 造一个 completed job
from src.models import Dataset, Job
from src.services.business import seed_records, analyze
with Session() as db:
    ds = Dataset(name="IT-ds", source="it", records=seed_records(), checksum="x", created_at=1.0)
    db.add(ds); db.flush()
    res = analyze(ds.records, {"confidence": "0.95", "shift": "全部"})
    admin_user = db.query(User).filter_by(role="admin").first()
    job = Job(name="IT-job", dataset_id=ds.id, params={}, status="completed", result=res,
              created_by=admin_user.id, created_at=1.0, approved=False)
    db.add(job); db.flush(); job_id = job.id; db.commit()

r = client.post("/energy/approve-analysis", headers=admin,
                json={"job_id": job_id, "period_id": pid})
check("封账后批准分析", r.status_code == 200 and r.json()["approved"] is True, r.text)

# ---- 修订账：迟到更正 ----
r = client.post(f"/energy/periods/{pid}/revisions", headers=analyst,
                json={"reason": "迟到读数 M1@50 补登", "idempotency_key": "rev-1"})
check("开修订账", r.status_code == 201, r.text)
rev_id = r.json()["id"]
check("原账状态保留", client.get(f"/energy/periods/{pid}/book", headers=viewer)
      .json()["status"] == "closed")
# 重复修订键拒绝
r = client.post(f"/energy/periods/{pid}/revisions", headers=analyst,
                json={"reason": "重复", "idempotency_key": "rev-1"})
check("修订键幂等", r.status_code == 409, r.text)

corr = {"stream": "meter", "idempotency_key": "c-late1",
        "payload": {"meter_serial": "M1", "reading_ts": 50, "reading_value": 140}}
r = client.post(f"/energy/revisions/{rev_id}/corrections", headers=analyst, json=corr)
check("加更正", r.status_code == 200 and r.json()["duplicate"] is False, r.text)
r = client.post(f"/energy/revisions/{rev_id}/corrections", headers=analyst, json=corr)
check("更正幂等", r.json()["duplicate"] is True, r.text)
# 同步更正片段：S-B 延伸到 50s 以覆盖迟到读数所在区间
corr_seg = {"stream": "segment", "idempotency_key": "c-seg1",
            "payload": {"segment_ref": "S-B", "label": "B 改动作(修订)",
                        "start_ts": 20, "end_ts": 50}}
r = client.post(f"/energy/revisions/{rev_id}/corrections", headers=analyst, json=corr_seg)
check("加片段更正", r.status_code == 200, r.text)

# 第一次处理
r = client.post(f"/energy/revisions/{rev_id}/process", headers=analyst)
check("修订完成", r.status_code == 200 and r.json()["status"] == "completed", r.text)
rev = r.json()
# 重试处理不重复
r2 = client.post(f"/energy/revisions/{rev_id}/process", headers=analyst)
check("修订重试幂等", r2.json().get("idempotent") is True, r2.text)
# 修订后增量变为 40（135->140 多了5）
check("修订后增量=40", abs(rev["totals"]["meter_delta"] - 40) < 1e-9, rev["totals"])
# 片段级差异：S-B 现在覆盖新增 5 度，净额应 +5
dmap = {d["segment_id"]: d["delta"] for d in rev["diff"]["diffs"]}
check("修订片段差异 S-B +5", abs(dmap.get("S-B", 0) - 5) < 1e-9, dmap)
check("修订差异净额+5", abs(rev["diff"]["net_delta"] - 5) < 1e-9, rev["diff"])
# 原账与原简报仍为 35，保留不变
orig = client.get(f"/energy/periods/{pid}/book", headers=viewer).json()
check("原账冻结保留35", abs(orig["totals"]["meter_delta"] - 35) < 1e-9, orig["totals"])
check("修订简报生成", client.get(f"/energy/briefing/revision/{rev_id}", headers=viewer).status_code == 200)
check("账期状态=revised", client.get(f"/energy/periods/{pid}", headers=viewer).json()["status"] == "revised")

# ---- 异常隔离：新建账期，构造换表回零 ----
r = client.post("/energy/periods", headers=analyst, json={
    "period_key": "P-BAD", "name": "异常账期", "start_ts": 0, "end_ts": 100})
bad_pid = r.json()["id"]
bad_readings = [
    {"meter_serial": "M1", "reading_ts": 0, "reading_value": 100, "idempotency_key": "b1"},
    {"meter_serial": "M1", "reading_ts": 10, "reading_value": 120, "idempotency_key": "b2"},
    {"meter_serial": "M1", "reading_ts": 20, "reading_value": 5, "idempotency_key": "b3"},
]
client.post(f"/energy/periods/{bad_pid}/ingest", headers=analyst,
            json={"stream": "meter", "items": bad_readings})
client.post(f"/energy/periods/{bad_pid}/ingest", headers=analyst,
            json={"stream": "segment", "items": segments})
client.post(f"/energy/periods/{bad_pid}/watermark", headers=analyst,
            json={"stream": "meter", "watermark_ts": 100})
client.post(f"/energy/periods/{bad_pid}/watermark", headers=analyst,
            json={"stream": "segment", "watermark_ts": 100})
r = client.post(f"/energy/periods/{bad_pid}/close", headers=admin)
check("异常隔离拒绝封账", r.status_code == 409, r.text)
check("账期置blocked", client.get(f"/energy/periods/{bad_pid}", headers=viewer).json()["status"] == "blocked")
anoms = client.get(f"/energy/periods/{bad_pid}/anomalies", headers=viewer).json()
check("记录回零异常", any(a["kind"] == "meter_reset" for a in anoms), anoms)

# ---- 并发补传不漏计：两个线程同时补不同读数，全部落库 ----
r = client.post("/energy/periods", headers=analyst, json={
    "period_key": "P-CONC", "name": "并发账期", "start_ts": 0, "end_ts": 1000})
conc_pid = r.json()["id"]
base = [{"meter_serial": "M1", "reading_ts": 0, "reading_value": 0, "idempotency_key": "base0"}]
client.post(f"/energy/periods/{conc_pid}/ingest", headers=analyst,
            json={"stream": "meter", "items": base})
errors = []


def worker(n):
    try:
        rr = client.post(f"/energy/periods/{conc_pid}/ingest", headers=analyst,
                         json={"stream": "meter", "items": [
                             {"meter_serial": "M1", "reading_ts": 100 + n,
                              "reading_value": 10 + n, "idempotency_key": f"c{n}"}]})
        if rr.status_code != 200:
            errors.append((n, rr.status_code, rr.text))
    except Exception as e:  # noqa
        errors.append((n, "exc", str(e)))


threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
[t.start() for t in threads]
[t.join() for t in threads]
check("并发补传无错误", not errors, errors)
with Session() as db:
    cnt = db.query(EnergyMeterReading).filter_by(period_id=conc_pid).count()
check("并发补传全部落库(9条)", cnt == 9, cnt)

print("\n%s，失败 %d 项" % ("全部通过" if not failures else "存在失败", len(failures)))
sys.exit(1 if failures else 0)
