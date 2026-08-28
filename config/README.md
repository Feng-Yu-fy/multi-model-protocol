# 配置说明

## models.toml — 模型注册表

每个模型一个 `[models.<id>]` 段落：

| 字段 | 含义 |
| --- | --- |
| `name` | 展示名 |
| `provider` | `gemini` / `openai` / `deepseek` / `moonshot` / `openrouter` / `xai` / `mock` |
| `model` | 上游模型 id |
| `weight` | 决策权重（投票、评审、Borda 都按它加权） |
| `roles` | 可用角色：`councillor`（参与者）/ `chairman`（主席） |
| `fallbacks` | 上游调用失败时依次尝试的模型 |
| `use_search` | Gemini 启用 Google 搜索 grounding；Grok（xai）启用内置 web_search 工具，强制先搜再答（research 模式利器） |
| `price_in/out` | 估算单价 USD/百万 token，仅统计成本 |

## protocol.toml — 协议参数

- `general.default_models`：未指定 `--models` 时的参与阵容
- `general.main_engine_weight`：主引擎提案权重（Claude Code/Codex 提供的方案）
- `quorum.pass_threshold`：加权通过阈值。例：0.6 表示支持权重占总权重 60% 才算共识
- `quorum.veto_enabled`：任何权重达标模型报 critical blocker 时直接否决
- `limits.max_rounds` / `budget_usd`：防失控保险丝

## API Key 解析顺序

1. 环境变量：`GEMINI_API_KEY` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` / `MOONSHOT_API_KEY` / `OPENROUTER_API_KEY` / `XAI_API_KEY`
2. `config/keys.local.json`（已 gitignore，复制 `keys.local.example.json` 改名填写）
3. 旧工具目录（`protocol.toml` 的 `keys.legacy_dir`）下的 `.gemini_key` / `.openai_key`
4. DeepSeek 额外兜底：`~/.claude/settings.json` 的 `ANTHROPIC_AUTH_TOKEN`

密钥不会写入会话记录。
