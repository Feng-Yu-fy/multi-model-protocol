"""协议核心算法 + mock 端到端测试（无需网络、无需 key）。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from council.config import Config
from council.engine import CouncilEngine
from council import providers as P
from council.tally import (borda_tally, gate_verdicts, merge_reviews,
                           votes_stalled, weighted_vote_tally)
from council.util import extract_json


ROOT = Path(__file__).resolve().parent.parent


def make_cfg(tmp: str) -> Config:
    cfg = Config(ROOT)
    cfg.protocol.session_dir = str(Path(tmp) / "sessions")
    return cfg


class TestTally(unittest.TestCase):
    def test_weighted_finalize(self):
        votes = [
            {"model": "A", "vote": "FINALIZE", "endorse": "P1"},
            {"model": "B", "vote": "FINALIZE", "endorse": "P1"},
            {"model": "C", "vote": "FINALIZE", "endorse": "P2"},
        ]
        weights = {"A": 1.2, "B": 1.0, "C": 1.0}
        t = weighted_vote_tally(votes, weights, 0.6)
        self.assertEqual(t["winner"], "P1")
        self.assertAlmostEqual(t["winner_share"], 2.2 / 3.2)
        self.assertEqual(t["result"], "finalized")

    def test_weighted_not_enough(self):
        votes = [
            {"model": "A", "vote": "FINALIZE", "endorse": "P1"},
            {"model": "B", "vote": "FINALIZE", "endorse": "P2"},
            {"model": "C", "vote": "FINALIZE", "endorse": "P2"},
        ]
        weights = {"A": 1.5, "B": 1.0, "C": 1.0}
        # P2 权重 2.0/3.5 < 0.6 → 需再议
        t = weighted_vote_tally(votes, weights, 0.6)
        self.assertEqual(t["result"], "revise")

    def test_deadlock_no_finalize(self):
        votes = [
            {"model": "A", "vote": "SPLIT", "endorse": None},
            {"model": "B", "vote": "SPLIT", "endorse": None},
        ]
        t = weighted_vote_tally(votes, {"A": 1, "B": 1}, 0.6)
        self.assertEqual(t["result"], "deadlock")

    def test_stall_detection(self):
        v1 = [{"model": "A", "vote": "REVISE", "endorse": None}]
        v2 = [{"model": "A", "vote": "REVISE", "endorse": None}]
        self.assertTrue(votes_stalled(v1, v2))
        self.assertFalse(votes_stalled([], v2))

    def test_borda(self):
        rankings = [
            {"reviewer": "A", "ranking": ["P1", "P2", "P3"]},
            {"reviewer": "B", "ranking": ["P2", "P1", "P3"]},
        ]
        scores = borda_tally(rankings, {"A": 1.0, "B": 1.0})
        self.assertEqual(scores["P1"], 5)
        self.assertEqual(scores["P2"], 5)
        self.assertEqual(scores["P3"], 2)

    def test_gate_veto(self):
        verdicts = [
            {"model": "A", "verdict": "agree", "critical_blockers": []},
            {"model": "B", "verdict": "disagree",
             "critical_blockers": [{"issue": "x", "evidence": "y"}]},
        ]
        g = gate_verdicts(verdicts, {"A": 1.0, "B": 1.0}, 0.6, True, 1.0)
        self.assertFalse(g["passed"])
        self.assertEqual(len(g["vetoes"]), 1)

    def test_merge_reviews_weighted(self):
        results = [
            {"model": "A", "issues": [
                {"severity": "critical", "title": "缓冲区溢出", "category": "security"}]},
            {"model": "B", "issues": [
                {"severity": "critical", "title": "缓冲区溢出", "category": "security"}]},
            {"model": "C", "issues": [
                {"severity": "minor", "title": "命名不规范", "category": "style"}]},
        ]
        merged = merge_reviews(results, {"A": 1.2, "B": 1.0, "C": 1.0}, 0.5)
        self.assertEqual(len(merged["confirmed_issues"]), 1)
        self.assertEqual(len(merged["pending_issues"]), 1)
        self.assertGreater(merged["confirmed_issues"][0]["confidence"], 0.5)

    def test_extract_json(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('前缀 {"a": 1} 后缀'), {"a": 1})
        self.assertIsNone(extract_json("没有 JSON"))


class TestConfig(unittest.TestCase):
    def test_load(self):
        cfg = Config(ROOT)
        self.assertIn("gemini-pro", cfg.models)
        self.assertIn("kimi", cfg.models)
        self.assertIn("grok", cfg.models)
        self.assertIn("grok-search", cfg.models)
        self.assertTrue(cfg.models["grok-search"].use_search)
        self.assertGreater(cfg.protocol.pass_threshold, 0)
        self.assertGreater(cfg.models["gemini-pro"].weight,
                           cfg.models["gpt-mini"].weight)

    def test_mock_mode_no_key_required(self):
        cfg = Config(ROOT)
        specs, warns = cfg.check_models(["gemini-pro", "gpt-mini"],
                                        require_key=False)
        self.assertEqual(len(specs), 2)
        self.assertEqual(warns, [])


class TestXaiProvider(unittest.TestCase):
    def test_web_search_tool_in_request(self):
        cfg = Config(ROOT)
        cfg.provider_key = lambda provider: "test-key"  # 实例级覆盖，无需真 key
        captured: dict = {}
        orig = P._post_json

        def fake_post(url, headers, body, timeout):
            captured["url"] = url
            captured["body"] = body
            return 200, {"choices": [{"message": {"content": "ok"}}],
                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

        P._post_json = fake_post
        try:
            r = P.call_model(cfg, cfg.models["grok-search"],
                             "system", "问题", use_search=True)
            self.assertTrue(r.ok, r.error)
            self.assertEqual(captured["body"]["tools"], [{"type": "web_search"}])
            self.assertEqual(captured["body"]["tool_choice"], "required")
            self.assertTrue(captured["url"].startswith("https://api.x.ai/v1"))
            # 普通 grok 不带搜索工具
            P.call_model(cfg, cfg.models["grok"], "system", "问题")
            self.assertNotIn("tools", captured["body"])
        finally:
            P._post_json = orig


class TestEngineMock(unittest.TestCase):
    def _run(self, mode: str, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            engine = CouncilEngine(cfg, mock=True, out=lambda *a, **k: None)
            state = engine.run(
                mode, "测试问题", opinion="主引擎方案",
                model_ids=["gemini-flash", "gpt-mini", "deepseek"], **kw)
            self.assertTrue(state.get("answer"))
            self.assertIn("final.md", [p.name for p in Path(cfg.protocol.session_dir).rglob("*")])
            return state, tmp

    def test_debate_full_flow(self):
        state, _ = self._run("debate")
        self.assertIn(state["result_summary"], ("共识达成（全体加权确认通过）",
                                                "确认未通过，回退到最高票提案"))
        self.assertEqual(len(state["participants"]), 4)  # 主引擎 + 3
        self.assertIn("M", state["proposals"])
        self.assertIn("cost_usd", state)

    def test_brainstorm_flow(self):
        state, _ = self._run("brainstorm")
        self.assertEqual(state["result_summary"], "综合完成")

    def test_review_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            engine = CouncilEngine(cfg, mock=True, out=lambda *a, **k: None)
            state = engine.run(
                "review", "review demo.py",
                model_ids=["gemini-flash", "gpt-mini"],
                target_name="demo.py", target_content="def f(x): return x\n")
            self.assertIn("merged", state)
            self.assertIn("confirmed_issues", state["merged"])

    def test_gate_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            engine = CouncilEngine(cfg, mock=True, out=lambda *a, **k: None)
            state = engine.run(
                "gate", "gate demo.py",
                model_ids=["gemini-flash", "gpt-mini", "deepseek"],
                target_name="demo.py", target_content="print('hi')\n")
            self.assertIn("gate", state)
            self.assertIn("passed", state["gate"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
