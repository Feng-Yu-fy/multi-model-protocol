"""模型供应商层 — 纯标准库 HTTP 调用。

- gemini: Google Generative Language REST（v1beta）
- openai / deepseek / moonshot / openrouter: OpenAI 兼容 /chat/completions
- mock: 离线演示/测试

所有供应商统一返回 LLMResult；调用失败返回 error 字段，由引擎决定降级或中止。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, ModelSpec


def _ensure_proxy():
    """读取 COUNCIL_PROXY（可选）：调用方未设 HTTP_PROXY 时自动启用，
    供代理网络环境（访问 Gemini / xAI 等境外 API）使用。"""
    proxy = os.environ.get("COUNCIL_PROXY", "").strip()
    if proxy and not (os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")):
        os.environ["HTTP_PROXY"] = proxy
        os.environ["HTTPS_PROXY"] = proxy


_ensure_proxy()


@dataclass
class LLMResult:
    text: str = ""
    sources: list[dict] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class ProviderError(Exception):
    """供应商调用失败（网络/认证/限流/模型不可用）。"""


def _post_json(url: str, headers: dict, body: dict, timeout: int) -> tuple[int, dict]:
    headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    )
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        raise ProviderError(f"HTTP {e.code}: {detail[:300]}") from e
    except urllib.error.URLError as e:
        raise ProviderError(f"网络错误: {e.reason}") from e
    except json.JSONDecodeError as e:
        raise ProviderError(f"响应不是 JSON: {e}") from e


def _estimate_cost(spec: ModelSpec, usage: dict) -> float:
    pin = usage.get("prompt_tokens") or 0
    pout = usage.get("completion_tokens") or 0
    return pin * spec.price_in / 1_000_000 + pout * spec.price_out / 1_000_000


def _call_gemini(spec: ModelSpec, key: str, system: str, user: str,
                 timeout: int, use_search: bool) -> LLMResult:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{spec.model}:generateContent?key={key}"
    )
    body: dict = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": spec.temperature,
            "maxOutputTokens": spec.max_tokens,
        },
    }
    if use_search:
        body["tools"] = [{"googleSearch": {}}]
    status, data = _post_json(url, {"Content-Type": "application/json"}, body, timeout)
    if status != 200:
        raise ProviderError(f"Gemini HTTP {status}")
    parts = []
    for cand in data.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            if "text" in part:
                parts.append(part["text"])
    sources = []
    gm = data.get("groundingMetadata") or {}
    seen = set()
    for chunk in gm.get("groundingChunks", []):
        web = chunk.get("web") or {}
        uri = web.get("uri", "")
        if uri and uri not in seen:
            seen.add(uri)
            sources.append({"title": web.get("title", ""), "url": uri})
    usage = data.get("usageMetadata") or {}
    result = LLMResult(
        text="\n".join(parts).strip(),
        sources=sources,
        provider="gemini",
        model=spec.model,
        usage={
            "prompt_tokens": usage.get("promptTokenCount", 0),
            "completion_tokens": usage.get("candidatesTokenCount", 0),
        },
    )
    result.cost_usd = _estimate_cost(spec, result.usage)
    return result


def _call_gemini_cli(spec: ModelSpec, system: str, user: str,
                     timeout: int) -> LLMResult:
    """通过 Antigravity CLI（agy）调用 Google AI Pro/Ultra 订阅额度。

    2026-06-18 起 Google 已停止用 Gemini CLI 服务个人版/AI Pro/Ultra 账号，
    官方继任者为 Antigravity CLI（agy）。登录方式：先运行 `agy` 完成一次
    Google 浏览器登录（用 AI Pro 订阅账号），之后即可无头调用。
    """
    prompt = f"系统要求：\n{system}\n\n用户问题：\n{user}"
    cli = shutil.which("agy")
    if not cli:
        cli = str(Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe")
    if not cli or not os.path.isfile(cli):
        raise ProviderError("未找到 Antigravity CLI：请运行 "
                            "`irm https://antigravity.google/cli/install.ps1 | iex`")

    # 登录预检：agy 未登录时 --print 会静默挂起，先用 models 命令快速探测。
    try:
        auth = subprocess.run(
            [cli, "models"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20,
        )
    except subprocess.TimeoutExpired:
        auth = None
    auth_out = ((auth.stdout or "") + (auth.stderr or "")).lower()
    if auth is not None and "please sign in" in auth_out:
        raise ProviderError(
            "Antigravity CLI 未登录：请先运行 `agy`，按提示在浏览器用 "
            "AI Pro 订阅账号完成登录，然后再试")

    cmd = [cli, "--print", prompt, "--mode", "plan", "--disable-slash-commands"]
    if spec.model and spec.model != "auto":
        cmd += ["--model", spec.model]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise ProviderError(f"Antigravity CLI 超时（>{timeout}s）") from e
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[-300:]
        low = err.lower()
        if "eligibility" in low or "not eligible" in low or "not currently available" in low:
            raise ProviderError(
                "Antigravity 账号地区受限：Google 账号归属国不在支持列表，"
                "需提交 https://policies.google.com/country-association-form "
                "把地区改为美国/新加坡等支持国家，批准后再试")
        if "quota" in low:
            raise ProviderError(
                f"Antigravity 订阅配额已用尽或受限: {err}")
        raise ProviderError(f"Antigravity CLI 退出码 {proc.returncode}: {err}")
    text = proc.stdout.strip()
    if not text:
        raise ProviderError("Antigravity CLI 无输出")
    return LLMResult(text=text, provider="gemini-cli", model=spec.model)


_OPENAI_COMPAT_BASES = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "xai": "https://api.x.ai/v1",
}


def _call_openai_compat(spec: ModelSpec, key: str, system: str, user: str,
                        timeout: int, use_search: bool = False) -> LLMResult:
    base = spec.base_url or _OPENAI_COMPAT_BASES.get(spec.provider, "")
    if not base:
        raise ProviderError(f"未知供应商 {spec.provider}（未配置 base_url）")
    base = base.rstrip("/")
    body: dict = {
        "model": spec.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": spec.temperature,
        "max_tokens": spec.max_tokens,
    }
    # xAI 内置 web_search 工具：research 模式强制先搜再答（Grok 的检索强项）
    if use_search and spec.provider == "xai":
        body["tools"] = [{"type": "web_search"}]
        body["tool_choice"] = "required"
    status, data = _post_json(
        f"{base}/chat/completions",
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        body,
        timeout,
    )
    if status != 200:
        raise ProviderError(f"{spec.provider} HTTP {status}")
    try:
        content = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage") or {}
    except (KeyError, IndexError) as e:
        raise ProviderError(f"响应结构异常: {e}") from e
    result = LLMResult(
        text=content.strip(),
        sources=_extract_citations(data),
        provider=spec.provider,
        model=spec.model,
        usage={
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
        },
    )
    result.cost_usd = _estimate_cost(spec, result.usage)
    return result


def _extract_citations(data: dict) -> list[dict]:
    """兼容提取 xAI/OpenRouter 的 citations 字段（结构不确定时容错）。"""
    sources: list[dict] = []
    for cit in data.get("citations") or []:
        if isinstance(cit, dict):
            url = cit.get("url") or cit.get("uri") or ""
            if url:
                sources.append({"title": cit.get("title", ""), "url": url})
    return sources


def _should_try_next(exc: ProviderError) -> bool:
    """判断是否值得尝试 fallback 模型。"""
    msg = str(exc)
    low = msg.lower()
    if "http 401" in low or "http 403" in low or "http 402" in low:
        return False
    if "未登录" in msg or "未找到 antigravity cli" in low or "未知供应商" in msg:
        return False
    if "网络错误" in msg:
        return False
    # 404/429/400(模型名不存在) 等可继续尝试下一个模型
    return True


def call_model(cfg: Config, spec: ModelSpec, system: str, user: str,
               *, timeout: int | None = None,
               use_search: bool | None = None,
               mock: dict | None = None) -> LLMResult:
    """按 spec 调用模型，失败时沿 fallbacks 链降级。"""
    timeout = timeout or cfg.protocol.request_timeout
    search = spec.use_search if use_search is None else use_search

    if spec.provider == "mock" or mock is not None:
        return _call_mock(spec, user, mock)

    key = cfg.provider_key(spec.provider)
    if not key and spec.provider != "gemini-cli":
        return LLMResult(provider=spec.provider, model=spec.model,
                         error=f"未配置 {spec.provider} API key")

    chain = [spec.model] + [f for f in spec.fallbacks]
    last_err = None
    for mid in chain:
        trial = ModelSpec(
            id=spec.id, name=spec.name, provider=spec.provider, model=mid,
            weight=spec.weight, roles=spec.roles, fallbacks=[],
            base_url=spec.base_url,
            temperature=spec.temperature, max_tokens=spec.max_tokens,
            price_in=spec.price_in, price_out=spec.price_out,
            use_search=spec.use_search,
        )
        try:
            if spec.provider == "gemini":
                return _call_gemini(trial, key, system, user, timeout, search)
            if spec.provider == "gemini-cli":
                return _call_gemini_cli(trial, system, user, timeout)
            if spec.provider in _OPENAI_COMPAT_BASES or trial.base_url:
                return _call_openai_compat(trial, key, system, user, timeout,
                                           use_search=search)
            return LLMResult(provider=spec.provider, model=mid,
                             error=f"不支持的供应商: {spec.provider}")
        except ProviderError as e:
            last_err = e
            if not _should_try_next(e):
                break
    return LLMResult(provider=spec.provider, model=spec.model,
                     error=f"所有模型调用失败: {last_err}")


# ── Mock（离线演示 / 测试）──────────────────────────────
def _call_mock(spec: ModelSpec, user: str, mock: dict | None) -> LLMResult:
    m = mock or {}
    text = m.get("text") or (
        f"【{spec.name} 的模拟提案】\n"
        f"核心结论: 针对「{user[:50]}」给出独立观点。\n"
        f"理由: 1) 论证一; 2) 论证二; 3) 风险说明。\n"
        f"置信度: 0.8"
    )
    return LLMResult(
        text=text,
        sources=[],
        provider="mock",
        model=spec.model,
        usage={"prompt_tokens": 100, "completion_tokens": 100},
        cost_usd=0.0,
    )
