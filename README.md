# Vouchpin（券钉簿）

骨架：券事件入湖 → 核销事实钉扎 → 过期/退回窗（后挖）→ 批次互证。

本地：
- API: `cd backend && python -m uvicorn app.main:app --port 10800`
- Web: `cd frontend && npm install && npm run dev`（5800）

## 按日重放重建事实（全局唯一策略：已钉行不可变 + 覆盖未钉事实）

事件湖完整、事实表被误删或漏物化时，按业务日重放重建 `redeem_facts`。全系统只贯彻这一种策略：

1. **已钉闸（不可变边界）**：业务日存在 `sealed` 钉扎批一律拒绝重放（409，零写入）；`sealed` 事实金额不改、不删、不重建，批头（笔数/合计）不抬不动。钉后新到事件也不得借“补缺口”名义写入该日。
2. **覆盖未钉**：单事务（`BEGIN IMMEDIATE`，全局互斥）内先把该日全部 `open` 事实置为 `void`（行留存可审计），再按事件湖中的 `redeem` 事件逐笔补建 `open` 事实——只补真缺失，绝不覆盖已钉行。
3. **批指纹**：钉扎时对 sealed 行规范集合计算 `fingerprint` 并落 `pin_snapshot` 逐行快照；预览/总览/批次列表/重建报告读取时重算比对。sealed 行或批头被任何路径静默改写都会判指纹分叉，互证必须暴露分叉、禁止假绿。
4. **同源互证与失败桶**：事件扫描、事实写、已钉闸、预览均经由 `repositories/facts.py` 同一组函数；重建后在同一事务内校验逐码、合计一致且报告笔数等于事实实际变更集合，不一致整体回滚。`GET /redeem/replay-report` 实时计算（无持久化绿色标志），`failures` 失败桶承载 `sealed_fact_rewritten` / `batch_header_drift` / `event_fact_mismatch`，桶非空时 `reconciled` 必为 false。
5. **并发交错**：重放、钉扎、核销写入全部 `BEGIN IMMEDIATE` 串行化；重建进行中同日钉扎不可能成功（反之亦然），不会出现“一边改已钉行一边仍标正常已钉”。
6. 同券码同日多笔 redeem 属脏数据，重放 400 拦截且不写库。

接口：`POST /api/redeem/replay`，body `{"business_day": "YYYY-MM-DD"}`（可空，取当前业务日）。
预览 `GET /api/redeem/preview` 返回 `redeem_event_total`、`reconciled`、`fingerprint_ok` 互证字段；报告 `GET /api/redeem/replay-report` 返回实时互证与失败桶。

事实状态：`open`（待钉）/ `sealed`（已钉，不可变）/ `void`（重放作废，留存）。

核对脚本：`scripts/verify_replay.py`（漏事实日、覆盖未钉幂等、已钉日拒绝、脏数据拦截）与
`scripts/verify_immutable.py`（夹具：重建改已钉金额、报告≠变更集、只补声明却覆盖已钉、交错后假绿、批头漂移、老库迁移）。
