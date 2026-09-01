"""
AudiobookOrganizer – MoviePilot V2 插件
从豆瓣/喜马拉雅刮削有声书元数据，整理本地文件（重命名、目录、标签、封面）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from app.plugins import _PluginBase
from app.schemas.types import NotificationType

try:
    from app.sdk.logging import logger
except ImportError:
    from app.log import logger

from .models import AudiobookMetadata, BookEntry, OrganizePlan, SearchResult
from .organizer import (
    DEFAULT_TEMPLATE,
    apply_plan,
    compute_confidence,
    merge_metadata,
    preview_plan,
)
from .scanner import scan_directory
from .scrapers import DoubanScraper, XimalayaScraper

SOURCE_PRIORITY_OPTIONS = [
    {"title": "喜马拉雅优先", "value": "ximalaya_first"},
    {"title": "豆瓣优先", "value": "douban_first"},
    {"title": "仅手动选择", "value": "manual"},
]

MONITOR_MODE_OPTIONS = [
    {"title": "仅通知", "value": "notify"},
    {"title": "高置信度自动整理", "value": "auto"},
]

ORGANIZE_MODE_OPTIONS = [
    {"title": "硬链接（推荐，不影响做种）", "value": "hardlink"},
    {"title": "复制（保留源文件，写入标签）", "value": "copy"},
    {"title": "移动/重命名（原地整理）", "value": "move"},
]


class AudiobookOrganizer(_PluginBase):
    """有声书刮削整理插件。"""

    plugin_name = "有声书刮削整理"
    plugin_desc = "从豆瓣/喜马拉雅刮削元数据，批量整理有声书文件（重命名、目录、标签、封面）"
    plugin_icon = "Audiobookshelf_A.png"
    plugin_version = "1.0.1"
    plugin_author = "cdjjustin"
    author_url = "https://github.com/cdjjustin"
    plugin_config_prefix = "audiobookorganizer_"
    plugin_order = 51
    auth_level = 1

    _enabled: bool = False
    _source_path: str = ""
    _target_path: str = ""
    _naming_template: str = DEFAULT_TEMPLATE
    _source_priority: str = "ximalaya_first"
    _douban_cookie: str = ""
    _ximalaya_cookie: str = ""
    _monitor_enabled: bool = False
    _monitor_interval: int = 60
    _monitor_mode: str = "notify"
    _organize_mode: str = "hardlink"
    _confidence_threshold: float = 0.85

    # 运行时缓存
    _books_cache: List[BookEntry] = []
    _plans_cache: Dict[str, OrganizePlan] = {}

    # ──────────────────────────── 生命周期 ────────────────────────────

    def init_plugin(self, config: dict = None) -> None:
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._source_path = (config.get("source_path") or "").strip()
        self._target_path = (config.get("target_path") or "").strip()
        self._naming_template = (config.get("naming_template") or DEFAULT_TEMPLATE).strip()
        self._source_priority = (config.get("source_priority") or "ximalaya_first").strip()
        self._douban_cookie = (config.get("douban_cookie") or "").strip()
        self._ximalaya_cookie = (config.get("ximalaya_cookie") or "").strip()
        self._monitor_enabled = bool(config.get("monitor_enabled", False))
        self._monitor_mode = (config.get("monitor_mode") or "notify").strip()
        self._organize_mode = (config.get("organize_mode") or "hardlink").strip()
        try:
            self._monitor_interval = max(5, int(config.get("monitor_interval") or 60))
        except (ValueError, TypeError):
            self._monitor_interval = 60
        try:
            self._confidence_threshold = float(config.get("confidence_threshold") or 0.85)
        except (ValueError, TypeError):
            self._confidence_threshold = 0.85

        if not self._target_path:
            self._target_path = self._source_path

    def get_state(self) -> bool:
        return self._enabled and bool(self._source_path)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._monitor_enabled and self._source_path:
            return [
                {
                    "id": "AudiobookOrganizerMonitor",
                    "name": "有声书目录监控",
                    "trigger": "interval",
                    "func": self._scheduled_scan,
                    "kwargs": {"minutes": self._monitor_interval},
                }
            ]
        return []

    # ──────────────────────────── API ────────────────────────────

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/scan",
                "endpoint": self.api_scan,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "扫描有声书目录",
                "description": "扫描源目录，返回待整理书籍列表",
            },
            {
                "path": "/search",
                "endpoint": self.api_search,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "搜索元数据",
                "description": "从豆瓣/喜马拉雅搜索有声书元数据",
            },
            {
                "path": "/preview",
                "endpoint": self.api_preview,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "预览整理计划",
                "description": "根据选中元数据生成整理预览",
            },
            {
                "path": "/apply",
                "endpoint": self.api_apply,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "执行整理",
                "description": "应用选中的整理计划",
            },
            {
                "path": "/history",
                "endpoint": self.api_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "操作历史",
                "description": "返回最近的整理操作记录",
            },
        ]

    def api_scan(self) -> dict:
        if not self._enabled:
            raise HTTPException(status_code=503, detail="插件未启用")
        if not self._source_path:
            raise HTTPException(status_code=400, detail="未配置源目录")

        books = scan_directory(self._source_path)
        self._books_cache = books
        self.save_data("last_scan", {
            "time": datetime.now(timezone.utc).isoformat(),
            "count": len(books),
            "books": [b.to_dict() for b in books],
        })
        logger.info(f"[AudiobookOrganizer] 扫描完成，共 {len(books)} 本")
        return {"total": len(books), "books": [b.to_dict() for b in books]}

    def api_search(self, keyword: str = "") -> dict:
        if not self._enabled:
            raise HTTPException(status_code=503, detail="插件未启用")
        keyword = (keyword or "").strip()
        if not keyword:
            raise HTTPException(status_code=400, detail="缺少搜索关键词")

        results = self._search_all(keyword)
        return {"keyword": keyword, "results": [r.to_dict() for r in results]}

    def api_preview(self, body: dict = None) -> dict:
        if not self._enabled:
            raise HTTPException(status_code=503, detail="插件未启用")

        body = body or {}
        book_id = (body.get("book_id") or "").strip()
        metadata_dict = body.get("metadata") or {}

        book = self._find_book(book_id)
        if not book:
            raise HTTPException(status_code=404, detail="未找到对应书籍")

        metadata = AudiobookMetadata.from_dict(metadata_dict)
        if not metadata.title and body.get("source") and body.get("source_id"):
            metadata = self._fetch_metadata(body["source"], body["source_id"])

        source_root = Path(self._source_path)
        target_root = Path(self._target_path or self._source_path)

        plan = preview_plan(
            book,
            metadata,
            source_root=source_root,
            target_root=target_root,
            template=self._naming_template,
            organize_mode=self._organize_mode,
        )
        self._plans_cache[plan.plan_id] = plan
        return plan.to_dict()

    def api_apply(self, body: dict = None) -> dict:
        if not self._enabled:
            raise HTTPException(status_code=503, detail="插件未启用")

        body = body or {}
        plan_ids = body.get("plan_ids") or []
        if not plan_ids:
            raise HTTPException(status_code=400, detail="未指定整理计划")

        target_root = Path(self._target_path or self._source_path)
        all_results: Dict[str, Any] = {"applied": [], "errors": []}

        for plan_id in plan_ids:
            plan = self._plans_cache.get(plan_id)
            if not plan:
                all_results["errors"].append({"plan_id": plan_id, "error": "计划不存在或已过期"})
                continue

            result = apply_plan(
                plan,
                target_root=target_root,
                cover_url=plan.metadata.cover_url,
                organize_mode=self._organize_mode,
            )
            all_results["applied"].append({"plan_id": plan_id, "book": plan.book_name, "result": result})
            self._append_history(plan, result)

        return all_results

    def api_history(self, limit: int = 20) -> dict:
        history = self.get_data("organize_history") or []
        return {"history": history[:limit]}

    # ──────────────────────────── 配置表单 ────────────────────────────

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
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
                                            "model": "source_path",
                                            "label": "有声书源目录",
                                            "placeholder": "/mnt/audiobooks/inbox",
                                            "hint": "待整理的原始有声书目录",
                                            "persistent-hint": True,
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
                                            "model": "target_path",
                                            "label": "整理输出目录",
                                            "placeholder": "留空则与源目录相同",
                                            "hint": "整理后的文件存放位置",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "organize_mode",
                                            "label": "整理方式",
                                            "items": ORGANIZE_MODE_OPTIONS,
                                            "hint": "硬链接：源文件不动，适合做种目录；复制：独立副本可写标签",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
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
                                            "model": "naming_template",
                                            "label": "命名模板",
                                            "placeholder": DEFAULT_TEMPLATE,
                                            "hint": "变量：{author} {title} {season} {episode} {episode_title} {ext}",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "source_priority",
                                            "label": "数据源优先级",
                                            "items": SOURCE_PRIORITY_OPTIONS,
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
                                            "model": "douban_cookie",
                                            "label": "豆瓣 Cookie（可选）",
                                            "type": "password",
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
                                            "model": "ximalaya_cookie",
                                            "label": "喜马拉雅 Cookie（可选）",
                                            "type": "password",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VDivider",
                        "props": {"class": "my-4"},
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "monitor_enabled", "label": "启用目录监控"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "monitor_interval",
                                            "label": "监控间隔（分钟）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "monitor_mode",
                                            "label": "监控模式",
                                            "items": MONITOR_MODE_OPTIONS,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "confidence_threshold",
                                            "label": "自动整理置信度阈值",
                                            "type": "number",
                                            "hint": "0.0 - 1.0",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "source_path": "",
            "target_path": "",
            "organize_mode": "hardlink",
            "naming_template": DEFAULT_TEMPLATE,
            "source_priority": "ximalaya_first",
            "douban_cookie": "",
            "ximalaya_cookie": "",
            "monitor_enabled": False,
            "monitor_interval": 60,
            "monitor_mode": "notify",
            "confidence_threshold": 0.85,
        }

    # ──────────────────────────── 详情页 ────────────────────────────

    def get_page(self) -> List[dict]:
        last_scan = self.get_data("last_scan") or {}
        books = last_scan.get("books", [])
        history = (self.get_data("organize_history") or [])[:5]

        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VCard",
                                "content": [
                                    {
                                        "component": "VCardTitle",
                                        "text": "有声书刮削整理",
                                    },
                                    {
                                        "component": "VCardText",
                                        "content": [
                                            {
                                                "component": "VBtn",
                                                "props": {"color": "primary", "class": "mr-2"},
                                                "text": "扫描目录",
                                                "events": {
                                                    "click": {
                                                        "api": f"plugin/{self.__class__.__name__}/scan",
                                                        "method": "get",
                                                    }
                                                },
                                            },
                                            {
                                                "component": "span",
                                                "text": f"上次扫描：{last_scan.get('time', '从未')}，共 {last_scan.get('count', 0)} 本",
                                            },
                                        ],
                                    },
                                ],
                            },
                        ],
                    },
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VCard",
                                "content": [
                                    {
                                        "component": "VCardTitle",
                                        "text": f"待整理书籍（{len(books)}）",
                                    },
                                    {
                                        "component": "VCardText",
                                        "content": [
                                            {
                                                "component": "VDataTable",
                                                "props": {
                                                    "headers": [
                                                        {"title": "书名", "key": "name"},
                                                        {"title": "文件数", "key": "file_count"},
                                                        {"title": "状态", "key": "status"},
                                                    ],
                                                    "items": books,
                                                    "items-per-page": 10,
                                                },
                                            },
                                        ],
                                    },
                                ],
                            },
                        ],
                    },
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VCard",
                                "content": [
                                    {
                                        "component": "VCardTitle",
                                        "text": "最近整理记录",
                                    },
                                    {
                                        "component": "VCardText",
                                        "content": [
                                            {
                                                "component": "VList",
                                                "props": {"items": history},
                                            }
                                            if history
                                            else {
                                                "component": "span",
                                                "text": "暂无整理记录",
                                            },
                                        ],
                                    },
                                ],
                            },
                        ],
                    },
                ],
            },
        ]

    # ──────────────────────────── 内部方法 ────────────────────────────

    def _scheduled_scan(self) -> None:
        if not self._enabled or not self._source_path:
            return

        books = scan_directory(self._source_path)
        if not books:
            return

        notify_lines: List[str] = []
        auto_applied: List[str] = []

        for book in books:
            results = self._search_all(book.name)
            if not results:
                notify_lines.append(f"• 《{book.name}》：未找到匹配元数据")
                continue

            best = results[0]
            metadata = self._fetch_metadata(best.source, best.source_id)
            confidence = compute_confidence(book.name, metadata, len(book.files))

            if self._monitor_mode == "auto" and confidence >= self._confidence_threshold:
                plan = preview_plan(
                    book,
                    metadata,
                    source_root=Path(self._source_path),
                    target_root=Path(self._target_path or self._source_path),
                    template=self._naming_template,
                    organize_mode=self._organize_mode,
                )
                result = apply_plan(
                    plan,
                    target_root=Path(self._target_path or self._source_path),
                    cover_url=metadata.cover_url,
                    organize_mode=self._organize_mode,
                )
                self._append_history(plan, result)
                auto_applied.append(f"• 《{book.name}》（置信度 {confidence:.0%}）")
            else:
                notify_lines.append(
                    f"• 《{book.name}》→ {metadata.title}（置信度 {confidence:.0%}，需手动确认）"
                )

        if auto_applied:
            self.post_message(
                title="📚 有声书整理 - 自动整理完成",
                text="\n".join(auto_applied),
                mtype=NotificationType.Manual,
            )

        if notify_lines:
            self.post_message(
                title="📚 有声书整理 - 待确认",
                text="\n".join(notify_lines) + "\n\n请到插件详情页手动确认整理。",
                mtype=NotificationType.Manual,
            )

    def _search_all(self, keyword: str) -> List[SearchResult]:
        results: List[SearchResult] = []
        ximalaya = XimalayaScraper(cookie=self._ximalaya_cookie)
        douban = DoubanScraper(cookie=self._douban_cookie)

        results.extend(ximalaya.search(keyword))
        results.extend(douban.search(keyword))
        results.sort(key=lambda r: r.score, reverse=True)
        return results

    def _fetch_metadata(self, source: str, source_id: str) -> AudiobookMetadata:
        ximalaya_data: Optional[AudiobookMetadata] = None
        douban_data: Optional[AudiobookMetadata] = None

        if source == "ximalaya":
            ximalaya_data = XimalayaScraper(cookie=self._ximalaya_cookie).fetch(source_id)
        elif source == "douban":
            douban_data = DoubanScraper(cookie=self._douban_cookie).fetch(source_id)

        if self._source_priority != "manual":
            if not ximalaya_data:
                ximalaya_scraper = XimalayaScraper(cookie=self._ximalaya_cookie)
                search_results = ximalaya_scraper.search(
                    (ximalaya_data or douban_data or AudiobookMetadata(title="")).title
                )
                if search_results:
                    ximalaya_data = ximalaya_scraper.fetch(search_results[0].source_id)

            if not douban_data:
                douban_scraper = DoubanScraper(cookie=self._douban_cookie)
                title = (ximalaya_data or douban_data or AudiobookMetadata(title="")).title
                search_results = douban_scraper.search(title)
                if search_results:
                    douban_data = douban_scraper.fetch(search_results[0].source_id)

        return merge_metadata(ximalaya_data, douban_data, self._source_priority)

    def _find_book(self, book_id: str) -> Optional[BookEntry]:
        for book in self._books_cache:
            if book.book_id == book_id:
                return book
        last_scan = self.get_data("last_scan") or {}
        for item in last_scan.get("books", []):
            if item.get("book_id") == book_id:
                from .models import AudioFile

                files = [
                    AudioFile(
                        path=Path(f["path"]),
                        relative_path=f["relative_path"],
                        season=f.get("season"),
                        episode=f.get("episode"),
                        episode_title=f.get("episode_title", ""),
                    )
                    for f in item.get("files", [])
                ]
                return BookEntry(
                    book_id=item["book_id"],
                    name=item["name"],
                    path=Path(item["path"]),
                    files=files,
                    status=item.get("status", "pending"),
                )
        return None

    def _append_history(self, plan: OrganizePlan, result: dict) -> None:
        history = self.get_data("organize_history") or []
        history.insert(
            0,
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "book": plan.book_name,
                "plan_id": plan.plan_id,
                "metadata_title": plan.metadata.title,
                "success_count": len(result.get("success", [])),
                "error_count": len(result.get("errors", [])),
            },
        )
        self.save_data("organize_history", history[:100])
