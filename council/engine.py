"""审议引擎 — 协议流程编排。

完整流程（debate / design）:
  提案 propose → 匿名互审 review → 反驳 rebuttal → 加权投票 vote（最多 N 轮）
  → 主席综合 synthesize → 全体确认 confirm → 落盘

brainstorm: 提案 → 互审 → 主席综合（不投票）
research:   提案(可联网) → 互审 → 主席综合（保留来源）
review:     并行结构化评审 → 按权重合并为确认/待定
gate:       并行放行判定 → 加权门禁 + critical 一票否决
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import prompts as P
from .config import Config, ModelSpec
from .providers import LLMResult, call_model
from .record import SessionRecorder
from .tally import (borda_tally, gate_verdicts, merge_reviews,
                    votes_stalled, weighted_vote_tally)
from .util import extract_json, slugify, session_timestamp


@dataclass
class Participant:
    label: str
    name: str
    provider: str
    model: str
    weight: float
    spec: ModelSpec | None = None
    is_main: bool = False

    def as_dict(self) -> dict:
        return {
            "label": self.label, "name": self.name, "provider": self.provider,
            "model": self.model, "weight": self.weight, "is_main": self.is_main,
        }


COUNCIL_MODES = {"brainstorm", "debate", "design", "research"}


class CouncilEngine:
    def __init__(self, cfg: Config, mock: bool = False, out=print):
        self.cfg = cfg
        self.mock = mock or cfg.protocol.mock
        self.out = out

    # ── 参与者解析 ─────────────────────────────────────
    def _build_participants(self, model_ids: list[str],
                            opinion: str | None) -> list[Participant]:
        specs, warns = self.cfg.check_models(model_ids, require_key=not self.mock)
        for w in warns:
            self.out(f"  ⚠️ {w}")
        if not specs:
            raise RuntimeError("没有可用模型（缺 key 或未注册），无法启动协议审议")
        parts: list[Participant] = []
        if opinion:
            parts.append(Participant(
                label="M", name="主引擎", provider="main",
                model="main", weight=self.cfg.protocol.main_engine_weight,
                is_main=True,
            ))
        letters = "ABCDEFGHIJ"
        for i, spec in enumerate(specs):
            s = self._maybe_mock(spec)
            parts.append(Participant(
                label=letters[i], name=s.name, provider=s.provider,
                model=s.model, weight=s.weight, spec=s,
            ))
        return parts

    def _maybe_mock(self, spec: ModelSpec) -> ModelSpec:
        if not self.mock or spec.provider == "mock":
            return spec
        return ModelSpec(
            id=spec.id, name=spec.name, provider="mock", model="mock-" + spec.id,
            weight=spec.weight, roles=spec.roles, temperature=0,
            max_tokens=512, use_search=False,
        )

    def _weights(self, parts: list[Participant]) -> dict[str, float]:
        return {p.label: p.weight for p in parts}

    # ── 并行调用 ───────────────────────────────────────
    def _parallel(self, fn, items: list):
        """items: [(key, args...)] → {key: (ok, result)}，并发执行。"""
        results: dict = {}
        if not items:
            return results
        with ThreadPoolExecutor(max_workers=min(4, len(items))) as ex:
            futs = {ex.submit(fn, *args): key for key, *args in items}
            for f in as_completed(futs):
                key = futs[f]
                try:
                    results[key] = (True, f.result())
                except Exception as e:  # 单个参与者失败不影响整体
                    results[key] = (False, e)
        return results

    def _ask(self, p: Participant, system: str, user: str) -> LLMResult:
        """单次模型调用（mock 模式下由 mock 分支处理）。"""
        if p.is_main:
            return LLMResult(error="主引擎不通过 API 调用")
        assert p.spec is not None
        if p.provider == "mock":
            return LLMResult(text="[mock]", provider="mock", model=p.model,
                             usage={"prompt_tokens": 10, "completion_tokens": 10})
        return call_model(self.cfg, p.spec, system, user)

    # ── Mock 各阶段脚本化回复 ──────────────────────────
    def _mock_json(self, p: Participant, stage: str, labels: list[str],
                    idx: int) -> dict:
        if stage == "review":
            scores = {lab: 6 + (i + idx) % 4 for i, lab in enumerate(labels)}
            ranking = sorted(labels, key=lambda x: -scores[x])
            return {"scores": scores, "ranking": ranking,
                    "strengths": {l: f"{l} 的强点" for l in labels},
                    "weaknesses": {l: f"{l} 的弱点" for l in labels},
                    "blind_spots": ["成本评估不足"], "critical_flags": []}
        if stage == "vote":
            endorse = labels[idx % len(labels)]
            return {"vote": "FINALIZE", "endorse": endorse,
                    "confidence": 0.8, "reason": "模拟投票",
                    "critical_blockers": []}
        if stage == "confirm":
            return {"decision": "APPROVE", "reason": "模拟确认", "amendments": []}
        return {}

    # ── 各阶段执行 ─────────────────────────────────────
    def _propose(self, parts: list[Participant], mode: str, question: str,
                 stance_map: dict[str, str], recorder: SessionRecorder) -> dict:
        if recorder.total_cost() >= self.cfg.protocol.budget_usd:
            self.out("⏹ 预算已用尽，跳过提案阶段")
            return {}
        proposals: dict[str, str] = {}
        jobs = []
        for p in parts:
            if p.is_main:
                continue
            system = P.propose_system(mode, p.label, stance_map.get(p.label))
            jobs.append((p.label, p, system, question))
        results = self._parallel(self._ask, [(k, p, s, q) for k, p, s, q in jobs])
        for p in parts:
            if p.is_main:
                continue
            ok, r = results.get(p.label, (False, None))
            if ok and r.ok and r.text:
                proposals[p.label] = r.text
                recorder.add_cost(p.spec, r)
                recorder.save_md("proposals", f"P{p.label}.md",
                                 f"提案 {p.label}（{p.name}）", r.text)
            else:
                err = getattr(r, "error", "无响应") if not ok else (r.error or "空响应")
                self.out(f"  ⚠️ {p.name} 提案失败: {err}")
        return proposals

    def _review(self, parts: list[Participant], proposals: dict[str, str],
                anon: bool, recorder: SessionRecorder) -> tuple[list[dict], dict]:
        if recorder.total_cost() >= self.cfg.protocol.budget_usd:
            self.out("⏹ 预算已用尽，跳过互审阶段")
            return [], {}
        labels = list(proposals.keys())
        reviews: list[dict] = []
        if not labels:
            return reviews, {}
        body = "\n\n".join(
            f"【参与者 {lab} 的提案】\n{proposals[lab][:2500]}" for lab in labels)
        jobs = []
        for p in parts:
            if p.is_main:
                continue
            jobs.append((p.label, p, P.REVIEW_SYSTEM, body))
        results = self._parallel(self._ask, [(k, p, s, q) for k, p, s, q in jobs])
        for p in parts:
            if p.is_main:
                continue
            ok, r = results.get(p.label, (False, None))
            if not ok or not r.ok:
                self.out(f"  ⚠️ {p.name} 互审失败: "
                         f"{getattr(r, 'error', '无响应') if not ok else r.error}")
                continue
            data = extract_json(r.text) if p.provider != "mock" else \
                self._mock_json(p, "review", labels, parts.index(p))
            if not data:
                self.out(f"  ⚠️ {p.name} 互审 JSON 解析失败，跳过")
                continue
            data.setdefault("reviewer", p.label)
            data.setdefault("ranking", [])
            reviews.append(data)
            recorder.add_cost(p.spec, r)
            recorder.save_md("reviews", f"R{p.label}.md",
                             f"互审报告（评审者 {p.label}: {p.name}）", r.text)
        weights = self._weights(parts)
        borda = borda_tally(reviews, weights) if self.cfg.protocol.borda_tiebreak else {}
        return reviews, borda

    def _rebut(self, parts: list[Participant], proposals: dict[str, str],
               reviews: list[dict], recorder: SessionRecorder) -> dict:
        rebuttals: dict[str, str] = {}
        critiques: dict[str, list[str]] = {lab: [] for lab in proposals}
        for r in reviews:
            for lab, text in (r.get("weaknesses") or {}).items():
                if text:
                    critiques.setdefault(lab, []).append(f"{r['reviewer']} 的批评: {text[:500]}")
            for flag in r.get("critical_flags") or []:
                if flag.get("target"):
                    critiques.setdefault(flag["target"], []).append(
                        f"{r['reviewer']} 的致命标记: {flag.get('issue','')[:500]}")
        jobs = []
        for p in parts:
            if p.is_main or p.label not in critiques or not critiques[p.label]:
                continue
            user = "针对你提案的评审意见:\n" + "\n".join(critiques[p.label])
            system = P.REBUT_SYSTEM.replace("{label}", p.label)
            jobs.append((p.label, p, system, user))
        if not jobs:
            return rebuttals
        results = self._parallel(self._ask, [(k, p, s, q) for k, p, s, q in jobs])
        for lab, ok, r in [(k, *v) for k, v in results.items()]:
            if ok and r.ok and r.text:
                rebuttals[lab] = r.text
                recorder.add_cost(next(p.spec for p in parts if p.label == lab), r)
                recorder.save_md("rebuttals", f"RB{lab}.md",
                                 f"反驳（{lab}）", r.text)
        return rebuttals

    def _vote_round(self, parts: list[Participant], proposals: dict[str, str],
                    recorder: SessionRecorder, round_no: int,
                    prior_votes: list[dict]) -> tuple[list[dict], dict]:
        if recorder.total_cost() >= self.cfg.protocol.budget_usd:
            self.out("⏹ 预算已用尽，跳过投票阶段")
            return [], {"support": {}, "total": 0.0, "winner": None,
                        "winner_share": 0.0, "result": "revise"}
        labels = list(proposals.keys())
        body = "\n\n".join(
            f"【参与者 {lab} 的提案】\n{proposals[lab][:1500]}" for lab in labels)
        votes: list[dict] = []
        # 主引擎对自己的提案自动背书（权重计入）
        main = next((p for p in parts if p.is_main), None)
        if main and "M" in proposals:
            votes.append({"model": "M", "vote": "FINALIZE",
                          "endorse": "M", "confidence": 1.0,
                          "reason": "主引擎提案（自动背书）", "critical_blockers": []})
        jobs = []
        for p in parts:
            if p.is_main:
                continue
            jobs.append((p.label, p, P.VOTE_SYSTEM.replace("{label}", p.label), body))
        results = self._parallel(self._ask, [(k, p, s, q) for k, p, s, q in jobs])
        for p in parts:
            if p.is_main:
                continue
            ok, r = results.get(p.label, (False, None))
            if not ok or not r.ok:
                self.out(f"  ⚠️ {p.name} 投票失败: "
                         f"{getattr(r, 'error', '无响应') if not ok else r.error}")
                votes.append({"model": p.label, "vote": "ABSTAIN", "reason": "调用失败"})
                continue
            data = extract_json(r.text) if p.provider != "mock" else \
                self._mock_json(p, "vote", labels, parts.index(p))
            if not data or "vote" not in data:
                self.out(f"  ⚠️ {p.name} 投票 JSON 解析失败，记为弃权")
                votes.append({"model": p.label, "vote": "ABSTAIN", "reason": "解析失败"})
                continue
            data.setdefault("model", p.label)
            votes.append(data)
            recorder.add_cost(p.spec, r)
        recorder.save_json(f"votes/r{round_no}.json",
                           {"round": round_no, "votes": votes})
        tally = weighted_vote_tally(
            votes, self._weights(parts), self.cfg.protocol.pass_threshold)
        return votes, tally

    # ── 主入口 ─────────────────────────────────────────
    def run(self, mode: str, question: str, *, opinion: str | None = None,
            model_ids: list[str] | None = None, chairman_id: str | None = None,
            rounds: int | None = None, stances: list[str] | None = None,
            anon: bool | None = None, target_name: str = "",
            target_content: str = "") -> dict:
        cfg = self.cfg
        anon = cfg.protocol.anonymity if anon is None else anon
        max_rounds = rounds or cfg.protocol.max_rounds
        model_ids = model_ids or cfg.protocol.default_models
        parts = self._build_participants(model_ids, opinion)
        stance_map = {}
        if stances:
            idx = 0
            for p in parts:
                if not p.is_main and idx < len(stances):
                    stance_map[p.label] = stances[idx]
                    idx += 1

        session_id = f"{session_timestamp()}-{slugify(question)}"
        recorder = SessionRecorder(cfg, session_id, mode, question)
        self.out(f"── 协议审议启动 ─────────────────────────────")
        self.out(f"模式: {mode} | 问题: {question[:80]}")
        self.out(f"参与: {', '.join(f'{p.name}(w={p.weight})' for p in parts)}")
        self.out(f"会话: {recorder.root}\n")

        state: dict = {
            "session_id": session_id, "mode": mode, "question": question,
            "participants": [p.as_dict() for p in parts],
            "deadlock": False, "result_summary": "", "sources": [],
        }
        proposals: dict[str, str] = {}

        # ── review / gate 单阶段模式 ──────────────────
        if mode == "review":
            return self._run_review(parts, recorder, state, target_name, target_content)
        if mode == "gate":
            return self._run_gate(parts, recorder, state, target_name, target_content)

        # ── 提案 ──────────────────────────────────────
        self.out("⏳ 阶段 1/5: 各模型独立提案...")
        proposals = self._propose(parts, mode, question, stance_map, recorder)
        if opinion:
            proposals["M"] = opinion
            recorder.save_md("proposals", "PM.md", "提案 M（主引擎）", opinion)
        state["proposals"] = proposals
        if len(proposals) < cfg.protocol.min_models:
            recorder.save_json("state.json", state)
            raise RuntimeError(
                f"有效提案不足（{len(proposals)} < {cfg.protocol.min_models}），中止")
        for lab, text in proposals.items():
            who = "主引擎" if lab == "M" else \
                next(p.name for p in parts if p.label == lab)
            self.out(f"  ✓ 提案 {lab}（{who}）{len(text)} 字")

        # ── 互审 ──────────────────────────────────────
        self.out("\n⏳ 阶段 2/5: 匿名互审 + 排名...")
        reviews, borda = self._review(parts, proposals, anon, recorder)
        if reviews:
            self.out(f"  ✓ {len(reviews)} 份互审报告，Borda: "
                     f"{', '.join(f'{k}={v:.1f}' for k, v in sorted(borda.items(), key=lambda x: -x[1]))}")
        else:
            self.out("  ⚠️ 无有效互审，继续流程")
        state["reviews"] = reviews
        state["borda"] = {k: round(v, 2) for k, v in borda.items()}

        # ── 反驳（brainstorm/research 跳过） ──────────
        rebuttals: dict[str, str] = {}
        if mode in ("debate", "design"):
            self.out("\n⏳ 阶段 3/5: 反驳与修订...")
            rebuttals = self._rebut(parts, proposals, reviews, recorder)
            self.out(f"  ✓ {len(rebuttals)} 份反驳")
        state["rebuttals"] = rebuttals

        # ── 投票（brainstorm/research 跳过） ──────────
        tally = None
        winner = None
        if mode in ("debate", "design"):
            self.out("\n⏳ 阶段 4/5: 加权投票...")
            prior: list[dict] = []
            for rn in range(1, max_rounds + 1):
                votes, tally = self._vote_round(parts, proposals, recorder, rn, prior)
                self.out(f"  第 {rn} 轮: {tally['result']} | "
                         f"支持 {', '.join(f'{k}={v:.2f}' for k, v in tally['support'].items())} "
                         f"| 领先 {tally['winner']} {tally['winner_share']:.0%}")
                if tally["result"] == "finalized":
                    winner = tally["winner"]
                    break
                if tally["result"] == "deadlock" or votes_stalled(prior, votes):
                    state["deadlock"] = True
                    break
                prior = votes
                if rn < max_rounds:
                    self.out("  ↻ 未达共识，进入修订轮...")
                    proposals = self._revise_round(parts, proposals, mode,
                                                   stance_map, recorder)
                    state["proposals"] = proposals
            if not winner and tally:
                # 死锁/未收敛 → 最多票数者胜出，平票用 Borda
                support = tally["support"]
                cands = [k for k, v in support.items() if v == max(support.values())] \
                    if support else list(proposals.keys())
                if len(cands) == 1:
                    winner = cands[0]
                elif borda:
                    winner = max(cands, key=lambda x: borda.get(x, 0))
                else:
                    winner = cands[0]
                state["deadlock"] = True
            state["rounds"] = [{"round": i + 1} for i in range(max_rounds)]
            state.setdefault("winner", winner)

        # ── 主席综合 ──────────────────────────────────
        self.out("\n⏳ 阶段 5/5: 主席综合...")
        synth = self._synthesize(parts, chairman_id, mode, question,
                                 proposals, reviews, rebuttals, tally,
                                 recorder, state)
        answer = synth.get("answer", "")
        state["answer"] = answer
        state.update({k: synth.get(k) for k in
                      ("confidence", "preserved_minority", "open_questions",
                       "rationale", "sources")})
        recorder.save_md("", "synthesis.md", "主席综合报告", json.dumps(
            synth, ensure_ascii=False, indent=2))

        # ── 确认 ──────────────────────────────────────
        confirm = {}
        if mode in ("debate", "design"):
            self.out("⏳ 全体确认...")
            confirm = self._confirm(parts, answer, recorder)
            if confirm.get("accepted"):
                state["result_summary"] = "共识达成（全体加权确认通过）"
            else:
                state["result_summary"] = "确认未通过，回退到最高票提案"
                if winner:
                    state["answer"] = proposals.get(winner, answer)
        elif mode in ("brainstorm", "research"):
            state["result_summary"] = "综合完成"
        state["confirm"] = confirm

        recorder.save_cost()
        state["cost_usd"] = recorder.total_cost()
        human = "待人工确认"
        if sys.stdin and sys.stdin.isatty() and not self.mock:
            human = input("人工决定（回车=待确认 / a=批准 / r=驳回+原因）: ") or "待人工确认"
        final_md = recorder.finalize(state, human)
        self.out(f"\n════ 结果 ════")
        self.out(state["result_summary"])
        self.out(f"答案: {state['answer'][:600]}")
        self.out(f"成本: ${state['cost_usd']} | 报告: {final_md}")
        return state

    # ── 修订轮 ────────────────────────────────────────
    def _revise_round(self, parts, proposals, mode, stance_map, recorder) -> dict:
        new: dict[str, str] = {}
        for p in parts:
            if p.is_main and "M" in proposals:
                new["M"] = proposals["M"]
            if p.is_main or p.label not in proposals:
                continue
            system = P.propose_system(mode, p.label, stance_map.get(p.label))
            user = (f"上一轮你的提案如下。请根据互审批评与辩论进展修订提案，"
                    f"明确写出改动点。\n\n原提案:\n{proposals[p.label][:2000]}")
            r = self._ask(p, system, user)
            if r.ok and r.text:
                new[p.label] = r.text
                recorder.add_cost(p.spec, r)
                recorder.save_md("proposals", f"P{p.label}-r2.md",
                                 f"提案 {p.label} 修订", r.text)
            else:
                new[p.label] = proposals[p.label]
        return new

    # ── 主席综合 ──────────────────────────────────────
    def _synthesize(self, parts, chairman_id, mode, question, proposals,
                    reviews, rebuttals, tally, recorder, state) -> dict:
        if recorder.total_cost() >= self.cfg.protocol.budget_usd:
            self.out("⏹ 预算已用尽，跳过主席综合，回退领先提案")
            first = next(iter(proposals.values()), "")
            return {"answer": first, "confidence": 0.0,
                    "preserved_minority": [], "open_questions": [],
                    "rationale": "预算用尽回退", "sources": []}
        spec = self.cfg.models.get(chairman_id or self.cfg.protocol.default_chairman)
        if not spec or not self.cfg.provider_key(spec.provider) and spec.provider != "mock":
            # 主席不可用 → 用 Borda 最高者/第一个模型
            borda = state.get("borda") or {}
            top = max(borda, key=borda.get) if borda else \
                next((p.label for p in parts if not p.is_main), None)
            spec = next((p.spec for p in parts if p.label == top), None)
            if spec is None:
                spec = next((p.spec for p in parts if not p.is_main), None)
        if spec is None:
            self.out("  ⚠️ 无主席可用，直接采用领先提案")
            first = next(iter(proposals.values()), "")
            return {"answer": first, "confidence": 0.0,
                    "preserved_minority": [], "open_questions": [],
                    "rationale": "主席不可用，回退", "sources": []}

        w = self._weights(parts)
        lines = [f"问题: {question}", ""]
        for lab, text in proposals.items():
            who = "主引擎" if lab == "M" else f"参与者 {lab}"
            weight = w.get(lab, 1.0)
            lines.append(f"【提案 {lab} — {who}（权重 {weight}）】\n{text[:2200]}")
        if reviews:
            lines.append("\n【匿名互审摘要】")
            for r in reviews:
                lines.append(f"- 评审 {r['reviewer']}: 得分 {r.get('scores')}, "
                             f"排名 {r.get('ranking')}, 盲区 {r.get('blind_spots')}")
        if rebuttals:
            lines.append("\n【反驳与修订】")
            for lab, text in rebuttals.items():
                lines.append(f"- {lab}: {text[:800]}")
        if tally:
            lines.append(f"\n【投票】支持: {tally['support']}，"
                         f"总权重: {tally['total']:.1f}，结果: {tally['result']}")
        lines.append("\n请综合决策权重与论证质量输出 JSON。")
        user = "\n".join(lines)
        s = self._maybe_mock(spec)
        if s.provider == "mock":
            return {"answer": next(iter(proposals.values()), ""), "confidence": 0.9,
                    "preserved_minority": [], "open_questions": [],
                    "rationale": "mock 综合", "sources": []}
        r = call_model(self.cfg, s, P.SYNTHESIS_SYSTEM, user)
        if not r.ok:
            self.out(f"  ⚠️ 主席调用失败: {r.error}，回退到领先提案")
            first = next(iter(proposals.values()), "")
            return {"answer": first, "confidence": 0.0,
                    "preserved_minority": [], "open_questions": [],
                    "rationale": f"主席失败: {r.error}", "sources": []}
        data = extract_json(r.text) or {"answer": r.text}
        recorder.add_cost(s, r)
        if mode == "research":
            state["sources"] = r.sources
        return data

    # ── 确认 ──────────────────────────────────────────
    def _confirm(self, parts, answer, recorder) -> dict:
        if recorder.total_cost() >= self.cfg.protocol.budget_usd:
            return {"decisions": [], "accepted": False, "approve_share": 0.0,
                    "reason": "预算用尽未确认"}
        decisions = []
        jobs = []
        for p in parts:
            if p.is_main:
                continue
            jobs.append((p.label, p, P.CONFIRM_SYSTEM.replace("{label}", p.label),
                         f"最终答案:\n{answer[:2500]}"))
        results = self._parallel(self._ask, [(k, p, s, q) for k, p, s, q in jobs])
        for p in parts:
            if p.is_main:
                continue
            ok, r = results.get(p.label, (False, None))
            if not ok or not r.ok:
                decisions.append({"model": p.label, "decision": "REJECT",
                                  "reason": "调用失败"})
                continue
            data = extract_json(r.text) if p.provider != "mock" else \
                self._mock_json(p, "confirm", [], parts.index(p))
            if not data or "decision" not in data:
                decisions.append({"model": p.label, "decision": "REJECT",
                                  "reason": "解析失败"})
                continue
            data.setdefault("model", p.label)
            decisions.append(data)
            recorder.add_cost(p.spec, r)
        w = self._weights(parts)
        total = sum(w.get(d["model"], 1.0) for d in decisions)
        approved = sum(w.get(d["model"], 1.0) for d in decisions
                       if d.get("decision") == "APPROVE")
        accepted = total > 0 and approved / total >= self.cfg.protocol.pass_threshold
        return {"decisions": decisions, "accepted": accepted,
                "approve_share": round(approved / total, 3) if total else 0.0}

    # ── review / gate 单阶段实现 ──────────────────────
    def _run_review(self, parts, recorder, state, target_name, target_content) -> dict:
        if recorder.total_cost() >= self.cfg.protocol.budget_usd:
            state["result_summary"] = "预算用尽，未执行评审"
            recorder.finalize(state)
            return state
        self.out("⏳ 并行代码/文档评审...")
        jobs = []
        for p in parts:
            if p.is_main:
                continue
            jobs.append((p.label, p, P.REVIEW_MODE_SYSTEM,
                         P.review_target_prompt(target_name, target_content)))
        results = self._parallel(self._ask, [(k, p, s, q) for k, p, s, q in jobs])
        raw = []
        for p in parts:
            if p.is_main:
                continue
            ok, r = results.get(p.label, (False, None))
            if not ok or not r.ok:
                self.out(f"  ⚠️ {p.name} 评审失败: "
                         f"{getattr(r, 'error', '无响应') if not ok else r.error}")
                continue
            data = extract_json(r.text) or {"parse_error": True, "raw": r.text[:500]}
            data.setdefault("model", p.label)
            raw.append(data)
            recorder.add_cost(p.spec, r)
            recorder.save_md("reviews", f"R{p.label}.md",
                             f"评审（{p.name}）", r.text)
        merged = merge_reviews(raw, self._weights(parts))
        state.update({"raw_reviews": raw, "merged": merged,
                      "result_summary": f"评审完成: {merged['total_models']} 模型参与，"
                      f"{len(merged['confirmed_issues'])} 确认 / "
                      f"{len(merged['pending_issues'])} 待定"})
        recorder.save_json("merged.json", merged)
        recorder.save_cost()
        state["cost_usd"] = recorder.total_cost()
        final_md = recorder.finalize(state)
        self.out(f"\n════ 评审结果 ════")
        self.out(state["result_summary"])
        self.out(f"报告: {final_md}")
        return state

    def _run_gate(self, parts, recorder, state, target_name, target_content) -> dict:
        if recorder.total_cost() >= self.cfg.protocol.budget_usd:
            state["result_summary"] = "预算用尽，未执行门禁"
            recorder.finalize(state)
            return state
        self.out("⏳ 并行放行判定...")
        jobs = []
        for p in parts:
            if p.is_main:
                continue
            jobs.append((p.label, p, P.GATE_MODE_SYSTEM,
                         P.gate_target_prompt(target_name, target_content)))
        results = self._parallel(self._ask, [(k, p, s, q) for k, p, s, q in jobs])
        verdicts = []
        for p in parts:
            if p.is_main:
                continue
            ok, r = results.get(p.label, (False, None))
            if not ok or not r.ok:
                self.out(f"  ⚠️ {p.name} 判定失败: "
                         f"{getattr(r, 'error', '无响应') if not ok else r.error}")
                continue
            data = extract_json(r.text) or {"parse_error": True, "raw": r.text[:500]}
            data.setdefault("model", p.label)
            verdicts.append(data)
            recorder.add_cost(p.spec, r)
            recorder.save_md("reviews", f"R{p.label}.md",
                             f"门禁判定（{p.name}）", r.text)
        g = gate_verdicts(verdicts, self._weights(parts),
                          self.cfg.protocol.pass_threshold,
                          self.cfg.protocol.veto_enabled,
                          self.cfg.protocol.veto_min_weight)
        state.update({"verdicts": verdicts, "gate": g,
                      "result_summary": f"门禁{g['summary']} "
                      f"(支持 {g['support_share']:.0%} ≥ {g['threshold']:.0%})"})
        recorder.save_json("gate.json", g)
        recorder.save_cost()
        state["cost_usd"] = recorder.total_cost()
        final_md = recorder.finalize(state)
        self.out(f"\n════ 门禁结果 ════")
        self.out(state["result_summary"])
        if g["vetoes"]:
            self.out(f"  否决: {len(g['vetoes'])} 个模型触发 critical blocker")
        self.out(f"报告: {final_md}")
        return state
