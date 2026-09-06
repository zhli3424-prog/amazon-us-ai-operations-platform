from __future__ import annotations

import math
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


def money_to_cents(value: Any) -> int:
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("金额必须是有限数字")
    return int(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


def cents_to_money(value: int) -> float:
    return float(Decimal(int(value)) / 100)


def inventory_status(stock_units: int, daily_sales: float, lead_time_days: int, pipeline_units: int = 0, moq: int = 1) -> dict[str, Any]:
    if daily_sales <= 0:
        return {"days": None, "level": "unknown", "label": "缺少销量数据", "reorder_point": None, "reorder_qty": 0}
    days = round(stock_units / daily_sales, 1)
    level, label = (("critical", "紧急补货") if days <= lead_time_days else ("warning", "需要补货") if days <= lead_time_days + 7 else ("healthy", "库存健康"))
    raw_qty = max(0, math.ceil(daily_sales * (lead_time_days + 14) - stock_units - pipeline_units))
    reorder_qty = math.ceil(raw_qty / max(1, moq)) * max(1, moq)
    return {"days": days, "level": level, "label": label, "reorder_point": lead_time_days + 7, "reorder_qty": reorder_qty}


def ticket_rules(rating: int, refund_requested: bool) -> dict[str, str]:
    if rating <= 2 and refund_requested:
        return {"priority": "P0", "topic": "after_sales", "sla": "2小时联系，24小时给方案，48小时闭环"}
    if rating <= 2 or refund_requested:
        return {"priority": "P1", "topic": "customer_risk", "sla": "24小时确认原因，72小时闭环"}
    return {"priority": "P2", "topic": "feedback", "sla": "3个工作日内跟进"}


def event_ticket_rules(event_type: str, rating: int, refund_requested: bool) -> dict[str, str]:
    if event_type == "return_refund" and (refund_requested or rating <= 2):
        return {"priority": "P0", "topic": "return_refund", "sla": "2小时核验订单，24小时给出处理方案"}
    if event_type in {"order_exception", "buyer_cancel"} or rating <= 2:
        return {"priority": "P1", "topic": event_type, "sla": "24小时核验并完成首次响应"}
    return {"priority": "P2", "topic": "buyer_message", "sla": "2个工作日内人工回复"}


def ticket_due_at(event_at: str, priority: str) -> str:
    hours = {"P0": 2, "P1": 24, "P2": 48}.get(priority, 48)
    return (datetime.fromisoformat(event_at.replace("Z", "+00:00")) + timedelta(hours=hours)).isoformat(timespec="seconds")


def allocate_cents(total_cents: int, weights: list[int]) -> list[int]:
    """按权重分摊整数美分，并把舍入余数归入最后一行。"""
    denominator, used, allocated = sum(weights), 0, []
    for index, weight in enumerate(weights):
        share = total_cents - used if index == len(weights) - 1 else (total_cents * weight // denominator if denominator else 0)
        allocated.append(share)
        used += share
    return allocated
