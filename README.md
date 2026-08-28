# 多模型共指导协议（Multi-Model Co-Guidance Protocol）

把「主引擎 + 多个外部模型」的协作从临时脚本规整成一套**配置驱动、分权重、可审计**的协议。

主引擎（Claude Code / Codex 等编码代理）先给出方案，协议再召集 Gemini、GPT、DeepSeek、Kimi、
Grok、GLM、Qwen 等模型独立提案 → 匿名互审 → 反驳 → 加权投票 → 主席综合 → 全体确认，
每一步都落盘到 `sessions/<id>/`，最终由你（人）拍板。

> 设计思路来源于 GitHub 同类项目（karpathy/llm-council、ensemble、GodModeSkill、ringi 等），
> 详细调研与借鉴点见 [docs/RESEARCH.md](docs/RESEARCH.md)。

## 为什么值得用

- **防附和**：互审阶段模型只看到「参与者 A/B/C」，不知道对方是谁；提示词强制独立判断。
- **真加权**：投票、评审合并、门禁全部按模型权重算，不是一人一票。主引擎权重默认 1.2。
- **有刹车**：`critical` 问题一票否决（gate 模式）、轮数上限、成本预算、超时降级。
- **可追溯**：提案/互审/反驳/投票/综合/确认全部生成 Markdown + JSON 档案。
- **零依赖**：纯标准库（Python 3.11+），任何运行时直接跑，不需要 pip install。

## 快速开始

```powershell
# 环境自检（看哪些模型 key 可用）
python council.py doctor

# 离线演示（不调用任何 API，走 mock 模型）
python council.py debate "S7-1200 与 CODESYS 选型" --opinion "选 S7-1200" --mock

# 真实调用：方案辩论（主引擎方案 + 三模型审议）
python council.py debate "PLC 程序架构怎么设计" -o "用 FB 封装" --models gemini-pro,gpt-mini,kimi

# 按重要程度自动选阵容（不传 --models 时生效）
python council.py debate "日常小问题" -o "方案" --importance low     # 低耗：GLM-4.5-Air
python council.py debate "关键架构决策" -o "方案" --importance high   # 关键：GLM-5.2 + 订阅Gemini

# 深度调研（Gemini 带 Google 搜索，结果保留来源）
python council.py research "Modbus TCP 与 Profinet 实时性对比" --models gemini-pro,deepseek

# 深度调研（Grok 联网搜索变体，强制先搜再答）
python council.py research "2026 年主流 PLC 安全标准" --models grok-search,gemini-pro,deepseek

# 代码评审（多模型并行审查，按权重合并确认/待定）
python council.py review .\st_程序.scl --models gemini-pro,gpt-mini,deepseek,kimi

# 质量门禁（放行/否决，critical 一票否决）
python council.py gate .\改造方案.md --models gemini-pro,gpt-mini,deepseek,kimi

# 会话管理
python council.py list
python council.py show <session_id>

# 费用面板（聚合所有会话成本）
python council.py costs                 # 控制台表格
python council.py costs --html --open   # 生成并打开 costs_dashboard.html
```

需要 Python 3.11+（只依赖标准库，无需 pip install）。Windows 终端输出乱码时
设置 `$env:PYTHONIOENCODING='utf-8'`；需要代理访问境外 API 时设置 `COUNCIL_PROXY=http://127.0.0.1:<端口>`。

## 六种模式

| 模式 | 流程 | 典型场景 |
| --- | --- | --- |
| `brainstorm` | 提案 → 互审 → 主席综合 | 思路发散、方案候选收集 |
| `debate` | 提案 → 互审 → 反驳 → 加权投票(≤N轮) → 主席综合 → 确认 | 方案决策、是否/怎么做 |
| `design` | 同 debate，提示词聚焦完整性/边界/工控落地 | 架构与设计审查 |
| `research` | 提案(可联网) → 互审 → 主席交叉验证 | 调研、技术对比、资料核实 |
| `review` | 并行结构化评审 → 按权重合并 | 代码/文档/实验报告审查 |
| `gate` | 并行放行判定 → 加权门禁 + critical 否决 | 合并前/发布前/开工前检查 |

## 分权重机制

权重定义在 [config/models.toml](config/models.toml)，协议参数在
[config/protocol.toml](config/protocol.toml)：

- 每个模型 `weight` 参与所有加权计算；
- 投票通过条件：`支持某提案的模型权重和 / 参与总权重 ≥ pass_threshold`（默认 60%）；
- 互审 Borda 排名按评审者权重加权，仅在死锁平票时裁决；
- 评审合并：某问题被发现的模型权重占比 ≥ 50% → 确认问题，否则 → 待人工确认；
- 门禁：支持权重 ≥ 60% 且无 critical 否决 → 放行；
- 主引擎方案自动获得 `main_engine_weight`（默认 1.2）的权重并自动背书自己的提案。

## 多模型接入

协议层统一封装了供应商，**新模型只需在 `models.toml` 加一段配置**：

- **Gemini**：`provider = "gemini"`（REST 直连，支持 Google 搜索 grounding）
- **Gemini 订阅（免卡）**：`provider = "gemini-cli"` — 调用本机已登录的
  Antigravity CLI（`agy`，Google AI Pro 订阅直连），无需 API key；
  首次运行 `agy` 完成 Google 登录即可
- **GPT**：`provider = "openai"`
- **DeepSeek**：`provider = "deepseek"`
- **Kimi（Moonshot）**：`provider = "moonshot"` — 在 `config/keys.local.json` 填
  `{"moonshot": "sk-..."}`（模板见 `keys.local.example.json`）
- **国内免卡备选**：智谱 GLM（`zhipu-glm`）、阿里百炼 Qwen（`qwen`）与 Kimi
  都是 OpenAI 兼容平台，手机号注册即送免费额度、无需外币卡；在
  `models.toml` 配 `base_url`，key 写 `config/keys.local.json`
  （如 `{"zhipu": "..."}`、`{"dashscope": "..."}`）
- **Grok（xAI）**：`provider = "xai"` — `grok` 普通版 / `grok-search` 联网版
  （内置 web_search 工具，research 模式强制先搜再答）；key 用 `XAI_API_KEY`
  或 `config/keys.local.json` 的 `{"xai": "xai-..."}`
- **OpenRouter 全家桶**：`provider = "openrouter"`，一个 key 接 Claude/GPT/Kimi/Qwen 等
- **任意 OpenAI 兼容中转站**：`provider = "自定义id"` + `base_url = "https://..."`，
  key 写进 `config/keys.local.json`（如 `{"relay": "sk-..."}`）；OpenAI 兼容即可，
  与官方通道共用同一套 fallback 逻辑

key 解析顺序：环境变量 → `config/keys.local.json`（已 gitignore，从
`keys.local.example.json` 复制改名）→ 可选的旧工具目录（`protocol.toml` 的
`keys.legacy_dir`，默认关闭）。密钥不会写入会话记录。

## 接入 Claude Code / Codex

主代理按 [AGENTS.md](AGENTS.md) 的触发规则调用协议。要点：

1. 用户表达犹豫/求证/多方案选择 → `debate` 或 `design`，带上自己的方案；
2. 用户要求调研 → `research`；
3. 代码/文档评审 → `review`；放行决策 → `gate`；
4. 把 `final.md` 的结论、少数意见、待确认事项带回对话，最终由用户决定。

在常用项目里把 AGENTS.md 的触发规则复制进全局指令（如 `~/.codex/AGENTS.md`），
即可让所有项目窗口都能触发参议层。

## 目录结构

```
├── council.py           # CLI 入口
├── council/             # 协议核心（纯标准库）
│   ├── config.py        #   配置 + key 解析
│   ├── providers.py     #   供应商客户端（Gemini REST / OpenAI 兼容 / mock）
│   ├── prompts.py       #   各阶段提示词（匿名/防附和）
│   ├── tally.py         #   加权投票 / Borda / 门禁 / 评审合并
│   ├── engine.py        #   审议流程编排
│   └── record.py        #   会话档案
├── config/              # 模型注册表 + 协议参数 + 本地 key
├── tests/               # 单元/端到端测试（离线）
├── docs/RESEARCH.md     # GitHub 调研报告
└── AGENTS.md            # 主代理调用规范
```

## 测试

```powershell
python -m unittest discover -s tests -v
```

## 已知边界与迭代路线

- 模型 ids 是 2026 年 8 月可用型号，模型更新后改 `models.toml` 即可；
- 价格为估算值，仅用于成本统计；
- 默认阵容/分档针对「国内免卡 + 低价订阅」场景配置，按自己的 key 情况改
  `protocol.toml` 的 `lineups` 即可；配额不足时把 `min_models` 临时调为 1；
- 下一步候选：真实场景评测集（对比单人 vs 协议输出质量）、Claude Code `/council`
  命令封装、评审问题去重的语义化（当前按标题模糊匹配）、会话恢复（`resume`）。

## License

[MIT](LICENSE)
