"""配置加载：模型注册表（config/models.toml）+ 协议参数（config/protocol.toml）。

纯标准库：Python 3.11+ 使用 tomllib；更早版本给出友好报错。
API Key 解析顺序：环境变量 > config/keys.local.json > 旧工具目录 .key 文件。
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    tomllib = None


@dataclass
class ModelSpec:
    """一个可用模型在协议中的注册信息。"""

    id: str                      # 配置里的短 id，如 gemini-pro
    name: str                    # 展示名
    provider: str                # gemini | openai | deepseek | moonshot | openrouter | mock
    model: str                   # 上游模型 id
    weight: float = 1.0          # 决策权重（分权重机制核心）
    roles: list[str] = field(default_factory=lambda: ["councillor"])
    fallbacks: list[str] = field(default_factory=list)
    base_url: str = ""               # 自定义 OpenAI 兼容端点（中转站），优先级高于默认
    temperature: float = 0.4
    max_tokens: int = 4096
    price_in: float = 0.0        # USD / 1M tokens（估算，用于成本统计）
    price_out: float = 0.0
    use_search: bool = False     # 仅 gemini provider 生效（Google 搜索 grounding）


@dataclass
class ProtocolConfig:
    session_dir: str = "sessions"
    default_mode: str = "debate"
    default_models: list[str] = field(default_factory=lambda: ["gemini-flash", "gpt-mini", "deepseek"])
    default_chairman: str = "gemini-pro"
    default_importance: str = "medium"
    lineups: dict[str, list[str]] = field(default_factory=dict)
    importance_budgets: dict[str, float] = field(default_factory=dict)
    main_engine_weight: float = 1.2      # 主引擎（Claude Code/Codex）提案权重
    min_models: int = 2                  # 最少有效参与模型数
    pass_threshold: float = 0.6          # 加权通过阈值（支持权重 / 总权重）
    veto_enabled: bool = True            # 关键阻塞（critical）一票否决
    veto_min_weight: float = 1.0         # 权重达到该值的模型可触发否决
    borda_tiebreak: bool = True
    max_rounds: int = 3
    request_timeout: int = 120
    budget_usd: float = 0.5
    anonymity: bool = True               # 互审阶段匿名（防品牌附和）
    legacy_key_dir: str = ""             # 旧工具目录（读 .gemini_key / .openai_key）
    mock: bool = False


def _toml_load(path: Path) -> dict:
    if tomllib is None:
        raise RuntimeError("需要 Python 3.11+（tomllib）读取 TOML 配置")
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_models(path: Path) -> dict[str, ModelSpec]:
    """加载 config/models.toml → {id: ModelSpec}"""
    data = _toml_load(path)
    models: dict[str, ModelSpec] = {}
    for mid, raw in data.get("models", {}).items():
        models[mid] = ModelSpec(
            id=mid,
            name=raw.get("name", mid),
            provider=raw.get("provider", "openai"),
            model=raw.get("model", mid),
            weight=float(raw.get("weight", 1.0)),
            roles=list(raw.get("roles", ["councillor"])),
            fallbacks=list(raw.get("fallbacks", [])),
            base_url=raw.get("base_url", ""),
            temperature=float(raw.get("temperature", 0.4)),
            max_tokens=int(raw.get("max_tokens", 4096)),
            price_in=float(raw.get("price_in", 0.0)),
            price_out=float(raw.get("price_out", 0.0)),
            use_search=bool(raw.get("use_search", False)),
        )
    return models


def load_protocol(path: Path) -> ProtocolConfig:
    data = _toml_load(path)
    general = data.get("general", {})
    lineups = data.get("lineups", {})
    importance = data.get("importance", {})
    quorum = data.get("quorum", {})
    limits = data.get("limits", {})
    keys = data.get("keys", {})
    cfg = ProtocolConfig(
        session_dir=general.get("session_dir", "sessions"),
        default_mode=general.get("default_mode", "debate"),
        default_models=list(general.get("default_models", ["gemini-flash", "gpt-mini", "deepseek"])),
        default_chairman=general.get("default_chairman", "gemini-pro"),
        default_importance=general.get("default_importance", "medium"),
        lineups={k: list(v) for k, v in lineups.items()},
        importance_budgets={k: float(v) for k, v in importance.get("budgets", {}).items()},
        main_engine_weight=float(general.get("main_engine_weight", 1.2)),
        min_models=int(quorum.get("min_models", 2)),
        pass_threshold=float(quorum.get("pass_threshold", 0.6)),
        veto_enabled=bool(quorum.get("veto_enabled", True)),
        veto_min_weight=float(quorum.get("veto_min_weight", 1.0)),
        borda_tiebreak=bool(quorum.get("borda_tiebreak", True)),
        max_rounds=int(limits.get("max_rounds", 3)),
        request_timeout=int(limits.get("request_timeout", 120)),
        budget_usd=float(limits.get("budget_usd", 0.5)),
        anonymity=bool(general.get("anonymity", True)),
        legacy_key_dir=keys.get("legacy_dir", ""),
    )
    return cfg


class Config:
    """聚合配置，负责 key 解析。"""

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.models = load_models(self.base_dir / "config" / "models.toml")
        self.protocol = load_protocol(self.base_dir / "config" / "protocol.toml")
        if not os.path.isabs(self.protocol.session_dir):
            self.protocol.session_dir = str(
                self.base_dir / self.protocol.session_dir)
        self._local_keys: dict[str, str] = {}
        lk = self.base_dir / "config" / "keys.local.json"
        if lk.exists():
            try:
                self._local_keys = json.loads(lk.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._local_keys = {}

    # ── Key 解析 ────────────────────────────────────────
    def _legacy_key(self, filename: str) -> str:
        d = self.protocol.legacy_key_dir
        if not d:
            return ""
        f = Path(d) / filename
        return f.read_text(encoding="utf-8", errors="replace").strip() if f.exists() else ""

    def provider_key(self, provider: str) -> str:
        """按供应商解析 API key：环境变量 > keys.local.json > 旧工具目录。"""
        mapping = {
            "gemini": ("GEMINI_API_KEY", ".gemini_key"),
            "gemini-cli": ("", None),
            "openai": ("OPENAI_API_KEY", ".openai_key"),
            "deepseek": ("DEEPSEEK_API_KEY", None),
            "moonshot": ("MOONSHOT_API_KEY", None),
            "openrouter": ("OPENROUTER_API_KEY", None),
            "xai": ("XAI_API_KEY", None),
            "mock": ("", None),
        }
        if provider not in mapping:
            # 自定义供应商（如中转站）：支持 keys.local.json 或 {PROVIDER}_API_KEY
            return (self._local_keys.get(provider, "")
                    or os.environ.get(f"{provider.upper()}_API_KEY", "").strip())
        env_name, legacy_file = mapping[provider]
        if env_name and os.environ.get(env_name):
            return os.environ[env_name].strip()
        if provider in self._local_keys and self._local_keys[provider]:
            return str(self._local_keys[provider]).strip()
        if legacy_file:
            return self._legacy_key(legacy_file)
        # DeepSeek 兜底：与 codex.py 一致，从 Claude settings.json 读 ANTHROPIC_AUTH_TOKEN
        if provider == "deepseek":
            try:
                settings = Path.home() / ".claude" / "settings.json"
                if settings.exists():
                    text = settings.read_text(encoding="utf-8", errors="replace")
                    m = re.search(r'"ANTHROPIC_AUTH_TOKEN"\s*:\s*"([^"]*)"', text)
                    if m:
                        return m.group(1)
            except Exception:
                pass
        return ""

    def check_models(self, ids: list[str],
                     require_key: bool = True) -> tuple[list[ModelSpec], list[str]]:
        """把模型 id 列表解析成可用 spec；缺失/无 key 的列入警告。"""
        ok: list[ModelSpec] = []
        warns: list[str] = []
        for mid in ids:
            spec = self.models.get(mid)
            if not spec:
                warns.append(f"未注册模型: {mid}")
                continue
            if (require_key and spec.provider not in ("mock", "gemini-cli")
                    and not self.provider_key(spec.provider)):
                warns.append(f"缺少 key，跳过 {mid} ({spec.provider})")
                continue
            ok.append(spec)
        return ok, warns
