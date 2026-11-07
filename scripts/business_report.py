"""经营流水与成本分析（纯标准库，零 LLM 调用）。

输入：
- logs/usage.jsonl        token 记账
- logs/orders.jsonl       订单流水（可缺失，降级用 sessions）
- logs/sessions/*.json    会话快照
- pricing.yaml            模型单价占位

输出：
- reports/business/经营流水与成本分析-YYYYMMDD-HHmm.md
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import re
import sys
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
REPORTS = ROOT / "reports" / "business"


def load_pricing() -> dict:
    """超轻量 YAML 解析，只支持本项目 pricing.yaml 的简单结构。"""
    path = ROOT / "pricing.yaml"
    data: dict = {}
    current: str | None = None
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith(" ") and line.endswith(":"):
            current = line[:-1].strip()
            data[current] = {}
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        try:
            number = float(value)
        except ValueError:
            continue
        if current and raw.startswith(" "):
            data[current][key] = number
        else:
            data[key] = number
            current = None
    return data


def load_usage() -> list[dict]:
    path = LOGS / "usage.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def load_orders() -> list[dict]:
    path = LOGS / "orders.jsonl"
    if not path.exists():
        return []
    last: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
            order = payload["order"]
            last[order["order_id"]] = order
        except (ValueError, KeyError):
            continue
    return list(last.values())


def load_sessions() -> list[dict]:
    sessions = []
    for path in sorted((LOGS / "sessions").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sessions.append(data)
    return sessions


def fmt_ts(ts: float | None) -> str:
    if not ts:
        return "-"
    return dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def build_report() -> tuple[str, dict]:
    pricing = load_pricing()
    usage = load_usage()
    all_orders = load_orders()
    test_count = sum(1 for order in all_orders if order.get("is_test"))
    orders = [order for order in all_orders if not order.get("is_test")]
    sessions = load_sessions()

    # ---------------------------------------------------------------- 流水
    orders_missing = not all_orders
    rows: list[dict] = []
    if all_orders:
        for order in sorted(all_orders, key=lambda item: item.get("created_at", 0)):
            rows.append(
                {
                    "id": order["order_id"],
                    "repo": order.get("repo_url", ""),
                    "status": order.get("status", "-"),
                    "price": float(order.get("price") or 0),
                    "created_at": order.get("created_at"),
                    "done_at": order.get("done_at"),
                    "duration": (
                        (order.get("done_at") or 0) - (order.get("created_at") or 0)
                        if order.get("done_at")
                        else None
                    ),
                    "approved": bool(order.get("approved_at")),
                    "rework_of": order.get("rework_of"),
                    "is_test": bool(order.get("is_test")),
                    "source": "orders",
                }
            )
    else:
        # 降级：只用 sessions 汇总
        for session in sorted(sessions, key=lambda item: item.get("created_at", 0)):
            phase = session.get("phase", "-")
            status = "done" if phase == "done" else ("failed" if session.get("error") else phase)
            rows.append(
                {
                    "id": session.get("id", "-"),
                    "repo": session.get("repo_url", ""),
                    "status": status,
                    "price": 0.0,
                    "created_at": session.get("created_at"),
                    "done_at": None,
                    "duration": None,
                    "approved": status == "done",
                    "rework_of": None,
                    "source": "sessions",
                }
            )

    total_orders = len(orders) if all_orders else len(rows)
    approved_rows = [
        row for row in rows if row["approved"] and not row.get("is_test")
    ]
    revenue = round(sum(row["price"] for row in approved_rows), 2)
    rework_count = sum(
        1 for row in rows if row["rework_of"] and not row.get("is_test")
    )
    done_raw = sum(
        1 for row in rows if row["status"] == "done" and not row.get("is_test")
    )
    done_count = max(1, done_raw)

    # ---------------------------------------------------------------- Usage
    by_caller = collections.defaultdict(lambda: {"calls": 0, "prompt": 0, "completion": 0})
    by_model = collections.defaultdict(lambda: {"calls": 0, "prompt": 0, "completion": 0})
    max_call = {"caller": "-", "total": 0}
    for item in usage:
        caller = str(item.get("caller", "-")).split(":")[0]
        model = str(item.get("model", "-"))
        prompt = int(item.get("prompt_tokens") or 0)
        completion = int(item.get("completion_tokens") or 0)
        for bucket, key in ((by_caller, caller), (by_model, model)):
            bucket[key]["calls"] += 1
            bucket[key]["prompt"] += prompt
            bucket[key]["completion"] += completion
        total = int(item.get("total_tokens") or (prompt + completion))
        if total > max_call["total"]:
            max_call = {"caller": item.get("caller", "-"), "total": total}

    total_tokens = sum(item["prompt"] + item["completion"] for item in by_caller.values())

    # ---------------------------------------------------------------- 成本
    pricing_models = {
        key: value
        for key, value in pricing.items()
        if isinstance(value, dict) and ("input" in value or "output" in value)
    }
    price_known = any(
        float(value.get("input", 0)) or float(value.get("output", 0))
        for value in pricing_models.values()
    )

    total_cost = 0.0
    for model, data in by_model.items():
        prices = pricing_models.get(model, {})
        total_cost += data["prompt"] / 1_000_000 * float(prices.get("input", 0))
        total_cost += data["completion"] / 1_000_000 * float(prices.get("output", 0))
    total_cost = round(total_cost, 4)

    per_order_cost = round(total_cost / done_count, 4) if price_known else None
    price_yuan = float(pricing.get("price_yuan", 19.9))
    gross_profit = round(price_yuan - total_cost / done_count, 4) if price_known else None
    human_hourly = float(pricing.get("human_expert_hourly", 300))
    human_hours = float(pricing.get("human_review_hours", 2))
    human_cost = round(human_hourly * human_hours, 2)

    flash_calls = sum(data["calls"] for model, data in by_model.items() if "flash" in model)
    all_calls = max(1, sum(data["calls"] for data in by_model.values()))

    # ---------------------------------------------------------------- 报告
    lines: list[str] = []
    now_label = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines.append(f"# 经营流水与成本分析 · {now_label}")
    lines.append("")
    lines.append("> 数据来源：`logs/usage.jsonl`、`logs/orders.jsonl`、`logs/sessions/*.json`。")
    lines.append("> 所有数字均可追溯到日志；人工成本为假设值，已在文中标注。")
    lines.append("")

    lines.append("## 一、经营流水")
    lines.append("")
    if orders_missing:
        lines.append("> ⚠️ 未找到 `logs/orders.jsonl`，以下流水仅来自会话记录，金额按 0 计。")
        lines.append("")
    lines.append("| 时间 | 会话ID | 仓库 | 状态 | 金额 | 评审耗时 |")
    lines.append("|---|---|---|---|---:|---|")
    for row in rows[-60:]:
        repo = str(row["repo"]).replace("|", " ")
        if len(repo) > 40:
            repo = repo[:37] + "..."
        duration = f"{row['duration']:.0f}s" if row["duration"] else "-"
        status = row["status"] + ("（测试）" if row.get("is_test") else "")
        lines.append(
            f"| {fmt_ts(row['created_at'])} | {str(row['id'])[:8]} | {repo} | {status} | "
            f"¥{row['price']:.2f} | {duration} |"
        )
    lines.append("")
    lines.append(
        f"**汇总**：真实总单数 {total_orders} ｜ 成交 {len(approved_rows)} ｜ "
        f"营业额 ¥{revenue:.2f} ｜ 重审 {rework_count} 单 ｜ 测试 {test_count} 笔（不计入）"
        + ("（仅会话记录）" if orders_missing else "")
    )
    lines.append("")

    lines.append("## 二、Agent 运行统计")
    lines.append("")
    lines.append("| caller | 调用次数 | prompt tokens | completion tokens |")
    lines.append("|---|---:|---:|---:|")
    for caller, data in sorted(by_caller.items(), key=lambda item: -item[1]["calls"]):
        lines.append(
            f"| {caller} | {data['calls']} | {data['prompt']} | {data['completion']} |"
        )
    lines.append("")
    lines.append("| 模型 | 调用次数 | prompt tokens | completion tokens |")
    lines.append("|---|---:|---:|---:|")
    for model, data in sorted(by_model.items(), key=lambda item: -item[1]["calls"]):
        lines.append(
            f"| {model} | {data['calls']} | {data['prompt']} | {data['completion']} |"
        )
    lines.append("")
    lines.append(
        f"- 总 token：**{total_tokens:,}** ｜ 完成订单：{done_raw} ｜ "
        f"平均每单 token：**{total_tokens // done_count:,}**"
    )
    lines.append("")

    lines.append("## 三、成本分析")
    lines.append("")
    if not price_known:
        lines.append(
            "> ⚠️ `pricing.yaml` 的模型单价为占位 0，**待 MiMo 官方价目确认**。"
            "以下只给出实测 token 量，不给出编造的成本数字。"
        )
        lines.append("")
        lines.append(f"- 模型总 token：**{total_tokens:,}**")
        lines.append(f"- 平均每单 token：**{total_tokens // done_count:,}**")
        lines.append("- 模型成本：待官方价目确认")
        lines.append(f"- 人机对比假设：人类专家 ¥{human_hourly:.0f}/小时 × {human_hours:.0f} 小时 = ¥{human_cost:.2f}/单")
        lines.append("- AI 单均成本：待官方价目确认，暂无法计算成本优势倍数")
    else:
        lines.append(f"- 模型总成本：**¥{total_cost:.4f}**")
        lines.append(f"- 单均模型成本：**¥{per_order_cost:.4f}**")
        lines.append(f"- 单均毛利（按定价 ¥{price_yuan:.2f}）：**¥{gross_profit:.4f}**")
        if total_cost > 0:
            lines.append(
                f"- 人机对比：人类专家 ¥{human_hourly:.0f}/小时 × {human_hours:.0f} 小时 = "
                f"¥{human_cost:.2f}/单；AI 成本约为人类的 **{human_cost / per_order_cost:.0f}×** 更低"
            )
        else:
            lines.append("- 人机对比：AI 单均成本低于可显示精度，优势倍数 >1000×")
    lines.append("")

    lines.append("## 四、MiMo 性价比叙述素材")
    lines.append("")
    lines.append(
        f"- flash 调用占比：**{flash_calls}/{all_calls}（{flash_calls / all_calls * 100:.1f}%）**，"
        "对应答辩实时响应与评委并行初评"
    )
    lines.append(
        f"- 单次最大 token：**{max_call['total']:,}**（caller `{max_call['caller']}`），"
        "对应解剖员吞仓库的长上下文场景"
    )
    lines.append("")

    report = "\n".join(lines) + "\n"
    summary = {
        "orders": total_orders,
        "approved": len(approved_rows),
        "revenue": revenue,
        "tokens": total_tokens,
        "cost_known": price_known,
        "total_cost": total_cost,
    }
    return report, summary


def main() -> int:
    report, summary = build_report()
    REPORTS.mkdir(parents=True, exist_ok=True)
    filename = f"经营流水与成本分析-{dt.datetime.now().strftime('%Y%m%d-%H%M')}.md"
    path = REPORTS / filename
    path.write_text(report, encoding="utf-8")

    print("=" * 46)
    print("经营流水与成本分析")
    print("=" * 46)
    print(f"订单总数：{summary['orders']}（成交 {summary['approved']}）")
    print(f"营业额：¥{summary['revenue']:.2f}")
    print(f"总 token：{summary['tokens']:,}")
    if summary["cost_known"]:
        print(f"模型总成本：¥{summary['total_cost']:.4f}")
    else:
        print("模型成本：待官方价目确认（pricing.yaml 为占位 0）")
    print(f"报告：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
