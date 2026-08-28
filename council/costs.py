"""费用面板：聚合 sessions/*/cost.json → 控制台表格 / 自包含 HTML 可视化。

说明：OpenCode Go 等订阅型通道按订阅计费，这里展示的是按 models.toml
估算单价换算的「估算成本」，用于观察各模型用量与相对开销，不代表真实扣费。
"""

from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# 供应商 → 中文来源标签（费用面板单独标注每个模型来自哪里）
SOURCE_LABELS = {
    "opencode": "OpenCode Go 订阅",
    "zhipu": "智谱开放平台",
    "deepseek": "DeepSeek 官方",
    "gemini": "Google AI Studio 免费",
    "gemini-cli": "Antigravity/agy 订阅",
    "openai": "OpenAI 官方",
    "moonshot": "Moonshot 官方",
    "dashscope": "阿里百炼",
    "openrouter": "OpenRouter",
    "xai": "xAI 官方",
    "mock": "本地模拟",
}


def source_label(provider: str) -> str:
    return SOURCE_LABELS.get(provider, provider)


def load_sessions(root: Path) -> list[dict]:
    rows: list[dict] = []
    if not root.exists():
        return rows
    for d in sorted(root.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        cost = d / "cost.json"
        if not cost.exists():
            continue
        try:
            data = json.loads(cost.read_text(encoding="utf-8"))
        except Exception:
            continue
        state: dict = {}
        sf = d / "state.json"
        if sf.exists():
            try:
                state = json.loads(sf.read_text(encoding="utf-8"))
            except Exception:
                pass
        rows.append({
            "session": d.name,
            "date": d.name[:8],
            "time": d.name[9:15] if len(d.name) > 15 else "",
            "mode": state.get("mode", "?"),
            "question": str(state.get("question", ""))[:40],
            "total": float(data.get("total_usd", 0.0)),
            "by_model": data.get("by_model", {}),
        })
    return rows


def aggregate(rows: list[dict]) -> dict[str, dict]:
    models: dict[str, dict] = defaultdict(lambda: {
        "provider": "", "model": "", "calls": 0,
        "prompt_tokens": 0, "completion_tokens": 0,
        "cost_usd": 0.0, "sessions": 0,
    })
    for r in rows:
        for mid, e in r["by_model"].items():
            m = models[mid]
            m["provider"] = e.get("provider", "")
            m["model"] = e.get("model", "")
            m["calls"] += int(e.get("calls", 0))
            m["prompt_tokens"] += int(e.get("prompt_tokens", 0))
            m["completion_tokens"] += int(e.get("completion_tokens", 0))
            m["cost_usd"] += float(e.get("cost_usd", 0.0))
            m["sessions"] += 1
    return dict(models)


def console_report(rows: list[dict], models: dict[str, dict]) -> str:
    total = sum(r["total"] for r in rows)
    lines = [
        f"参议层费用面板（{len(rows)} 场审议，估算成本合计 ${total:.4f}）",
        "",
        "| 模型 | 来源 | 型号 | 场次 | 调用 | 输入tok | 输出tok | 估算$ |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for mid in sorted(models, key=lambda k: -models[k]["cost_usd"]):
        m = models[mid]
        lines.append(
            f"| {mid} | {source_label(m['provider'])} | {m['model']} | "
            f"{m['sessions']} | "
            f"{m['calls']} | {m['prompt_tokens']} | {m['completion_tokens']} | "
            f"{m['cost_usd']:.4f} |")
    lines.append(f"| **合计** | | | | | | | **{total:.4f}** |")
    lines.append("")
    lines.append("按日期：")
    by_date: dict[str, float] = defaultdict(float)
    for r in rows:
        by_date[r["date"]] += r["total"]
    for date in sorted(by_date):
        lines.append(f"  {date}: ${by_date[date]:.4f}")
    lines.append("")
    lines.append("（订阅型通道按订阅计费，上表为 models.toml 估算单价换算的参考值）")
    return "\n".join(lines)


def html_report(rows: list[dict], models: dict[str, dict]) -> str:
    total = sum(r["total"] for r in rows)
    max_cost = max((m["cost_usd"] for m in models.values()), default=0.0) or 1.0
    model_rows = []
    for mid in sorted(models, key=lambda k: -models[k]["cost_usd"]):
        m = models[mid]
        pct = min(100.0, m["cost_usd"] / max_cost * 100)
        model_rows.append(f"""
      <div class="row">
        <div class="label">{html.escape(mid)}
          <span class="src">{html.escape(source_label(m['provider']))} · {html.escape(m['model'])}</span>
        </div>
        <div class="bar-wrap"><div class="bar" style="width:{pct:.1f}%"></div></div>
        <div class="val">${m['cost_usd']:.4f} · {m['calls']} 次</div>
      </div>""")
    session_rows = []
    for r in rows:
        session_rows.append(
            f"<tr><td>{html.escape(r['date'])}</td>"
            f"<td>{html.escape(r['time'])}</td>"
            f"<td>{html.escape(r['mode'])}</td>"
            f"<td title=\"{html.escape(r['question'])}\">{html.escape(r['question'][:30])}</td>"
            f"<td>${r['total']:.4f}</td></tr>")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>参议层费用面板</title>
<style>
  body {{ font-family: "Segoe UI", "Microsoft YaHei", sans-serif; margin: 24px; background: #0f1420; color: #dce3f0; }}
  h1 {{ font-size: 20px; }} h2 {{ font-size: 15px; margin-top: 28px; color: #9fb2d8; }}
  .stats {{ display: flex; gap: 16px; flex-wrap: wrap; }}
  .card {{ background: #182136; border: 1px solid #2a3a58; border-radius: 10px; padding: 12px 18px; min-width: 130px; }}
  .card b {{ font-size: 22px; color: #6fd3a5; }}
  .row {{ display: grid; grid-template-columns: 180px 1fr 150px; gap: 10px; align-items: center; margin: 8px 0; }}
  .bar-wrap {{ background: #1d2940; border-radius: 6px; height: 18px; overflow: hidden; }}
  .bar {{ height: 100%; background: linear-gradient(90deg, #2f8f6d, #6fd3a5); }}
  .src {{ display: block; font-size: 11px; color: #7f90ad; margin-top: 2px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  th, td {{ border: 1px solid #2a3a58; padding: 6px 10px; text-align: left; }}
  th {{ background: #1d2940; }}
  .note {{ color: #7f90ad; font-size: 12px; margin-top: 20px; }}
</style>
</head>
<body>
<h1>参议层费用面板</h1>
<div class="stats">
  <div class="card">审议场次<b>{len(rows)}</b></div>
  <div class="card">估算总成本<b>${total:.4f}</b></div>
  <div class="card">参与模型<b>{len(models)}</b></div>
</div>
<h2>按模型估算成本</h2>
{''.join(model_rows)}
<h2>最近审议</h2>
<table>
  <tr><th>日期</th><th>时间</th><th>模式</th><th>问题</th><th>成本$</th></tr>
  {''.join(session_rows)}
</table>
<div class="note">订阅型通道（OpenCode Go / agy）按订阅计费，本面板为 models.toml 估算单价的参考值；数据来自 sessions/*/cost.json。</div>
</body>
</html>"""


def write_dashboard(cfg, root: Path) -> Path:
    rows = load_sessions(root)
    models = aggregate(rows)
    out = cfg.base_dir / "costs_dashboard.html"
    out.write_text(html_report(rows, models), encoding="utf-8")
    return out


def json_report(rows: list[dict], models: dict[str, dict]) -> dict:
    """面板数据源：控制台/网页通用，含每个模型的来源标签。"""
    total = sum(r["total"] for r in rows)
    by_model = {}
    for mid, m in models.items():
        by_model[mid] = {
            "provider": m["provider"],
            "source": source_label(m["provider"]),
            "model": m["model"],
            "calls": m["calls"],
            "prompt_tokens": m["prompt_tokens"],
            "completion_tokens": m["completion_tokens"],
            "cost_usd": round(m["cost_usd"], 6),
            "sessions": m["sessions"],
        }
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_sessions": len(rows),
        "total_usd": round(total, 6),
        "model_count": len(models),
        "by_model": by_model,
        "sessions": rows,
        "note": "订阅型通道按订阅计费，成本为 models.toml 估算单价的参考值",
    }


def write_json_report(cfg, root: Path, out: Path) -> Path:
    rows = load_sessions(root)
    models = aggregate(rows)
    out.write_text(
        json.dumps(json_report(rows, models), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out
