"""命名模板引擎。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_name(name: str) -> str:
    """清理文件名非法字符。"""
    cleaned = _INVALID_CHARS.sub("_", (name or "").strip())
    return cleaned.strip(". ") or "unknown"


def render_template(template: str, variables: Dict[str, Any]) -> str:
    """
    渲染命名模板。
    支持 {author}、{title}、{season:02d} 等格式。
    各变量值单独清理，保留模板中的路径分隔符。
    """
    sanitized_vars: Dict[str, Any] = {}
    for k, v in variables.items():
        if k == "ext":
            sanitized_vars[k] = str(v)
        elif k in ("season", "episode"):
            sanitized_vars[k] = v
        else:
            sanitized_vars[k] = sanitize_name(str(v))
    result = template
    for key, value in sanitized_vars.items():
        placeholder_simple = "{" + key + "}"
        if placeholder_simple in result:
            result = result.replace(placeholder_simple, value)

    def _fmt_replace(m: re.Match) -> str:
        key = m.group(1)
        fmt = m.group(2) or ""
        value = sanitized_vars.get(key, "")
        if fmt:
            try:
                return format(value, fmt) if not isinstance(value, str) else str(value)
            except (ValueError, TypeError):
                return str(value)
        return str(value)

    result = re.sub(r"\{(\w+)(?::([^}]+))?\}", _fmt_replace, result)
    return result


def build_file_path(
    template: str,
    target_root: Path,
    *,
    author: str,
    title: str,
    narrator: str = "",
    series: str = "",
    season: int = 1,
    episode: int = 1,
    episode_title: str = "",
    ext: str = ".mp3",
) -> Path:
    """根据模板生成目标文件路径。"""
    variables = {
        "author": author or "未知作者",
        "title": title or "未知书名",
        "narrator": narrator,
        "series": series or title,
        "season": season,
        "episode": episode,
        "episode_title": episode_title or f"第{episode:02d}集",
        "ext": ext if ext.startswith(".") else f".{ext}",
    }
    rendered = render_template(template, variables)
    if not rendered.endswith(variables["ext"]):
        rendered += variables["ext"]
    return target_root / rendered
