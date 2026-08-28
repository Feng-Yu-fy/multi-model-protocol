# 多模型共指导协议 — 主代理调用规范

本目录是「多模型共指导协议」的协议化实现。主引擎（Claude Code / Codex 等）在收到
带犹豫、求证、方案选择、评审、门禁需求时，按本规范调用，并把结果带回对话。

把本文件内容并入主代理的全局指令（如 `~/.codex/AGENTS.md`）即可在所有项目窗口生效。

## 触发规则

| 用户需求特征 | 模式 | 调用 |
| --- | --- | --- |
| 新项目开始：目标/方案/架构梳理 | `debate` | `python council.py debate "<问题>" -o "<主引擎方案>"` |
| 项目中间梳理：方向/方案评审 | `design` | `python council.py design "<需求>" -o "<设计方案>"` |
| 调研/学习/资料核实 | `research` | `python council.py research "<主题>" --models gemini-pro,grok-search,deepseek`（grok-search 联网强） |
| 代码/文档/报告评审 | `review` | `python council.py review <文件路径> --models <阵容>` |
| 放行/合并/开工前的质量门禁 | `gate` | `python council.py gate <文件路径> --models <阵容>` |
| 快速思路发散 | `brainstorm` | `python council.py brainstorm "<问题>"` |

## 执行要点

1. 运行时用 Python 3.11+（纯标准库，无需安装依赖）。
2. 阵容按重要程度自动切换（`--importance`，默认 medium；`--models` 可显式覆盖），
   阵容定义见 `config/protocol.toml` 的 `[lineups]`：
   - low 低耗日常：免费/低价型号为主；
   - medium 标准：日常审议默认档；
   - high 关键决策：权重更高的主力型号。
   调研类优先用带联网搜索的型号（`grok-search` 或 `use_search = true` 的模型）。
   模型不可用时先 `python council.py doctor` 检查 key。
3. `debate` / `design` 必须带 `-o`（主引擎自己的方案），否则主引擎不参与权重。
4. 调完把 `sessions/<id>/final.md` 的「结论 / 保留的少数意见 / 待人工确认事项」
   完整带回对话；**最终决定权始终在用户**，不要替用户拍板。
5. 高成本场景可加 `--budget 0.1` 限制；输出乱码时设置 `$env:PYTHONIOENCODING='utf-8'`。

## 结果处理

- 达成共识 → 直接采用并说明权重支持率；
- 死锁/分歧 → 把各方观点摊开，指出分歧点，请用户裁决；
- 门禁否决 → 列出 critical blocker 与证据，修改后重跑 gate。
