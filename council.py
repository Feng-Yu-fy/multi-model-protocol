#!/usr/bin/env python3
"""多模型共指导协议 — 命令行入口。

用法:
  python council.py debate "问题" --opinion "我的方案" --models gemini-pro,gpt-mini,kimi
  python council.py research "调研主题" --models gemini-pro,deepseek
  python council.py review <文件> --models gemini-flash,gpt-mini,deepseek
  python council.py gate <文件> --models gemini-pro,gpt-mini,deepseek,kimi
  python council.py brainstorm "问题" --mock            # 离线演示
  python council.py doctor                              # 环境自检
  python council.py list / show <session_id>            # 会话管理
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from council import costs
from council.config import Config
from council.engine import CouncilEngine
from council.util import ensure_utf8_io, read_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="council.py",
        description="多模型共指导协议（分权重审议 / 评审 / 门禁）",
    )
    sub = parser.add_subparsers(dest="mode")

    for name, help_text in [
        ("brainstorm", "头脑风暴：独立观点 → 互审 → 主席综合"),
        ("debate", "方案辩论：提案 → 互审 → 反驳 → 加权投票 → 主席综合 → 确认"),
        ("design", "设计审查：与 debate 相同，提示词聚焦工控/架构"),
        ("research", "深度调研：多模型调研 → 互审 → 主席交叉验证（保留来源）"),
        ("review", "评审：多模型并行结构化评审，按权重合并确认/待定"),
        ("gate", "门禁：多模型放行判定，加权通过 + critical 一票否决"),
    ]:
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("target", help="问题/主题（review、gate 为文件路径）")
        sp.add_argument("--opinion", "-o", help="主引擎方案（debate/design 推荐提供）")
        sp.add_argument("--file", "-f", help="从文件读取主引擎方案")
        sp.add_argument("--models", help="参与模型 id，逗号分隔（默认见 protocol.toml）")
        sp.add_argument("--importance", choices=["low", "medium", "high"],
                        help="按重要程度自动选阵容：low=低耗日常 / medium=标准 / high=关键决策")
        sp.add_argument("--chairman", help="主席模型 id（默认 gemini-pro）")
        sp.add_argument("--rounds", type=int, help="投票最大轮数")
        sp.add_argument("--budget", type=float, help="成本上限（USD）")
        sp.add_argument("--stances", help="立场分配，逗号分隔，如 skeptic,advocate,pragmatist")
        sp.add_argument("--no-anon", action="store_true", help="互审不匿名")
        sp.add_argument("--mock", action="store_true", help="离线 mock 演示（不调 API）")
        sp.add_argument("--json", action="store_true", help="输出 JSON 结果")

    sub.add_parser("doctor", help="环境自检：Python/配置/模型/key")
    sub.add_parser("list", help="列出历史协议会话")
    sp_costs = sub.add_parser("costs", help="费用面板：聚合会话成本，--html 生成可视化")
    sp_costs.add_argument("--html", action="store_true",
                          help="生成 costs_dashboard.html")
    sp_costs.add_argument("--json", nargs="?", const="costs_dashboard.json",
                          metavar="PATH", help="导出 JSON 数据源（面板用）")
    sp_costs.add_argument("--open", action="store_true",
                          help="生成后用默认浏览器打开")
    sp_show = sub.add_parser("show", help="查看会话最终报告")
    sp_show.add_argument("session_id")
    return parser


def cmd_doctor(cfg: Config) -> int:
    print(f"Python: {sys.version.split()[0]}")
    print(f"tomllib: {'可用' if sys.version_info >= (3, 11) else '不可用（需 3.11+）'}")
    print(f"配置: {cfg.base_dir}")
    print(f"协议: 阈值 {cfg.protocol.pass_threshold:.0%} | 最大轮数 "
          f"{cfg.protocol.max_rounds} | 预算 ${cfg.protocol.budget_usd}")
    print(f"默认阵容: {', '.join(cfg.protocol.default_models)}")
    print(f"主席默认: {cfg.protocol.default_chairman}")
    print(f"重要度默认: {cfg.protocol.default_importance}")
    for lv in ("low", "medium", "high"):
        lineup = cfg.protocol.lineups.get(lv)
        if lineup:
            budget = cfg.protocol.importance_budgets.get(lv, 0)
            print(f"  {lv:<6} 阵容: {', '.join(lineup)} | 预算 ${budget}")
    print("\n模型注册:")
    for mid, spec in cfg.models.items():
        if spec.provider in ("mock", "gemini-cli"):
            key = "无需"
        else:
            key = "✓" if cfg.provider_key(spec.provider) else "✗"
        print(f"  {mid:<18} {spec.name:<20} {spec.provider:<10} "
              f"权重 {spec.weight:<4} key {key}")
    print(f"\n会话目录: {cfg.protocol.session_dir}")
    return 0


def cmd_list(cfg: Config) -> int:
    root = Path(cfg.protocol.session_dir)
    if not root.exists():
        print("（暂无会话）")
        return 0
    for d in sorted(root.iterdir(), reverse=True):
        if d.is_dir():
            state = d / "state.json"
            if state.exists():
                s = json.loads(state.read_text(encoding="utf-8"))
                print(f"{d.name}  [{s.get('mode')}] {s.get('question','')[:60]}")
    return 0


def cmd_show(cfg: Config, session_id: str) -> int:
    final = Path(cfg.protocol.session_dir) / session_id / "final.md"
    if not final.exists():
        print(f"❌ 会话不存在: {session_id}")
        return 1
    print(read_text(final))
    return 0


def cmd_costs(cfg: Config, args) -> int:
    root = Path(cfg.protocol.session_dir)
    rows = costs.load_sessions(root)
    models = costs.aggregate(rows)
    print(costs.console_report(rows, models))
    if args.json is not None:
        out = costs.write_json_report(cfg, root, Path(args.json))
        print(f"\n数据源已生成: {out}")
    if args.html or args.open:
        out = costs.write_dashboard(cfg, root)
        print(f"\n面板已生成: {out}")
        if args.open:
            try:
                import os
                os.startfile(str(out))
            except Exception:
                import subprocess
                subprocess.Popen(["cmd", "/c", "start", "", str(out)])
    return 0


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_io()
    args = build_parser().parse_args(argv)
    base = Path(__file__).resolve().parent
    cfg = Config(base)

    if args.mode == "doctor":
        return cmd_doctor(cfg)
    if args.mode == "list":
        return cmd_list(cfg)
    if args.mode == "show":
        return cmd_show(cfg, args.session_id)
    if args.mode == "costs":
        return cmd_costs(cfg, args)
    if not args.mode:
        build_parser().print_help()
        return 0

    opinion = args.opinion or ""
    if args.file:
        fp = Path(args.file)
        if fp.exists():
            opinion = read_text(fp)
        else:
            print(f"❌ 方案文件不存在: {args.file}")
            return 1

    target_name, target_content = "", ""
    if args.mode in ("review", "gate"):
        tp = Path(args.target)
        if not tp.exists():
            print(f"❌ 目标文件不存在: {args.target}")
            return 1
        target_name, target_content = tp.name, read_text(tp)
        question = f"{args.mode} {tp.name}"
    else:
        question = args.target

    importance = args.importance or cfg.protocol.default_importance
    if args.models:
        model_ids = [m.strip() for m in args.models.split(",")]
    else:
        model_ids = (cfg.protocol.lineups.get(importance)
                     or cfg.protocol.default_models)
    stances = [s.strip() for s in args.stances.split(",")] if args.stances else None
    engine = CouncilEngine(cfg, mock=args.mock)
    if args.budget:
        cfg.protocol.budget_usd = args.budget
    elif importance in cfg.protocol.importance_budgets:
        cfg.protocol.budget_usd = cfg.protocol.importance_budgets[importance]
    try:
        state = engine.run(
            args.mode, question,
            opinion=opinion or None,
            model_ids=model_ids,
            chairman_id=args.chairman,
            rounds=args.rounds,
            stances=stances,
            anon=not args.no_anon,
            target_name=target_name,
            target_content=target_content,
        )
    except RuntimeError as e:
        print(f"❌ {e}")
        return 1
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
