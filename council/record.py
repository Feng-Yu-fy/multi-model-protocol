"""会话档案 — 每次协议审议全程落盘，可审计、可恢复、可追溯（借鉴 ringi/ensemble）。

sessions/<id>/
├── prompt.md / state.json / final.md / cost.json
├── proposals/P01.md …  reviews/R01.md …  rebuttals/RB01.md …  votes/r1.json
└── synthesis.md
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import Config, ModelSpec
from .util import now_iso, safe_write


class SessionRecorder:
    def __init__(self, cfg: Config, session_id: str, mode: str, question: str):
        self.root = Path(cfg.protocol.session_dir) / session_id
        self.cfg = cfg
        self.mode = mode
        self.question = question
        self.costs: dict[str, dict] = {}
        for d in ("proposals", "reviews", "rebuttals", "votes"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        safe_write(self.root / "prompt.md",
                   f"# 协议审议会话 {session_id}\n\n- 时间: {now_iso()}\n"
                   f"- 模式: {mode}\n- 问题: {question}\n")

    def save_json(self, name: str, obj: dict) -> Path:
        p = self.root / name
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    def save_md(self, subdir: str, name: str, title: str, text: str) -> Path:
        p = self.root / subdir / name
        safe_write(p, f"# {title}\n\n{text}\n")
        return p

    def add_cost(self, spec: ModelSpec, result) -> None:
        """累加单次调用的 token 与成本。"""
        entry = self.costs.setdefault(
            spec.id,
            {"provider": spec.provider, "model": spec.model,
             "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
             "cost_usd": 0.0},
        )
        entry["calls"] += 1
        entry["prompt_tokens"] += result.usage.get("prompt_tokens", 0)
        entry["completion_tokens"] += result.usage.get("completion_tokens", 0)
        entry["cost_usd"] = round(entry["cost_usd"] + result.cost_usd, 6)

    def total_cost(self) -> float:
        return round(sum(e["cost_usd"] for e in self.costs.values()), 6)

    def cost_table(self) -> str:
        lines = ["| 模型 | 供应商 | 调用 | 输入tok | 输出tok | 成本$ |",
                 "|---|---|---|---|---|---|"]
        for mid, e in self.costs.items():
            lines.append(f"| {mid} | {e['provider']} | {e['calls']} | {e['prompt_tokens']} "
                         f"| {e['completion_tokens']} | {e['cost_usd']} |")
        lines.append(f"| **合计** | | | | | **{self.total_cost()}** |")
        return "\n".join(lines)

    def save_cost(self) -> Path:
        return self.save_json("cost.json", {
            "by_model": self.costs,
            "total_usd": self.total_cost(),
        })

    def finalize(self, state: dict, human_decision: str = "待人工确认") -> Path:
        """写最终报告 final.md + 完整 state.json。"""
        s = state
        lines = [
            "# 最终审议报告",
            "",
            f"- 会话: {self.root.name}",
            f"- 模式: {self.mode}",
            f"- 问题: {self.question}",
            "",
            "## 参与阵容（决策权重）",
            "",
        ]
        for p in s.get("participants", []):
            lines.append(f"- {p['label']}: {p['name']}（权重 {p['weight']}，{p['provider']}）")
        lines.append("")
        lines.append("## 结论")
        lines.append("")
        lines.append(s.get("answer") or s.get("result_summary") or "（无共识）")
        lines.append("")
        if s.get("preserved_minority"):
            lines.append("## 保留的少数意见")
            lines.append("")
            lines.extend(f"- {m}" for m in s["preserved_minority"])
            lines.append("")
        if s.get("open_questions"):
            lines.append("## 待人工确认事项")
            lines.append("")
            lines.extend(f"- {q}" for q in s["open_questions"])
            lines.append("")
        if s.get("sources"):
            lines.append("## 来源")
            lines.append("")
            for i, src in enumerate(s["sources"], 1):
                lines.append(f"- [{i}] {src.get('title','')} {src.get('url','')}")
            lines.append("")
        lines.append("## 成本")
        lines.append("")
        lines.append(self.cost_table())
        lines.append("")
        lines.append(f"## 人工决定\n\n{human_decision}")
        final_md = self.root / "final.md"
        safe_write(final_md, "\n".join(lines))
        state.setdefault("human_decision", human_decision)
        self.save_json("state.json", state)
        return final_md
