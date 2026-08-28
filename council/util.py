"""通用工具：编码修复、JSON 提取、slug、时间戳。"""

from __future__ import annotations

import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path


def ensure_utf8_io() -> None:
    """Windows GBK 终端兼容 UTF-8。"""
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
    if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
        sys.stderr = io.TextIOWrapper(
            sys.stderr.buffer, encoding="utf-8", errors="replace"
        )


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def session_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def slugify(text: str, max_len: int = 40) -> str:
    """把问题转成安全的文件片段名。"""
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "-", text).strip("-")
    s = s[:max_len].rstrip("-")
    return s or "question"


def extract_json(text: str) -> dict | None:
    """从模型回复中健壮提取 JSON：整段 → 代码围栏 → 首个 {...} 块。"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        t = "\n".join(lines[1:])
        if t.endswith("```"):
            t = t[:-3].strip()
        if t.lower().startswith("json"):
            t = t[4:].strip()
    candidates = [t]
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        candidates.append(m.group(0))
    for cand in candidates:
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def read_text(path: str | Path, encoding: str = "utf-8") -> str:
    return Path(path).read_text(encoding=encoding, errors="replace")


def safe_write(path: str | Path, content: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
