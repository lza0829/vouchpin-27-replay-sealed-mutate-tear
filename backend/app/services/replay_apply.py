"""按业务日重放重建事实——只作用于未钉（open）事实的纯事务应用层。

不可变边界（全局唯一策略）：
- sealed 行/钉扎批的任何写操作都不允许出现在本模块；已钉闸由 replay_service 在入口拦截；
- 未钉日重建 = 先作废该日全部 open（status -> void，行留存审计），再按事件湖逐笔补建 open；
- 互证与绿色状态一律由 repositories.facts.build_day_snapshot 实时计算，不写任何标志位。
"""
from __future__ import annotations

from typing import Any


def apply(conn, business_day: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    """在已持有的写事务内执行未钉日重建。调用方必须先通过已钉闸与脏数据闸。"""
    # 1. 覆盖未钉：该日 open 全部 void 留存（sealed/void 行绝不动）
    cur = conn.execute(
        "UPDATE redeem_facts SET status='void', batch_id=NULL "
        "WHERE business_day=? AND status='open'",
        (business_day,),
    )
    voided = int(cur.rowcount or 0)

    # 2. 仅补真缺失：按事件湖逐笔重建 open 事实
    created = 0
    for event in events:
        conn.execute(
            """
            INSERT INTO redeem_facts(coupon_code, campaign_code, amount, business_day, event_id, status)
            VALUES(?,?,?,?,?, 'open')
            """,
            (
                event["coupon_code"],
                event["campaign_code"],
                float(event["amount"]),
                business_day,
                int(event["id"]),
            ),
        )
        created += 1

    return {
        "voided_open_facts": voided,
        "created_open_facts": created,
        # 实际变更集合 = 被作废的旧行 + 新建行；报告笔数必须与之一致
        "changed_count": voided + created,
        "sealed_rewritten": 0,
    }
