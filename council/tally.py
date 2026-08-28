"""加权统计层 — 分权重协议的核心算法。

- weighted_vote_tally: FINALIZE 票按模型权重累加，判断是否达到阈值
- borda_tally: 互审排名的加权 Borda 分（仅死锁平票时生效）
- gate_verdicts: 质量门禁加权判定 + critical 一票否决
- merge_reviews: 评审模式把多模型发现按权重合并为确认/待定
"""

from __future__ import annotations

import re


def severity_rank(sev: str) -> int:
    return {"critical": 0, "major": 1, "minor": 2, "suggestion": 3}.get(
        sev.lower() if sev else "minor", 3
    )


def issue_key(iss: dict) -> str:
    """问题归一化 key（标题+类别），用于跨模型去重。"""
    title = (iss.get("title") or iss.get("case") or "").lower().strip()
    cat = (iss.get("category") or "").lower().strip()
    clean = re.sub(r"[^a-z0-9\u4e00-\u9fff\s]", "", title)
    return f"{cat}:{clean}"[:120]


def weighted_vote_tally(
    votes: list[dict],
    weights: dict[str, float],
    pass_threshold: float,
) -> dict:
    """统计 FINALIZE 投票。

    votes: [{"model": id, "vote": "FINALIZE", "endorse": "A", ...}, ...]
    weights: {model_id: weight}
    返回: support(各提案支持权重), total, winner, winner_share, result
    """
    support: dict[str, float] = {}
    finalize_count = 0
    total = 0.0
    for v in votes:
        w = weights.get(v.get("model", ""), 1.0)
        total += w
        if v.get("vote") == "FINALIZE" and v.get("endorse"):
            support[v["endorse"]] = support.get(v["endorse"], 0.0) + w
            finalize_count += 1
    winner = None
    share = 0.0
    if support:
        winner = max(support, key=support.get)
        share = support[winner] / total if total else 0.0
    result = (
        "finalized" if (winner and share >= pass_threshold)
        else "deadlock" if finalize_count == 0
        else "revise"
    )
    return {
        "support": support,
        "total": total,
        "winner": winner,
        "winner_share": share,
        "result": result,
    }


def votes_stalled(prev: list[dict], cur: list[dict]) -> bool:
    """两轮投票完全一致 → 参与者停止移动 → 死锁。"""
    def norm(votes: list[dict]) -> set[tuple]:
        return {(v.get("model"), v.get("vote"), v.get("endorse")) for v in votes}
    return bool(prev) and norm(prev) == norm(cur)


def borda_tally(rankings: list[dict], weights: dict[str, float]) -> dict[str, float]:
    """加权 Borda 分：ranking 每份提案按名次得分，乘以评审者权重。"""
    scores: dict[str, float] = {}
    for r in rankings:
        w = weights.get(r.get("reviewer", ""), 1.0)
        ranking = r.get("ranking") or []
        n = len(ranking)
        for idx, label in enumerate(ranking):
            scores[label] = scores.get(label, 0.0) + (n - idx) * w
    return scores


def gate_verdicts(
    verdicts: list[dict],
    weights: dict[str, float],
    pass_threshold: float,
    veto_enabled: bool,
    veto_min_weight: float,
) -> dict:
    """质量门禁：agree/partial 视为支持；critical blocker 触发否决。"""
    total = 0.0
    support = 0.0
    vetoes: list[dict] = []
    for v in verdicts:
        w = weights.get(v.get("model", ""), 1.0)
        total += w
        verdict = v.get("verdict", "disagree")
        if verdict in ("agree", "partial"):
            support += w
        blockers = v.get("critical_blockers") or []
        if veto_enabled and w >= veto_min_weight and blockers:
            vetoes.append({"model": v.get("model"), "blockers": blockers})
    share = support / total if total else 0.0
    passed = share >= pass_threshold and not vetoes
    return {
        "passed": passed,
        "support_share": share,
        "threshold": pass_threshold,
        "vetoes": vetoes,
        "summary": "通过" if passed else "不通过",
    }


def merge_reviews(
    results: list[dict],
    weights: dict[str, float],
    confirm_threshold: float = 0.5,
) -> dict:
    """评审模式合并：按模型权重归一化每条发现的"置信度"。

    confidence = 发现该问题的模型权重和 / 有效模型总权重
    confidence >= confirm_threshold → 确认问题；否则 → 待定。
    """
    issues_map: dict[str, dict] = {}
    model_scores: dict[str, float] = {}
    summaries: dict[str, str] = {}
    highlights: list[str] = []
    industrial: list[str] = []
    valid = [r for r in results if not r.get("error")]
    total_w = sum(weights.get(r.get("model", ""), 1.0) for r in valid) or 1.0

    for r in valid:
        m = r.get("model", "?")
        model_scores[m] = r.get("overall_score")
        summaries[m] = r.get("summary", "")
        for iss in r.get("issues", []):
            key = issue_key(iss)
            if key not in issues_map:
                issues_map[key] = {
                    "issue": iss,
                    "finders": set(),
                    "severity": iss.get("severity", "minor"),
                }
            entry = issues_map[key]
            entry["finders"].add(m)
            if severity_rank(iss.get("severity", "minor")) < severity_rank(entry["severity"]):
                entry["issue"] = iss
                entry["severity"] = iss.get("severity", "minor")
        highlights.extend(r.get("highlights", []))
        industrial.extend(r.get("industrial_concerns", []))

    confirmed, pending = [], []
    for key, entry in issues_map.items():
        finder_w = sum(weights.get(f, 1.0) for f in entry["finders"])
        confidence = finder_w / total_w
        item = {
            "issue": entry["issue"],
            "finders": sorted(entry["finders"]),
            "confidence": round(confidence, 2),
        }
        (confirmed if confidence >= confirm_threshold else pending).append(item)
    confirmed.sort(key=lambda x: severity_rank(x["issue"].get("severity", "minor")))
    pending.sort(key=lambda x: severity_rank(x["issue"].get("severity", "minor")))
    return {
        "model_scores": model_scores,
        "model_summaries": summaries,
        "confirmed_issues": confirmed,
        "pending_issues": pending,
        "highlights": list(dict.fromkeys(highlights)),
        "industrial_concerns": list(dict.fromkeys(industrial)),
        "total_models": len(valid),
    }
