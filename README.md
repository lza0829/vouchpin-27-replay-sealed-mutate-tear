# Vouchpin（券钉簿）

骨架：券事件入湖 → 核销事实钉扎 → 过期/退回窗（后挖）→ 批次互证。

本地：
- API: `cd backend && python -m uvicorn app.main:app --port 10800`
- Web: `cd frontend && npm install && npm run dev`（5800）

## 按日重放重建事实（全局唯一策略：覆盖未钉事实）

事件湖完整、事实表被误删或漏物化时，按业务日重放重建 `redeem_facts`。全系统只贯彻这一种策略：

1. **已钉闸**：业务日存在 `sealed` 钉扎批一律拒绝重放（409）；`sealed` 事实金额不改、不删、不重建。
2. **覆盖未钉**：单事务（`BEGIN IMMEDIATE`，全局互斥）内先把该日全部 `open` 事实置为 `void`（行留存可审计），再按事件湖中的 `redeem` 事件逐笔重建 `open` 事实。
3. **同源互证**：事件扫描、事实写、已钉闸、预览均经由 `repositories/facts.py` 同一组函数；重建后在同一事务内校验 open 事实与 redeem 事件逐码、合计一致，不一致整体回滚。
4. 同券码同日多笔 redeem 属脏数据，重放 400 拦截且不写库。

接口：`POST /api/redeem/replay`，body `{"business_day": "YYYY-MM-DD"}`（可空，取当前业务日）。
预览 `GET /api/redeem/preview` 返回 `redeem_event_total` 与 `reconciled` 互证字段。

事实状态：`open`（待钉）/ `sealed`（已钉，不可变）/ `void`（重放作废，留存）。

核对脚本：`scripts/verify_replay.py`（漏事实日与已钉日各重放一次等场景）。
