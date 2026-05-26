"""
AudiobookPodcast – MoviePilot V2 插件
将本地有声书目录生成 iOS 播客（Apple Podcasts）兼容的 RSS 2.0 订阅源。

目录约定
--------
<audiobook_path>/
├── 书名 A/
│   ├── cover.jpg          # 可选封面（cover/folder/front/artwork）
│   ├── 第01集.mp3
│   └── 第02集.mp3
├── 书名 B/
│   ├── CD1/
│   │   └── ...
│   └── CD2/
│       └── ...
└── 散装文件.mp3            # 根目录音频归入"杂项"

订阅地址格式
-----------
{server_url}/api/v1/plugin/AudiobookPodcast/feed?book={书名}&apikey={API密钥}
"""

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType

# ──────────────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────────────

AUDIO_EXTENSIONS: frozenset = frozenset(
    {".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".flac", ".wav", ".opus", ".wma", ".aiff", ".mp4"}
)

MIME_TYPES: Dict[str, str] = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".m4b": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".opus": "audio/ogg; codecs=opus",
    ".wma": "audio/x-ms-wma",
    ".aiff": "audio/aiff",
}

IMAGE_EXTENSIONS: frozenset = frozenset({".jpg", ".jpeg", ".png", ".webp"})
IMAGE_MIME: Dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

# 候选封面文件名（不含扩展名，小写）
COVER_NAMES: frozenset = frozenset({"cover", "folder", "front", "artwork", "album", "thumbnail"})

# 中文数字 → 阿拉伯数字（用于自然排序）
_CN_DIGIT_MAP: Dict[str, int] = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}
# 匹配中文数字序列（一–九十九）
_CN_NUM_RE = re.compile(
    r"[一二三四五六七八九]?十[一二三四五六七八九]?|[一二三四五六七八九]"
)


def _natural_key(s: str) -> list:
    """
    自然排序 key：中文数字（一–九十九）与阿拉伯数字均按数值比较。
    例：第一季 < 第二季 < … < 第七季 < 第十季 < 第十一季
    """
    def _cn_replace(m: re.Match) -> str:
        t = m.group()
        if t == "十":
            return "10"
        if t.startswith("十"):
            return str(10 + _CN_DIGIT_MAP.get(t[1:], 0))
        if "十" in t:
            idx = t.index("十")
            return str(_CN_DIGIT_MAP.get(t[:idx], 0) * 10 + _CN_DIGIT_MAP.get(t[idx + 1:], 0))
        return str(_CN_DIGIT_MAP.get(t, 0))

    normalized = _CN_NUM_RE.sub(_cn_replace, s)
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r"(\d+)", normalized)]


# 匹配书名末尾的码率/格式标记，如 " 128kbps"、"[320K]"、"（FLAC）"、" - MP3" 等
_NAME_JUNK_RE = re.compile(
    r"[\s\-_—\[【（(]+(?:\d+\s*k(?:bps?|b?)?|mp[34]|flac|aac|wav)[\s\]】）)]*$",
    re.IGNORECASE,
)


def _clean_book_name(name: str) -> str:
    """去除书名末尾的码率/格式标记，返回干净的显示名称。"""
    return _NAME_JUNK_RE.sub("", name).strip()


# ──────────────────────────────────────────────────────────────────────────────
# 插件主类
# ──────────────────────────────────────────────────────────────────────────────


class AudiobookPodcast(_PluginBase):
    """将本地有声书目录生成 iOS 播客可订阅的 RSS 订阅源。"""

    # ---------- 插件元数据（必须与 package.v2.json 保持一致） ----------
    plugin_name = "有声书播客"
    plugin_desc = "扫描本地有声书目录，生成 iOS 播客（Apple Podcasts）兼容的 RSS 2.0 订阅源"
    plugin_icon = "Audiobookshelf_A.png"
    plugin_version = "1.0.3"
    plugin_author = "cdjjustin"
    author_url = "https://github.com/cdjjustin"
    plugin_config_prefix = "audiobookpodcast_"
    plugin_order = 50
    auth_level = 1

    # ---------- 运行时状态 ----------
    _enabled: bool = False
    _audiobook_path: str = ""
    _server_url: str = ""
    _podcast_author: str = "AudiobookPodcast"
    _podcast_image: str = ""
    _monitor_enabled: bool = False
    _monitor_interval: int = 30

    # ──────────────────────────── 生命周期 ────────────────────────────

    def init_plugin(self, config: dict = None) -> None:
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._audiobook_path = (config.get("audiobook_path") or "").strip()
        self._server_url = (config.get("server_url") or "").strip().rstrip("/")
        self._podcast_author = (config.get("podcast_author") or "AudiobookPodcast").strip()
        self._podcast_image = (config.get("podcast_image") or "").strip()
        self._monitor_enabled = bool(config.get("monitor_enabled", False))
        try:
            self._monitor_interval = max(5, int(config.get("monitor_interval") or 30))
        except (ValueError, TypeError):
            self._monitor_interval = 30

    def get_state(self) -> bool:
        return self._enabled and bool(self._audiobook_path) and bool(self._server_url)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """注册定时目录监控任务。"""
        if self._enabled and self._monitor_enabled and self._audiobook_path:
            return [
                {
                    "id": "AudiobookPodcastMonitor",
                    "name": "有声书目录监控",
                    "trigger": "interval",
                    "func": self._scheduled_scan,
                    "kwargs": {"minutes": self._monitor_interval},
                }
            ]
        return []

    # ──────────────────────────── API 注册 ────────────────────────────

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/books",
                "endpoint": self.api_list_books,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "列出所有有声书",
                "description": "返回扫描目录下所有有声书及其 RSS 订阅地址",
            },
            {
                "path": "/feed",
                "endpoint": self.api_get_feed,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "获取有声书 RSS 订阅源",
                "description": "返回指定有声书的 RSS 2.0 XML（兼容 Apple Podcasts / iOS 播客）",
            },
            {
                "path": "/audio",
                "endpoint": self.api_serve_audio,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "获取音频/封面文件",
                "description": "流式返回音频或封面图片文件，支持 HTTP Range 请求",
            },
            {
                "path": "/scan",
                "endpoint": self.api_scan,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "重新扫描有声书目录",
                "description": "重新扫描目录并通过消息渠道推送整理结果",
            },
        ]

    # ──────────────────────────── 配置表单 ────────────────────────────

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    # ---- 启用开关 ----
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                        ],
                    },
                    # ---- 目录监控 ----
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "monitor_enabled",
                                            "label": "启用目录监控",
                                            "hint": "定期扫描新增内容并推送通知",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "monitor_interval",
                                            "label": "扫描间隔（分钟）",
                                            "type": "number",
                                            "placeholder": "30",
                                            "hint": "最小 5 分钟；建议 15〞60 分钟",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # ---- 有声书根目录 ----
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "audiobook_path",
                                            "label": "有声书根目录",
                                            "placeholder": "/mnt/audiobooks",
                                            "hint": "每个子目录视为一本书/一个播客节目；根目录下的音频归入「杂项」",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    # ---- 服务器外部访问地址 ----
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "server_url",
                                            "label": "MoviePilot 外部访问地址",
                                            "placeholder": "http://192.168.1.100:3001",
                                            "hint": "iOS 设备需能直接访问该地址；用于构建 RSS 中的音频 URL",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    # ---- 作者 & 默认封面 ----
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "podcast_author",
                                            "label": "播客作者",
                                            "placeholder": "AudiobookPodcast",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "podcast_image",
                                            "label": "默认封面图 URL（可选）",
                                            "placeholder": "https://example.com/cover.jpg",
                                            "hint": "当书目录中没有本地封面时使用",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # ---- 使用说明 ----
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "title": "如何在 iOS 播客 App 中订阅",
                                            "text": (
                                                "打开\"播客\"App → 搜索 → 右上角通过 URL 收听，"
                                                "粘贴订阅地址即可。\n\n"
                                                "订阅地址格式：\n"
                                                "{服务器地址}/api/v1/plugin/AudiobookPodcast/feed"
                                                "?book={书名}&apikey={API密钥}\n\n"
                                                "API 密钥在 MoviePilot 后台 → 设置 → 安全 中查看。\n"
                                                "详情页可直接复制完整订阅地址。"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "audiobook_path": "",
            "server_url": "",
            "podcast_author": "AudiobookPodcast",
            "podcast_image": "",
            "monitor_enabled": False,
            "monitor_interval": 30,
        }

    # ──────────────────────────── 详情页 ────────────────────────────

    def get_page(self) -> List[dict]:
        if not self._enabled:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "warning",
                        "variant": "tonal",
                        "text": "插件未启用，请在配置页中开启。",
                    },
                }
            ]

        if not self._audiobook_path or not self._server_url:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "warning",
                        "variant": "tonal",
                        "text": "请先配置有声书目录和 MoviePilot 外部访问地址。",
                    },
                }
            ]

        books = self._scan_books()
        if not books:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": f"在 {self._audiobook_path} 下未找到音频文件，请检查目录配置。",
                    },
                }
            ]

        apikey = getattr(settings, "API_TOKEN", "YOUR_API_KEY")

        rows = []
        for book in books:
            feed_url = (
                f"{self._server_url}/api/v1/plugin/AudiobookPodcast/feed"
                f"?book={quote(book['name'])}&apikey={apikey}"
            )
            rows.append(
                {
                    "component": "tr",
                    "content": [
                        {
                            "component": "td",
                            "props": {"style": "padding:8px 12px;vertical-align:top"},
                            "text": book.get("display_name", book["name"]),
                        },
                        {
                            "component": "td",
                            "props": {
                                "style": "padding:8px 12px;vertical-align:top;text-align:center"
                            },
                            "text": str(book["count"]),
                        },
                        {
                            "component": "td",
                            "props": {
                                "style": (
                                    "padding:8px 12px;vertical-align:top;"
                                    "word-break:break-all;font-size:12px;font-family:monospace"
                                )
                            },
                            "text": feed_url,
                        },
                    ],
                }
            )

        return [
            {
                "component": "VBtn",
                "props": {
                    "variant": "tonal",
                    "color": "primary",
                    "prepend-icon": "mdi-refresh",
                    "class": "mb-4",
                },
                "events": {
                    "click": {
                        "api": "plugin/AudiobookPodcast/scan",
                        "method": "get",
                    }
                },
                "text": "重新整理",
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "success",
                    "variant": "tonal",
                    "text": (
                        f"共找到 {len(books)} 本有声书。"
                        "将下方订阅地址复制到 iOS 播客 App（搜索 → 通过 URL 收听）即可订阅。"
                    ),
                },
            },
            {
                "component": "div",
                "props": {"style": "overflow-x:auto;margin-top:16px"},
                "content": [
                    {
                        "component": "table",
                        "props": {
                            "style": (
                                "width:100%;border-collapse:collapse;"
                                "border:1px solid rgba(128,128,128,0.2)"
                            )
                        },
                        "content": [
                            {
                                "component": "thead",
                                "content": [
                                    {
                                        "component": "tr",
                                        "props": {
                                            "style": "background:rgba(128,128,128,0.08)"
                                        },
                                        "content": [
                                            {
                                                "component": "th",
                                                "props": {
                                                    "style": "text-align:left;padding:10px 12px"
                                                },
                                                "text": "书名",
                                            },
                                            {
                                                "component": "th",
                                                "props": {
                                                    "style": "text-align:center;padding:10px 12px;white-space:nowrap"
                                                },
                                                "text": "集数",
                                            },
                                            {
                                                "component": "th",
                                                "props": {
                                                    "style": "text-align:left;padding:10px 12px"
                                                },
                                                "text": "RSS 订阅地址（复制到 iOS 播客 App）",
                                            },
                                        ],
                                    }
                                ],
                            },
                            {"component": "tbody", "content": rows},
                        ],
                    }
                ],
            },
        ]

    def stop_service(self) -> None:
        pass

    # ──────────────────────────── 定时监控 ────────────────────────────

    def _scheduled_scan(self) -> None:
        """
        定时扫描：与上次快照对比，仅当文件大小在两个连续扫描周期内保持不变
        （认为下载已完成）时，才将其计入"新增内容"并推送通知。

        持久化数据 key: "file_snapshot"
        结构: {
            "prev":      {book_name: {rel_path: file_size}},  # 上次扫描结果
            "confirmed": {book_name: {rel_path: file_size}},  # 已通知过的稳定文件
        }
        """
        if not self._enabled or not self._audiobook_path:
            return

        # ── 1. 构建当前快照 ──
        books = self._scan_books()
        current: Dict[str, Dict[str, int]] = {}
        for book in books:
            snap: Dict[str, int] = {}
            for f in book["files"]:
                try:
                    rel = str(f.relative_to(book["path"])).replace("\\", "/")
                    snap[rel] = f.stat().st_size
                except OSError:
                    pass
            current[book["name"]] = snap

        # ── 2. 加载历史快照 ──
        saved: Dict[str, Any] = self.get_data("file_snapshot") or {}
        prev: Dict[str, Dict[str, int]] = saved.get("prev", {})
        confirmed: Dict[str, Dict[str, int]] = saved.get("confirmed", {})

        # ── 3. 找出本轮新稳定的书籍/集数 ──
        notify_lines: List[str] = []
        new_confirmed: Dict[str, Dict[str, int]] = {}

        for book_name, cur_files in current.items():
            prev_files = prev.get(book_name, {})
            conf_files = confirmed.get(book_name, {})

            # 稳定文件 = 当前大小与上次扫描一致（文件在整个间隔内未被写入）
            stable_files = {
                path: size
                for path, size in cur_files.items()
                if prev_files.get(path) == size
            }

            # 新稳定文件 = 稳定但尚未通知过（或大小发生了变化，如替换）
            newly_stable = {
                path: size
                for path, size in stable_files.items()
                if conf_files.get(path) != size
            }

            if newly_stable:
                is_new_book = book_name not in confirmed
                count = len(newly_stable)
                if is_new_book:
                    notify_lines.append(f"• 新增《{book_name}》（{count} 集）")
                else:
                    notify_lines.append(f"• 《{book_name}》新增 {count} 集")

            # 更新 confirmed：合并已有 + 新稳定，移除已删除文件
            merged = {**conf_files, **{p: s for p, s in stable_files.items()}}
            new_confirmed[book_name] = {p: s for p, s in merged.items() if p in cur_files}

        # ── 4. 推送通知 ──
        if notify_lines:
            self.post_message(
                title="📚 有声书播客 - 检测到新内容",
                text="\n".join(notify_lines),
                mtype=NotificationType.Manual,
            )
            logger.info(f"[AudiobookPodcast] 监控通知：{notify_lines}")

        # ── 5. 持久化快照 ──
        self.save_data(
            "file_snapshot",
            {"prev": current, "confirmed": new_confirmed},
        )

    # ──────────────────────────── API：重新整理 ────────────────────────────

    def api_scan(self) -> dict:
        """
        重新扫描有声书目录，整理完成后通过系统消息渠道推送通知。
        """
        if not self._enabled:
            raise HTTPException(status_code=503, detail="插件未启用")

        books = self._scan_books()
        total = len(books)

        if books:
            apikey = getattr(settings, "API_TOKEN", "")
            lines = []
            for b in books[:15]:
                clean = b.get("display_name", b["name"])
                line = f"• {clean}（{b['count']} 集）"
                if self._server_url and apikey:
                    feed_url = (
                        f"{self._server_url}/api/v1/plugin/AudiobookPodcast/feed"
                        f"?book={quote(b['name'])}&apikey={apikey}"
                    )
                    line += f"\n  {feed_url}"
                lines.append(line)
            if total > 15:
                lines.append(f"... 共 {total} 本")
            detail = "\n".join(lines)
            text = f"共找到 {total} 本有声书：\n{detail}"
        else:
            text = "未找到任何有声书，请检查目录配置。"

        self.post_message(
            title="📚 有声书播客 - 整理完成",
            text=text,
        )

        logger.info(f"[AudiobookPodcast] 重新整理完成，共 {total} 本有声书")
        return {
            "total": total,
            "books": [{"name": b["name"], "count": b["count"]} for b in books],
        }

    # ──────────────────────────── 内部：扫描目录 ────────────────────────────

    def _scan_books(self) -> List[Dict[str, Any]]:
        """
        扫描有声书根目录，返回书籍信息列表。
        每个子目录 → 独立播客；根目录直接放的音频 → 归入"杂项"。
        """
        if not self._audiobook_path:
            return []

        base = Path(self._audiobook_path)
        if not base.is_dir():
            logger.warning(f"[AudiobookPodcast] 目录不存在或无法访问：{base}")
            return []

        books: List[Dict[str, Any]] = []

        # 子目录 → 独立播客（递归收集音频，支持 CD1/CD2 子结构）
        for item in sorted(base.iterdir(), key=lambda p: _natural_key(p.name)):
            if not item.is_dir():
                continue
            audio_files = sorted(
                [
                    f
                    for f in item.rglob("*")
                    if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
                ],
                key=lambda f: _natural_key(str(f.relative_to(item))),
            )
            if audio_files:
                books.append(
                    {
                        "name": item.name,
                        "display_name": _clean_book_name(item.name),
                        "path": item,
                        "files": audio_files,
                        "count": len(audio_files),
                        "cover": self._find_cover(item),
                    }
                )

        # 根目录直接放的音频文件 → 归入"杂项"
        root_files = sorted(
            [
                f
                for f in base.iterdir()
                if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
            ],
            key=lambda f: f.name,
        )
        if root_files:
            books.insert(
                0,
                {
                    "name": "杂项",
                    "path": base,
                    "files": root_files,
                    "count": len(root_files),
                    "cover": self._find_cover(base),
                },
            )

        return books

    @staticmethod
    def _find_cover(directory: Path) -> Optional[str]:
        """
        查找封面图片，返回相对于 directory 的路径字符串或 None。
        优先查书根目录；根目录无封面时递归查子目录（取排序后第一个命中）。
        """
        # 1. 先查根目录
        for f in directory.iterdir():
            if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
                if f.stem.lower() in COVER_NAMES:
                    return f.name
        # 2. 根目录无封面，递归查子目录
        for f in sorted(directory.rglob("*")):
            if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
                if f.stem.lower() in COVER_NAMES:
                    return str(f.relative_to(directory)).replace("\\", "/")
        return None

    def _resolve_file_path(self, book: str, file: str) -> Optional[Path]:
        """
        将 (book, file) 解析为绝对路径，并验证路径在允许目录范围内，
        防止目录遍历攻击（path traversal）。
        """
        if not self._audiobook_path:
            return None

        base = Path(self._audiobook_path).resolve()
        try:
            if book in ("杂项",):
                # 根目录下的文件，file 仅为文件名
                target = (base / file).resolve()
            else:
                # 书目录下的文件，file 可能含子路径（如 CD1/第01集.mp3）
                target = (base / book / file).resolve()
            # 安全检查：目标必须在 base 目录内
            target.relative_to(base)
        except (ValueError, Exception):
            return None

        return target if target.is_file() else None

    # ──────────────────────────── 内部：构建 RSS ────────────────────────────

    def _build_rss_xml(self, book: Dict[str, Any]) -> str:
        """
        构建符合 Apple Podcasts / iOS 播客规范的 RSS 2.0 XML 字符串。
        参考：https://podcasters.apple.com/support/823-podcast-requirements
        """
        from xml.sax.saxutils import escape as xe

        name: str = book["name"]
        display_name: str = book.get("display_name", name)
        files: List[Path] = book["files"]
        book_path: Path = book["path"]
        cover: Optional[str] = book.get("cover")

        apikey: str = getattr(settings, "API_TOKEN", "")

        def _audio_url(f: Path) -> str:
            """生成带 apikey 的音频文件 URL。"""
            rel = f.relative_to(book_path)
            rel_str = str(rel).replace("\\", "/")
            return (
                f"{self._server_url}/api/v1/plugin/AudiobookPodcast/audio"
                f"?book={quote(name)}&file={quote(rel_str)}&apikey={apikey}"
            )

        # 封面 URL
        if cover:
            cover_url = (
                f"{self._server_url}/api/v1/plugin/AudiobookPodcast/audio"
                f"?book={quote(name)}&file={quote(cover)}&apikey={apikey}"
            )
        elif self._podcast_image:
            cover_url = self._podcast_image
        else:
            cover_url = ""

        # ── 按第一级子目录分季 ──
        # season_key: "" 表示直接放在 book_path 下（无子目录）
        season_buckets: Dict[str, List[Path]] = {}
        for f in files:
            rel_parts = f.relative_to(book_path).parts
            key = rel_parts[0] if len(rel_parts) > 1 else ""
            season_buckets.setdefault(key, []).append(f)

        # 按子目录名自然排序（数字段按整数值比较），保证第2季在第10季前
        sorted_seasons = sorted(season_buckets.keys(), key=_natural_key)
        has_seasons = len(sorted_seasons) > 1 or (
            len(sorted_seasons) == 1 and sorted_seasons[0] != ""
        )

        # <item> 列表
        items: List[str] = []
        for season_num, season_key in enumerate(sorted_seasons, 1):
            season_files = season_buckets[season_key]
            for ep_num, f in enumerate(season_files, 1):
                try:
                    stat = f.stat()
                    file_size: int = stat.st_size
                    mtime: float = stat.st_mtime
                except OSError:
                    logger.warning(f"[AudiobookPodcast] 无法读取文件信息：{f}")
                    continue

                mime = MIME_TYPES.get(f.suffix.lower(), "audio/mpeg")
                url = _audio_url(f)
                guid = hashlib.sha1(f"{name}/{f.relative_to(book_path)}".encode()).hexdigest()
                pub_date = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(
                    "%a, %d %b %Y %H:%M:%S %z"
                )
                # 标题：多季时加季号前缀
                if has_seasons and season_key:
                    episode_title = xe(f"[{season_key}] {ep_num:03d}. {f.stem}")
                else:
                    episode_title = xe(f"{ep_num:03d}. {f.stem}")

                duration_tag = ""
                duration = self._get_audio_duration(f)
                if duration:
                    duration_tag = f"      <itunes:duration>{duration}</itunes:duration>\n"

                season_tag = (
                    f"      <itunes:season>{season_num}</itunes:season>\n"
                    if has_seasons
                    else ""
                )

                items.append(
                    f"    <item>\n"
                    f"      <title>{episode_title}</title>\n"
                    f'      <enclosure url="{xe(url)}" length="{file_size}" type="{mime}"/>\n'
                    f'      <guid isPermaLink="false">{guid}</guid>\n'
                    f"      <pubDate>{pub_date}</pubDate>\n"
                    f"      <itunes:title>{episode_title}</itunes:title>\n"
                    f"      <itunes:episode>{ep_num}</itunes:episode>\n"
                    f"{season_tag}"
                    f"{duration_tag}"
                    f"    </item>"
                )

        # 封面标签
        image_block = ""
        if cover_url:
            image_block = (
                f'    <itunes:image href="{xe(cover_url)}"/>\n'
                f"    <image>\n"
                f"      <url>{xe(cover_url)}</url>\n"
                f"      <title>{xe(display_name)}</title>\n"
                f"      <link>{xe(self._server_url)}</link>\n"
                f"    </image>\n"
            )

        channel_link = (
            f"{self._server_url}/api/v1/plugin/AudiobookPodcast/feed"
            f"?book={quote(name)}&apikey={apikey}"
        )

        rss = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0"\n'
            '     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"\n'
            '     xmlns:content="http://purl.org/rss/1.0/modules/content/">\n'
            "  <channel>\n"
            f"    <title>{xe(display_name)}</title>\n"
            f"    <link>{xe(channel_link)}</link>\n"
            f"    <description>{xe(display_name)}</description>\n"
            "    <language>zh-cn</language>\n"
            f"    <itunes:author>{xe(self._podcast_author)}</itunes:author>\n"
            '    <itunes:category text="Arts"/>\n'
            "    <itunes:explicit>no</itunes:explicit>\n"
            "    <itunes:type>serial</itunes:type>\n"
            f"{image_block}"
            + "\n".join(items)
            + "\n  </channel>\n</rss>"
        )
        return rss

    @staticmethod
    def _get_audio_duration(path: Path) -> Optional[str]:
        """
        使用 mutagen 读取音频时长，返回 H:MM:SS 或 M:SS 格式字符串。
        mutagen 未安装或读取失败时静默返回 None。
        """
        try:
            from mutagen import File as MutagenFile  # type: ignore[import]

            audio = MutagenFile(str(path))
            if audio is None or not hasattr(audio, "info") or audio.info is None:
                return None
            seconds = int(audio.info.length)
            h, remainder = divmod(seconds, 3600)
            m, s = divmod(remainder, 60)
            return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        except Exception:
            return None

    # ──────────────────────────── API 端点实现 ────────────────────────────

    def api_list_books(self) -> dict:
        """列出所有有声书及其 RSS 订阅地址。"""
        if not self._enabled:
            raise HTTPException(status_code=503, detail="插件未启用")

        apikey = getattr(settings, "API_TOKEN", "")
        books = self._scan_books()
        result = []
        for b in books:
            feed_url = (
                f"{self._server_url}/api/v1/plugin/AudiobookPodcast/feed"
                f"?book={quote(b['name'])}&apikey={apikey}"
            )
            result.append(
                {
                    "name": b["name"],
                    "episode_count": b["count"],
                    "feed_url": feed_url,
                }
            )
        return {"total": len(result), "books": result}

    def api_get_feed(self, book: str = "") -> Response:
        """
        返回指定有声书的 RSS 2.0 XML 订阅源。
        查询参数 `book` 为书目录名称（URL 编码）。
        """
        if not self._enabled:
            raise HTTPException(status_code=503, detail="插件未启用")
        if not book:
            raise HTTPException(status_code=400, detail="缺少查询参数 book")

        books = self._scan_books()
        matched = next((b for b in books if b["name"] == book), None)
        if matched is None:
            raise HTTPException(status_code=404, detail=f"未找到有声书：{book}")

        xml_content = self._build_rss_xml(matched)
        return Response(
            content=xml_content.encode("utf-8"),
            media_type="application/rss+xml; charset=utf-8",
        )

    def api_serve_audio(self, book: str = "", file: str = "") -> FileResponse:
        """
        提供音频文件或封面图片的流式访问，支持 HTTP Range 请求（断点续播）。

        查询参数：
          book  – 书名（对应根目录下的子目录名，根目录散装文件使用"杂项"）
          file  – 文件相对于 book 目录的路径（支持子目录，如 CD1/第01集.mp3）
        """
        if not self._enabled:
            raise HTTPException(status_code=503, detail="插件未启用")
        if not book or not file:
            raise HTTPException(status_code=400, detail="缺少 book 或 file 参数")

        # 路径解析与安全验证
        target = self._resolve_file_path(book, file)
        if target is None:
            raise HTTPException(status_code=404, detail="文件不存在或路径非法")

        ext = target.suffix.lower()
        allowed = AUDIO_EXTENSIONS | IMAGE_EXTENSIONS
        if ext not in allowed:
            raise HTTPException(status_code=403, detail="不支持的文件类型")

        media_type = MIME_TYPES.get(ext) or IMAGE_MIME.get(ext, "application/octet-stream")
        return FileResponse(path=str(target), media_type=media_type, filename=target.name)
