#!/usr/bin/env python3
"""按业务日重放重建事实的端到端核对（独立临时库，不触碰仓库 data/）。

覆盖：
1. 漏物化日：有 redeem 事件、事实被误删 -> 重放补回，预览与事件合计可对；
2. 重放覆盖未钉 open（旧行 void 留存，金额以事件湖为准），可重复执行幂等；
3. 已钉（sealed）日拒绝重放，sealed 事实金额不改不删；
4. 同码多笔 redeem 被拦截且不写库。
"""
from __future__ import annotations

import os
import sys
import tempfile
import pathlib

# 隔离运行：脚本目录不放任何业务模块，仓库 backend 仅经 sys.path 显式引入
BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

_tmp = tempfile.mkdtemp(prefix="vouchpin-replay-")
os.environ["DATA_DIR"] = _tmp

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.db import connect  # noqa: E402

client = TestClient(app)
# 避开 seed 占用的今天/昨天，保证事件湖初始干净
DAY = "2026-11-05"
D2 = "2026-11-06"
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def api(method: str, path: str, **kw):
    r = getattr(client, method)(path, **kw)
    return r


def setup_day() -> None:
    api("put", "/api/settings/day", json={"business_day": DAY})
    # 三笔 redeem 事件（面额由活动补全：NEW10=10, VIP20=20, TEA5=5）
    for code, camp in [("R-1", "NEW10"), ("R-2", "VIP20"), ("R-3", "TEA5")]:
        r = api("post", "/api/events", json={
            "event_type": "redeem", "coupon_code": code,
            "campaign_code": camp, "business_day": DAY,
        })
        assert r.status_code == 200, r.text


def facts_by_status(day: str) -> dict[str, list[dict]]:
    rows = client.get("/api/facts", params={"business_day": day}).json()
    out: dict[str, list[dict]] = {}
    for f in rows:
        out.setdefault(f["status"], []).append(f)
    return out


# ---------- 场景 1：漏物化日重放 ----------
setup_day()
before = facts_by_status(DAY)
check("重放前 3 笔 open 事实随事件物化", len(before.get("open", [])) == 3)

# 误删/漏物化：直接把该日事实全部物理删除，事件湖保持完整
conn = connect(immediate=True)
conn.execute("DELETE FROM redeem_facts WHERE business_day=?", (DAY,))
conn.commit()
conn.close()
check("模拟事实表被误删后无事实", facts_by_status(DAY) == {})

pv_before = client.get("/api/redeem/preview", params={"business_day": DAY}).json()
check("漏事实日预览互证不符（待修复信号）", pv_before["reconciled"] is False,
      f"open={pv_before['open_total']} events={pv_before['redeem_event_total']}")
check("事件湖仍完整（3 笔，合计 35）",
      pv_before["redeem_event_count"] == 3 and pv_before["redeem_event_total"] == 35.0)

r = api("post", "/api/redeem/replay", json={"business_day": DAY})
check("漏事实日重放 200", r.status_code == 200, r.text)
body = r.json()
check("重放创建 3 笔 open", body["created_open_facts"] == 3)
check("重放返回互证通过", body["reconciled"] is True)
check("策略标识为覆盖未钉", body["strategy"] == "overwrite_open")

pv_after = client.get("/api/redeem/preview", params={"business_day": DAY}).json()
check("重建后预览与事件合计可对", pv_after["reconciled"] is True,
      f"open={pv_after['open_total']} events={pv_after['redeem_event_total']}")
check("重建后 open 合计=35、笔数=3",
      pv_after["open_total"] == 35.0 and pv_after["open_count"] == 3)

# 事件金额与事实金额逐笔对齐
ev = {e["coupon_code"]: e["amount"] for e in client.get("/api/events", params={"business_day": DAY}).json()
      if e["event_type"] == "redeem"}
ft = {f["coupon_code"]: f["amount"] for f in pv_after["facts"]}
check("逐券码金额与事件一致", ev == ft, f"{ev} vs {ft}")

# ---------- 场景 2：覆盖未钉 open + 幂等 ----------
# 先篡改一笔 open 金额（同码集、同行数、总额改变），互证必须报不符
conn = connect(immediate=True)
conn.execute("UPDATE redeem_facts SET amount=99 WHERE business_day=? AND status='open' AND coupon_code='R-1'", (DAY,))
conn.commit()
conn.close()
pv_tamper = client.get("/api/redeem/preview", params={"business_day": DAY}).json()
check("篡改金额后预览互证不符", pv_tamper["reconciled"] is False,
      f"open={pv_tamper['open_total']} events={pv_tamper['redeem_event_total']}")

r = api("post", "/api/redeem/replay", json={"business_day": DAY})
check("二次重放 200（幂等）", r.status_code == 200, r.text)
check("二次重放作废 3 笔旧 open", r.json()["voided_open_facts"] == 3)
check("二次重放仍创建 3 笔 open", r.json()["created_open_facts"] == 3)
check("重放按事件湖修复被篡改金额",
      client.get("/api/redeem/preview", params={"business_day": DAY}).json()["reconciled"] is True)
by = facts_by_status(DAY)
check("旧 open 以 void 留存（审计痕迹，未物理删除）", len(by.get("void", [])) == 3)
check("当前 open 仍为 3 笔且互证可对", len(by.get("open", [])) == 3
      and client.get("/api/redeem/preview", params={"business_day": DAY}).json()["reconciled"])

# dashboard 口径：void 留存行不进有效事实与核销合计
d = client.get("/api/dashboard").json()
check("dashboard 有效事实数不含 void（=3）", d["fact_count"] == 3, str(d))
check("dashboard 单列 void 数（=3）", d["void_fact_count"] == 3)
check("dashboard 核销合计不含 void（=35）", d["redeem_total_today"] == 35.0)

# ---------- 场景 3：已钉日拒绝重放，sealed 金额不被改写 ----------
r = api("post", "/api/redeem/seal", json={"business_day": DAY})
check("钉扎成功", r.status_code == 200, r.text)
sealed_before = facts_by_status(DAY).get("sealed", [])
sealed_snapshot = [(f["id"], f["coupon_code"], f["amount"]) for f in sealed_before]

r = api("post", "/api/redeem/replay", json={"business_day": DAY})
check("已钉日重放被拒 409", r.status_code == 409, r.text)
check("拒绝信息含 sealed 语义", "钉扎" in r.json()["detail"])

# 钉扎后 open 清零，但有效事实（sealed）仍与事件互证可对，不应误报不符
pv_sealed = client.get("/api/redeem/preview", params={"business_day": DAY}).json()
check("钉扎后互证仍可对（按 open+sealed 有效事实）", pv_sealed["reconciled"] is True, str(pv_sealed))
check("钉扎后 open 笔数为 0", pv_sealed["open_count"] == 0)
check("钉扎后事件侧合计仍为 35", pv_sealed["redeem_event_total"] == 35.0)

sealed_after = facts_by_status(DAY).get("sealed", [])
sealed_now = [(f["id"], f["coupon_code"], f["amount"]) for f in sealed_after]
check("sealed 事实行数不变", len(sealed_after) == 3)
check("sealed 事实金额/ID 未被重放改写", sealed_snapshot == sealed_now, f"{sealed_now}")

# 再补一笔该日 redeem 事件后重放，仍须被钉闸挡住，且不产生新事实
api("post", "/api/events", json={
    "event_type": "redeem", "coupon_code": "R-4", "campaign_code": "NEW10", "business_day": DAY,
})
r = api("post", "/api/redeem/replay", json={"business_day": DAY})
check("钉后新增事件，重放仍 409", r.status_code == 409)
check("钉后重放未新增任何 open 事实",
      len(facts_by_status(DAY).get("open", [])) == 0)
check("sealed 合计保持 35 不变",
      round(sum(f["amount"] for f in facts_by_status(DAY)["sealed"]), 2) == 35.0)

# ---------- 场景 4：同码多笔 redeem 拦截（直接造事件湖脏数据） ----------
# 正常写入路径有 open 唯一索引挡着，重复行只能从事件湖侧异常产生
conn = connect(immediate=True)
for _ in range(2):
    conn.execute(
        "INSERT INTO coupon_events(event_type, coupon_code, campaign_code, amount, "
        "business_day, occurred_at) VALUES('redeem','DUP','NEW10',10,?,?)",
        (D2, f"{D2}T10:00:00"),
    )
conn.commit()
conn.close()
r = api("post", "/api/redeem/replay", json={"business_day": D2})
check("同码多笔 redeem 重放被拒 400", r.status_code == 400, r.text)
check("拦截后不写事实", client.get("/api/facts", params={"business_day": D2}).json() == [])

print()
if failures:
    print(f"{len(failures)} 项失败: {failures}")
    sys.exit(1)
print("全部核对通过")
