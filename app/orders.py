"""D3 订单持久化：logs/orders.jsonl 追加写，启动回放重建；经营统计与质检建议。"""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
from typing import Any

from pydantic import BaseModel, Field

from . import config

TERMINAL_STATUSES = {"done", "failed", "reworked"}


class Order(BaseModel):
    order_id: str
    repo_url: str
    prd: str | None = None
    demo_url: str | None = None
    price: float = 0.0
    status: str = "unpaid"  # unpaid→pending→approved→running→done/failed/reworked
    created_at: float
    paid_at: float | None = None
    approved_at: float | None = None
    started_at: float | None = None
    done_at: float | None = None
    owner_comment: str = ""
    qa_mark: str = "none"  # none | pass | rework
    qa_suggested: bool = False
    qa_suggest_reasons: list[str] = Field(default_factory=list)
    rework_of: str | None = None
    rework_reason: str = ""
    refunded: bool = False
    note: str = ""
    totals: dict[str, Any] = Field(default_factory=dict)
    user_id: str = ""
    user_name: str = ""

    # D3.5：营业数据纯度 / 自动放行核验
    is_test: bool = False
    auto_approved: bool = False
    verify_status: str = "unverified"  # unverified | verified | evaded
    verified_at: float | None = None
    client_ip: str = ""


class OrderStore:
    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self._loaded = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 持久化
    def load(self) -> None:
        if self._loaded:
            return
        path = config.ORDERS_FILE
        if path.exists():
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                        order = Order(**payload["order"])
                    except Exception:
                        continue
                    self._orders[order.order_id] = order
        self._loaded = True

    def _append(self, order: Order) -> None:
        try:
            config.ensure_dirs()
            line = json.dumps(
                {"ts": time.time(), "order": order.model_dump()},
                ensure_ascii=False,
            )
            with self._lock:
                with open(config.ORDERS_FILE, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError:
            pass

    def _write_summary(self) -> None:
        try:
            payload = [order.model_dump() for order in self._orders.values()]
            tmp = config.ORDERS_SUMMARY_FILE.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(config.ORDERS_SUMMARY_FILE)
        except OSError:
            pass

    def upsert(self, order: Order) -> Order:
        self._orders[order.order_id] = order
        self._append(order)
        if order.status in TERMINAL_STATUSES:
            self._write_summary()
        return order

    # ------------------------------------------------------------ 状态机
    def create(
        self,
        order_id: str,
        repo_url: str,
        prd: str | None = None,
        demo_url: str | None = None,
        price: float | None = None,
        is_test: bool = False,
        client_ip: str = "",
        user_id: str = "",
        user_name: str = "",
    ) -> Order:
        order = Order(
            order_id=order_id,
            repo_url=repo_url,
            prd=prd,
            demo_url=demo_url,
            price=config.PRICE_YUAN if price is None else price,
            created_at=time.time(),
            is_test=is_test,
            client_ip=client_ip,
            user_id=user_id,
            user_name=user_name,
        )
        return self.upsert(order)

    def active_count_by_ip(self, client_ip: str) -> int:
        if not client_ip:
            return 0
        return sum(
            1
            for order in self._orders.values()
            if order.client_ip == client_ip
            and order.status in {"unpaid", "pending", "approved", "running"}
        )

    def get(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def all(self) -> list[Order]:
        return list(self._orders.values())

    def transition(
        self,
        order_id: str,
        status: str | None = None,
        **fields: Any,
    ) -> Order | None:
        order = self._orders.get(order_id)
        if order is None:
            return None
        now = time.time()
        if status:
            order.status = status
            if status == "pending" and order.paid_at is None:
                order.paid_at = now
            elif status == "approved" and order.approved_at is None:
                order.approved_at = now
            elif status == "running" and order.started_at is None:
                order.started_at = now
            elif status == "done" and order.done_at is None:
                order.done_at = now
        for key, value in fields.items():
            if hasattr(order, key):
                setattr(order, key, value)
        return self.upsert(order)

    # ------------------------------------------------------------ 统计
    def stats(self, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        all_orders = self.all()
        orders = [order for order in all_orders if not order.is_test]
        today = dt.date.today()

        def _date(ts: float | None) -> dt.date | None:
            return dt.datetime.fromtimestamp(ts).date() if ts else None

        approved = [order for order in orders if order.approved_at]
        today_approved = [
            order for order in approved if _date(order.approved_at) == today
        ]
        total_revenue = round(sum(order.price for order in approved), 2)
        today_revenue = round(sum(order.price for order in today_approved), 2)

        current_hour = dt.datetime.fromtimestamp(now).replace(
            minute=0, second=0, microsecond=0
        )
        hourly = []
        cumulative = []
        for offset in range(11, -1, -1):
            start = current_hour - dt.timedelta(hours=offset)
            end = start + dt.timedelta(hours=1)
            count = sum(
                1
                for order in approved
                if order.approved_at and start.timestamp() <= order.approved_at < end.timestamp()
            )
            hourly.append({"hour": start.strftime("%H:00"), "count": count})
            cumulative.append(
                {
                    "hour": start.strftime("%H:00"),
                    "value": round(
                        sum(
                            order.price
                            for order in approved
                            if order.approved_at and order.approved_at < end.timestamp()
                        ),
                        2,
                    ),
                }
            )

        return {
            "today_count": len(today_approved),
            "today_revenue": today_revenue,
            "total_count": len(approved),
            "total_revenue": total_revenue,
            "pending_count": sum(1 for order in orders if order.status == "pending"),
            "rework_count": sum(1 for order in orders if order.rework_of),
            "suggested_count": sum(1 for order in orders if order.qa_suggested),
            "test_count": sum(1 for order in all_orders if order.is_test),
            "unverified_count": sum(
                1
                for order in orders
                if order.auto_approved and order.verify_status == "unverified"
            ),
            "evaded_count": sum(
                1 for order in orders if order.verify_status == "evaded"
            ),
            "hourly": hourly,
            "cumulative": cumulative,
        }

    # ------------------------------------------------------------ 质检
    def suggestions(
        self,
        order: Order,
        finals: list[Any],
        totals: dict[str, Any],
    ) -> list[str]:
        if order.is_test:
            return []
        reasons: list[str] = []
        scored = [
            item
            for item in finals
            if getattr(item, "weight", 0) > 0 and not getattr(item, "degraded", False)
        ]

        if len(scored) == 4 and all(
            getattr(item, "final_verdict", "hold") == "turn" for item in scored
        ):
            reasons.append("四转单：极端好评，重点核对是否评委失准")

        if totals.get("valid_weight", 0) and totals.get("final", 100) <= 10:
            reasons.append("零分/超低单：终评 ≤10，核对是否为恶意/空壳项目")

        if abs(float(totals.get("delta", 0))) >= 15:
            reasons.append("大翻盘单：临场力波动 ≥15，核对评委状态")

        # TODO(窦柘): 随机 1/3 抽检由老板人工习惯执行，不做进系统
        today = dt.date.today()
        done_today = [
            item
            for item in self.all()
            if item.status == "done"
            and item.done_at
            and dt.datetime.fromtimestamp(item.done_at).date() == today
            and item.order_id != order.order_id
        ]
        if len(done_today) < 2:
            reasons.append("每日校准单：当日前 2 个完成订单，建议人工抽检")

        return reasons


store = OrderStore()
