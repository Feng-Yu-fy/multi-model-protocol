# GitHub 调研报告 — 多模型协作/审议类项目

调研时间：2026-08-01。目标：为「Gemini 分权重协议机制」的协议化迭代找可借鉴的
架构、算法与工程实践。

## 一、代表性项目

### 1. karpathy/llm-council（概念源头，22.9k stars）
`https://github.com/karpathy/llm-council`

三阶段：多个模型独立作答 → 匿名互审并排名 → 主席模型综合。
核心启发：
- **匿名互审**是防品牌附和的关键，只给「Assessment A/B/C」标签；
- **主席综合**解决分歧，且输出保持与第一阶段相同的结构化 schema；
- 通过 OpenRouter 一个 key 接多个供应商。

### 2. raiyanyahya/ensemble（多轮收敛 + 文件系统审计）
`https://github.com/raiyanyahya/ensemble`

把 llm-council 扩展成真正的辩论：`提案 → 互审 → 反驳 → 投票 → 综合 → 确认`，
每步都是磁盘上的 Markdown 文件，可恢复可审计。
核心启发：
- **投票语义**：FINALIZE（背书某提案）/ REVISE（再来一轮）/ SPLIT（根本分歧），
  多数背书同一提案才算共识，固定轮数不算数；
- **死锁检测**：连续两轮投票完全一致 → 停止移动 → 最佳努力答案；
- **Borda 排名**只用于平票裁决，永远不覆盖真多数；
- **综合后仍需全体确认**（APPROVE/REJECT），失败回退到原样胜出提案——最坏情况不比旧行为差；
- **成本/预算**：按模型统计 token 与美元，硬预算上限；
- **角色/立场**：`--roles diverse`（skeptic/advocate/pragmatist）防群体思维。

### 3. 99xAgency/GodModeSkill（Claude Code 多模型门禁）
`https://github.com/99xAgency/GodModeSkill`

`/work` 一个命令让 Codex + Gemini + OpenCode(Kimi/DeepSeek) 三系谱并行评审，
quorum 全过才放行，失败自动修订重试。
核心启发：
- **谱系多样性**（lineage diversity）：同族模型互相审是回声室，跨族才有价值；
- **quorum 判定**：agree/partial 规则 + 关键问题拦截；provider 挂了自动换模型/降级；
- **证据自检**：critical 发现必须给文件+行号+引用原文，找不到引用即标记为疑似幻觉；
- 人类确认环节（merge checklist）不能省。

### 4. flonat/council-api（OpenRouter 三阶段议会）
`https://github.com/flonat/council-api`

三阶段（独立评估 → 匿名互审排名 → 主席综合），支持 OpenRouter/OpenAI/Anthropic/
Gemini/Mistral 混合路由，`existing_result` 可复用已有单模型结果。
核心启发：
- 供应商抽象统一成 OpenAI 兼容端点，接入成本极低；
- 主席失败自动回退到排名第一的评估——保证最坏情况可用；
- 默认配置持久化（`~/.config/`），CLI 与库共用。

### 5. tacticaldoll/ringi（人工拍板 + 持久档案）
`https://github.com/tacticaldoll/ringi`

稟議流程：draft → submit → answer → arbitrate → decide → archive。
核心启发：
- 仲裁者只输出离散的、有来源绑定的 Move（新增异议/关闭风险/提问），不越权重写方案；
- 档案（dossier）是人可读、带完整性摘要的最终记录，**只记录决策，不授予执行权**；
- 最终决定由人记录——AI 管到「就绪」为止。

### 6. 其他值得参考

| 项目 | 链接 | 借鉴点 |
| --- | --- | --- |
| magi-ai/opencode-magi | github.com/magi-ai/opencode-magi | 多评审者多数决 + 视角分工（general/security/compat）+ 修复后复审 |
| charlieyou/cerberus | github.com/charlieyou/cerberus | Codex+Gemini+Claude 三头评审质量门 |
| wdnmd1265/Audison | github.com/wdnmd1265/Audison | 双脑仲裁：一个审、一个从 5 个对抗视角攻击、第三个交叉验证；共识=经得起攻击 |
| focuslead/ai-council-framework | github.com/focuslead/ai-council-framework | 并行咨询→结构辩论→共识综合的方法论文档 |
| mcp-debat | github.com/sprindigo-art/mcp-debat | 6 模型顺序辩论 + 反谄媚强制 + 证据验证 |
| SafeRL-Lab/Agent-Scaling | github.com/SafeRL-Lab/Agent-Scaling | 异构模型 + 辩论/投票的实验框架 |

## 二、现有机制与业界方案的差距

现有 `gemini_team.py`（5 模式）+ `multi_review.py`（双模型投票）：

| 维度 | 现有 | 业界成熟做法 | 本次落地 |
| --- | --- | --- | --- |
| 模型接入 | Gemini 专用 + 硬编码 GPT | OpenAI 兼容统一抽象 | `providers.py` 统一 + OpenRouter |
| 权重 | 无（≥2 票即确认） | 权重化投票/门禁 | `tally.py` 全链路加权 |
| 互审 | 无 | 匿名互审 + Borda | 内置匿名 + 加权 Borda |
| 反驳 | debate 单次反驳 | 提案→互审→反驳→投票循环 | 多轮收敛 + 死锁检测 |
| 综合 | 无/单模型合并 | 主席综合 + 全体确认 | 主席综合 + 确认回退 |
| 档案 | 仅 stdout | 全程落盘可审计 | `record.py` 会话档案 |
| 防失控 | 无 | 轮数/预算/超时/降级 | 全部内置 |
| 防幻觉 | 无 | 证据/行号自检 | gate/review 要求证据，合并时降级 |

## 三、设计决策记录

1. **纯标准库实现**：本机 Python 环境混乱（无 requests/openai 包），REST 直连零依赖，
   任何 3.11+ 运行时可跑。
2. **TOML 配置驱动**：模型、权重、阈值、阵容全部配置化，`doctor` 可查。
3. **主引擎参与权重**：保留「Codex/Claude 做主引擎」原则，主引擎提案权重默认 1.2，
   并自动背书自己的提案（明示记录，避免暗中偏袒）。
4. **确认回退**：主席综合后全体确认，不通过则回退到最高票提案原文（ensemble 的最坏情况保证）。
5. **人最终拍板**：final.md 强制留「人工决定」字段；CLI 交互时提示输入。
6. **评审去重从简**：v1 用标题+类别模糊匹配（继承 multi_review），语义去重列入迭代路线。
