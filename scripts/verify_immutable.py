#!/usr/bin/env python3
"""不可变边界专项核对（独立临时库，不触碰仓库 data/）。

题目夹具：
A. 重建改已钉金额：已钉日重放零写入；即便有人绕过服务层 SQL 篡改 sealed 行/批头，
   预览/总览/重建报告/批次列表都必须暴露指纹分叉，禁止假绿；
B. 报告≠变更集：replay-report 的笔数/合计实时取自事实表实际行集，与库内行数逐一对齐，
   且失败桶非空时 reconciled 必为 False；
C. 只补声明却覆盖已钉：任何重放都不得覆盖 sealed 行，钉时快照逐行保持；
D. 交错后仍假绿：重建 ∥ 钉扎 并发交错，只允许串行提交，结束后指纹必然自洽、互证不假绿。

另含：老库（无 fingerprint/pin_snapshot 列）启动迁移并回填指纹。
"""
from __future__ import annotations

import os
import sys
import tempfile
import pathlib
import threading

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

_tmp = tempfile.mkdtemp(prefix="vouchpin-immutable-")
os.environ["DATA_DIR"] = _tmp

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.db import connect  # noqa: E402
from app.services import replay_service, redeem_service  # noqa: E402

client = TestClient(app)
DAY = "2026-11-07"
D2 = "2026-11-08"
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def setup_day(day: str, codes: list[tuple[str, str]]) -> None:
    client.put("/api/settings/day", json={"business_day": day})
    for code, camp in codes:
        r = client.post("/api/events", json={
            "event_type": "redeem", "coupon_code": code,
            "campaign_code": camp, "business_day": day,
        })
        assert r.status_code == 200, r.text


def sealed_snapshot(day: str):
    rows = [f for f in client.get("/api/facts", params={"business_day": day}).json()
            if f["status"] == "sealed"]
    return sorted((f["id"], f["coupon_code"], f["amount"], f["batch_id"]) for f in rows)


# ---------- 夹具 C/A 前置：钉扎一个正常批次 ----------
setup_day(DAY, [("P-1", "NEW10"), ("P-2", "VIP20"), ("P-3", "TEA5")])
r = client.post("/api/redeem/seal", json={"business_day": DAY})
check("钉扎成功并返回指纹", r.status_code == 200 and len(r.json().get("fingerprint", "")) == 16, r.text)
pinned = sealed_snapshot(DAY)
pin_total = round(sum(row[2] for row in pinned), 2)

# ---------- 夹具 C：已钉日重放不得覆盖任何 sealed 行 ----------
r = client.post("/api/redeem/replay", json={"business_day": DAY})
check("已钉日重放 409", r.status_code == 409, r.text)
check("sealed 行与钉时快照逐行一致（只补声明不得覆盖已钉）", sealed_snapshot(DAY) == pinned)
batch_now = client.get("/api/batches").json()[0]
check("批头合计/笔数未被抬动", batch_now["total_amount"] == pin_total and batch_now["fact_count"] == 3)
check("批次自报指纹正常", batch_now["fingerprint_ok"] is True and batch_now["header_ok"] is True)

# 钉后新到的 redeem 事件：重放仍被拒，且不得偷偷补 open（已钉日写闸）
client.post("/api/events", json={
    "event_type": "redeem", "coupon_code": "P-4", "campaign_code": "NEW10", "business_day": DAY,
})
r = client.post("/api/redeem/replay", json={"business_day": DAY})
check("钉后新事件重放仍 409", r.status_code == 409)
check("已钉日未被补出任何 open 行",
      all(f["status"] != "open" for f in client.get("/api/facts", params={"business_day": DAY}).json()))
check("sealed 行依旧逐行不变", sealed_snapshot(DAY) == pinned)

# ---------- 夹具 A：绕过服务层篡改 sealed 金额，全链路必须暴露分叉、禁止假绿 ----------
conn = connect(immediate=True)
conn.execute(
    "UPDATE redeem_facts SET amount=999 WHERE business_day=? AND status='sealed' AND coupon_code='P-1'",
    (DAY,),
)
conn.commit()
conn.close()

pv = client.get("/api/redeem/preview", params={"business_day": DAY}).json()
check("篡改后预览互证判分叉（不假绿）", pv["reconciled"] is False)
check("预览指纹校验失败", pv["fingerprint_ok"] is False)
check("预览带回分叉行定位",
      any((d.get("current") or {}).get("coupon_code") == "P-1"
          for d in (pv["batch"] or {}).get("diverged_facts", [])),
      str(pv.get("batch")))

rep = client.get("/api/redeem/replay-report", params={"business_day": DAY}).json()
check("重建报告 reconciled=False（不假绿）", rep["reconciled"] is False)
check("失败桶含 sealed_fact_rewritten",
      any(f["code"] == "sealed_fact_rewritten" for f in rep["failures"]), str(rep["failures"]))
check("报告 sealed 笔数实时=实际行数 3", rep["sealed_count"] == 3)
check("报告 sealed 合计实时=被篡改后的 1024", rep["sealed_total"] == 1024.0, str(rep["sealed_total"]))

d = client.get("/api/dashboard").json()
check("总览互证暴露分叉（recon_ok=False）", d["recon_ok"] is False, str(d))
check("总览指纹态暴露分叉（fingerprint_ok=False）", d["fingerprint_ok"] is False)
batches = client.get("/api/batches").json()
b = next(x for x in batches if x["business_day"] == DAY)
check("批次列表指纹标红且带分叉明细", b["fingerprint_ok"] is False and len(b["diverged_facts"]) == 1)

# 篡改态下重放仍被拒，且不会“修复成功”地假绿
r = client.post("/api/redeem/replay", json={"business_day": DAY})
check("分叉态重放仍 409（不会重建成功洗白）", r.status_code == 409)
rep2 = client.get("/api/redeem/replay-report", params={"business_day": DAY}).json()
check("分叉态报告依旧非绿", rep2["reconciled"] is False and rep2["failures"])

# ---------- 批头被抬：header 守卫单独暴露 ----------
conn = connect(immediate=True)
conn.execute("UPDATE redeem_batches SET total_amount=ROUND(total_amount+50,2) WHERE business_day=?", (DAY,))
conn.commit()
conn.close()
rep3 = client.get("/api/redeem/replay-report", params={"business_day": DAY}).json()
check("抬批头进入失败桶 batch_header_drift",
      any(f["code"] == "batch_header_drift" for f in rep3["failures"]), str(rep3["failures"]))
check("抬批头后报告仍非绿", rep3["reconciled"] is False)
b2 = next(x for x in client.get("/api/batches").json() if x["business_day"] == DAY)
check("批次 header_ok=False 且现算合计可对照", b2["header_ok"] is False and b2["current_total_amount"] == 1024.0)

# ---------- 夹具 B：未钉日重建，报告笔数 == 事实实际变更集合 ----------
setup_day(D2, [("Q-1", "NEW10"), ("Q-2", "TEA5")])
# 先制造一行漏物化 + 一行金额被篡改的 open
conn = connect(immediate=True)
conn.execute("DELETE FROM redeem_facts WHERE business_day=? AND coupon_code='Q-2'", (D2,))
conn.execute("UPDATE redeem_facts SET amount=88 WHERE business_day=? AND coupon_code='Q-1'", (D2,))
conn.commit()
conn.close()
r = client.post("/api/redeem/replay", json={"business_day": D2})
check("未钉日重建 200", r.status_code == 200, r.text)
body = r.json()
open_rows = [f for f in client.get("/api/facts", params={"business_day": D2}).json() if f["status"] == "open"]
void_rows = [f for f in client.get("/api/facts", params={"business_day": D2}).json() if f["status"] == "void"]
check("报告 created_open_facts=实际 open 行数", body["created_open_facts"] == len(open_rows) == 2)
check("报告 voided_open_facts=实际 void 行数", body["voided_open_facts"] == len(void_rows) == 1)
check("报告 changed_count=作废+补建的实际变更集合",
      body["changed_count"] == len(void_rows) + len(open_rows) == 3)
rep_d2 = client.get("/api/redeem/replay-report", params={"business_day": D2}).json()
check("报告笔数/合计与事实表行集一致（报告≠变更集不再发生）",
      rep_d2["open_count"] == len(open_rows) == 2
      and rep_d2["open_total"] == 15.0
      and rep_d2["reconciled"] is True
      and rep_d2["failures"] == [])
check("无失败桶时才允许绿色", rep_d2["reconciled"] is True)

# ---------- 夹具 D：重建 ∥ 钉扎 并发交错，串行提交、无假绿 ----------
D3 = "2026-11-09"
setup_day(D3, [("S-1", "NEW10"), ("S-2", "VIP20"), ("S-3", "TEA5")])
errors: list[str] = []
barrier = threading.Barrier(6)


def worker(kind: str) -> None:
    barrier.wait()
    try:
        for _ in range(5):
            if kind == "replay":
                try:
                    replay_service.replay_day(D3)
                except Exception as e:  # 已钉后 409 属预期
                    if "409" not in str(e) and "钉扎" not in str(e):
                        errors.append(f"replay: {e}")
            else:
                try:
                    redeem_service.seal({"business_day": D3})
                except Exception as e:  # 已钉后 409/无 open 400 属预期
                    if "409" not in str(e) and "400" not in str(e):
                        errors.append(f"seal: {e}")
    except Exception as e:  # noqa: BLE001
        errors.append(f"{kind} fatal: {e}")


threads = [threading.Thread(target=worker, args=("replay",)) for _ in range(3)] + \
          [threading.Thread(target=worker, args=("seal",)) for _ in range(3)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("并发交错无数据库级撕裂错误", not errors, str(errors[:3]))

final_pv = client.get("/api/redeem/preview", params={"business_day": D3}).json()
final_batches = [b for b in client.get("/api/batches").json() if b["business_day"] == D3]
check("交错后恰好一个钉扎批", len(final_batches) == 1, str(final_batches))
if final_batches:
    fb = final_batches[0]
    check("交错后批指纹自洽（无静默改写）", fb["fingerprint_ok"] is True and fb["header_ok"] is True)
    check("交错后互证不假绿（实时可对才绿）", final_pv["reconciled"] is True)
    check("不存在任何 replay_green 持久标志",
          connect().execute("SELECT 1 FROM settings WHERE key LIKE 'replay_green:%' LIMIT 1").fetchone() is None)

# ---------- 老库迁移：旧 schema 在全新进程启动后自动补列并回填指纹 ----------
import json  # noqa: E402
import subprocess  # noqa: E402
legacy_tmp = tempfile.mkdtemp(prefix="vouchpin-legacy-")
legacy_db = pathlib.Path(legacy_tmp) / "clipbond.db"
import sqlite3  # noqa: E402
lc = sqlite3.connect(legacy_db)
lc.executescript("""
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE campaigns(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL, face_value REAL NOT NULL, min_spend REAL NOT NULL DEFAULT 0);
CREATE TABLE coupon_events(id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
  coupon_code TEXT NOT NULL, campaign_code TEXT NOT NULL, amount REAL NOT NULL DEFAULT 0,
  business_day TEXT NOT NULL, occurred_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '');
CREATE TABLE redeem_facts(id INTEGER PRIMARY KEY AUTOINCREMENT, coupon_code TEXT NOT NULL,
  campaign_code TEXT NOT NULL, amount REAL NOT NULL, business_day TEXT NOT NULL,
  event_id INTEGER NOT NULL, batch_id INTEGER, status TEXT NOT NULL DEFAULT 'open',
  CHECK (status IN ('open','sealed','void')));
CREATE TABLE redeem_batches(id INTEGER PRIMARY KEY AUTOINCREMENT, business_day TEXT NOT NULL,
  status TEXT NOT NULL, fact_count INTEGER NOT NULL DEFAULT 0, total_amount REAL NOT NULL DEFAULT 0,
  sealed_at TEXT, note TEXT NOT NULL DEFAULT '');
""")
lc.execute("INSERT INTO settings VALUES('business_day','2026-11-01')")
lc.execute("INSERT INTO redeem_batches(business_day,status,fact_count,total_amount,sealed_at) "
           "VALUES('2026-11-01','sealed',1,10,'2026-11-01T20:00:00')")
lc.execute("INSERT INTO redeem_facts(coupon_code,campaign_code,amount,business_day,event_id,batch_id,status) "
           "VALUES('L-1','NEW10',10,'2026-11-01',1,1,'sealed')")
lc.commit()
lc.close()

migrate_probe = (
    "import sys; sys.path.insert(0, %r);"
    "from app.db import init_db; init_db();"
    "import sqlite3; "
    "r = sqlite3.connect(%r).execute("
    "'SELECT fingerprint, pin_snapshot FROM redeem_batches WHERE id=1').fetchone();"
    "import json; print(json.dumps(r))"
) % (str(BACKEND), str(legacy_db))
proc = subprocess.run(
    [sys.executable, "-I", "-c", migrate_probe],
    env={**os.environ, "DATA_DIR": legacy_tmp},
    capture_output=True, text=True,
)
migrated = json.loads(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip() else None
check("老库迁移补列并回填指纹/快照",
      bool(migrated and migrated[0] and "L-1" in (migrated[1] or "")),
      proc.stderr.strip()[:200])

print()
if failures:
    print(f"{len(failures)} 项失败: {failures}")
    sys.exit(1)
print("全部不可变边界核对通过")
