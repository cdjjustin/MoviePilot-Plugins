"""
AudiobookOrganizer – MoviePilot V3 插件
从豆瓣/喜马拉雅刮削有声书元数据，整理本地文件（重命名、目录、标签、封面）。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from fastapi import HTTPException

from app import schemas
from app.plugins import _PluginBase
from app.schemas.types import NotificationType
from app.sdk.logging import logger

from .models import AudiobookMetadata, BookEntry, OrganizePlan, SearchResult
from .organizer import (
    DEFAULT_TEMPLATE,
    apply_plan,
    build_local_metadata,
    cleanup_previous_outputs,
    compute_confidence,
    merge_metadata,
    preview_plan,
    resolve_metadata,
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


def _safe_log_text(value: object) -> str:
    """避免文件名或异常文本中的控制字符伪造日志行。"""
    text = str(value).replace("\\", "\\\\")
    return "".join(
        f"\\x{ord(char):02x}"
        if ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F
        else char
        for char in text
    )[:500]


class AudiobookOrganizer(_PluginBase):
    """有声书刮削整理插件。"""

    plugin_name = "有声书刮削整理"
    plugin_desc = "从豆瓣/喜马拉雅刮削元数据，批量整理有声书文件（重命名、目录、标签、封面）"
    plugin_icon = "https://raw.githubusercontent.com/cdjjustin/MoviePilot-Plugins/main/icons/Audiobookshelf_A.png"
    plugin_version = "3.0.21"
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
    _local_fallback_enabled: bool = True
    _confidence_threshold: float = 0.85

    # 运行时缓存
    _books_cache: List[BookEntry] = []
    _plans_cache: Dict[str, OrganizePlan] = {}
    _candidate_cache: Dict[str, Tuple[str, SearchResult, datetime]] = {}
    _candidate_cache_ttl_seconds = 900
    _candidate_cache_limit = 256

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
        self._local_fallback_enabled = bool(config.get("local_fallback_enabled", True))
        self._candidate_cache = {}
        self.__dict__.pop("_candidate_selection", None)
        self.__dict__.pop("_active_preview", None)
        self.__dict__.pop("_operation_feedback", None)
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

    def stop_service(self) -> None:
        """释放插件后台资源；宿主会按 get_service() 注销定时任务。"""
        self._enabled = False
        self._monitor_enabled = False
        self._candidate_cache = {}
        self.__dict__.pop("_candidate_selection", None)
        self.__dict__.pop("_active_preview", None)
        self._plans_cache = {}

    def _candidate_store(self) -> Dict[str, Tuple[str, SearchResult, datetime]]:
        """返回实例级候选缓存，避免未初始化实例共享类属性。"""
        cache = self.__dict__.get("_candidate_cache")
        if not isinstance(cache, dict):
            cache = {}
            self.__dict__["_candidate_cache"] = cache
        return cache

    # ──────────────────────────── API ────────────────────────────

    def get_api(self) -> List[Dict[str, Any]]:
        response_model = schemas.Response[Dict[str, Any]]
        return [
            {
                "path": "/scan",
                "endpoint": self.api_scan,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "扫描有声书目录",
                "description": "扫描源目录，返回待整理书籍列表",
                "response_model": response_model,
            },
            {
                "path": "/search",
                "endpoint": self.api_search,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "搜索元数据",
                "description": "从豆瓣/喜马拉雅搜索有声书元数据",
                "response_model": response_model,
            },
            {
                "path": "/candidates",
                "endpoint": self.api_candidates,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取书籍刮削候选",
                "description": "按本地书名搜索并返回可选择的远程版本",
                "response_model": response_model,
            },
            {
                "path": "/selection/clear",
                "endpoint": self.api_selection_clear,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "关闭候选选择",
                "description": "清除当前书籍的候选选择状态",
                "response_model": response_model,
            },
            {
                "path": "/preview",
                "endpoint": self.api_preview,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "预览整理计划",
                "description": "根据选中元数据生成整理预览",
                "response_model": response_model,
            },
            {
                "path": "/apply",
                "endpoint": self.api_apply,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "执行整理",
                "description": "应用选中的整理计划",
                "response_model": response_model,
            },
            {
                "path": "/history",
                "endpoint": self.api_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "操作历史",
                "description": "返回最近的整理操作记录",
                "response_model": response_model,
            },
            {
                "path": "/organize",
                "endpoint": self.api_organize,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "手动整理单本",
                "description": "按刮削结果或本地目录名整理指定有声书",
                "response_model": response_model,
            },
            {
                "path": "/organize_all",
                "endpoint": self.api_organize_all,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "批量手动整理",
                "description": "整理上次扫描中的全部待整理有声书",
                "response_model": response_model,
            },
        ]

    @staticmethod
    def _response(
        success: bool,
        data: Any = None,
        message: str = "",
    ) -> schemas.Response[Dict[str, Any]]:
        """宿主详情页按钮走普通 api 客户端，必须返回严格三段式 envelope。"""
        return schemas.Response(success=success, message=message, data=data)

    def api_scan(self) -> schemas.Response[Dict[str, Any]]:
        if not self._enabled:
            raise HTTPException(status_code=503, detail="插件未启用")
        if not self._source_path:
            raise HTTPException(status_code=400, detail="未配置源目录")

        books = scan_directory(self._source_path)
        self._books_cache = books
        self._candidate_cache = {}
        self.__dict__.pop("_candidate_selection", None)
        self.__dict__.pop("_active_preview", None)
        self._plans_cache = {}
        books_data = [b.to_dict() for b in books]
        self.save_data("last_scan", {
            "time": datetime.now(timezone.utc).isoformat(),
            "count": len(books),
            "books": books_data,
        })
        logger.info(f"[AudiobookOrganizer] 扫描完成，共 {len(books)} 本")
        return self._response(
            True,
            data={"total": len(books), "books": books_data},
            message=f"扫描完成，共 {len(books)} 本",
        )

    def api_search(self, keyword: str = "") -> schemas.Response[Dict[str, Any]]:
        if not self._enabled:
            raise HTTPException(status_code=503, detail="插件未启用")
        keyword = (keyword or "").strip()
        if not keyword:
            raise HTTPException(status_code=400, detail="缺少搜索关键词")

        results = self._search_all(keyword)
        return self._response(
            True,
            data={"keyword": keyword, "results": [r.to_dict() for r in results]},
        )

    def api_candidates(self, book_id: str = "") -> schemas.Response[Dict[str, Any]]:
        """按本地书名搜索候选版本，供用户选择后再预览。"""
        if not self._enabled:
            raise HTTPException(status_code=503, detail="插件未启用")
        if not self._source_path:
            raise HTTPException(status_code=400, detail="未配置源目录")
        book_id = (book_id or "").strip()
        book = self._find_book(book_id)
        if not book:
            raise HTTPException(status_code=404, detail="未找到对应书籍")

        search_keywords = [book.name]
        for separator in ("-", "｜", "|", "·"):
            if separator in book.name:
                base_name = book.name.split(separator, 1)[0].strip()
                if base_name and base_name not in search_keywords:
                    search_keywords.append(base_name)
        results: List[SearchResult] = []
        for keyword in search_keywords:
            results = self._search_all(keyword)
            if results:
                break
        cache = self._candidate_store()
        now = datetime.now(timezone.utc)
        expired = [
            key for key, value in cache.items()
            if (now - value[2]).total_seconds() >= self._candidate_cache_ttl_seconds
        ]
        for key in expired:
            cache.pop(key, None)
        if len(cache) >= self._candidate_cache_limit:
            oldest = sorted(cache, key=lambda key: cache[key][2])
            for key in oldest[: len(cache) - self._candidate_cache_limit + 1]:
                cache.pop(key, None)
        candidates = []
        for result in results:
            while len(cache) >= self._candidate_cache_limit:
                oldest = min(cache, key=lambda key: cache[key][2])
                cache.pop(oldest, None)
            candidate_id = uuid4().hex
            cache[candidate_id] = (book_id, result, now)
            item = result.to_dict()
            item["candidate_id"] = candidate_id
            candidates.append(item)
        # PageRender does not consume an API response body. Its successful action
        # reloads get_page(), so retain the results for that subsequent render.
        self.__dict__.setdefault("_candidate_selection", {})[book_id] = candidates
        return self._response(
            True,
            data={"book_id": book_id, "keyword": book.name, "candidates": candidates},
        )

    def api_selection_clear(self, book_id: str = "") -> schemas.Response[Dict[str, Any]]:
        """关闭候选弹窗；缓存的短期 candidate_id 仍由 TTL 管理。"""
        selections = self.__dict__.get("_candidate_selection", {})
        previews = self.__dict__.get("_active_preview", {})
        book_id = (book_id or "").strip()
        selections.pop(book_id, None)
        preview = previews.pop(book_id, None)
        if isinstance(preview, dict) and preview.get("plan_id"):
            self._plans_cache.pop(preview["plan_id"], None)
        for candidate_id, cached in list(self._candidate_store().items()):
            if cached[0] == book_id:
                self._candidate_store().pop(candidate_id, None)
        return self._response(True, data={"book_id": book_id}, message="已关闭候选/预览")

    def api_preview(self, body: dict = None) -> schemas.Response[Dict[str, Any]]:
        if not self._enabled:
            raise HTTPException(status_code=503, detail="插件未启用")

        body = body or {}
        book_id = (body.get("book_id") or "").strip()
        metadata_dict = body.get("metadata") or {}
        candidate_id = body.get("candidate_id")
        if candidate_id is not None and not isinstance(candidate_id, str):
            raise HTTPException(status_code=400, detail="candidate_id 必须是字符串")
        selected_candidate = None
        if candidate_id:
            if metadata_dict:
                raise HTTPException(status_code=400, detail="选择候选时不允许覆盖远程元数据")
            candidate_cache = self._candidate_store()
            cached_candidate = candidate_cache.get(candidate_id)
            if not cached_candidate:
                raise HTTPException(status_code=404, detail="候选已过期，请重新搜索")
            candidate_book_id, candidate_result, created_at = cached_candidate
            if (datetime.now(timezone.utc) - created_at).total_seconds() >= self._candidate_cache_ttl_seconds:
                candidate_cache.pop(candidate_id, None)
                raise HTTPException(status_code=404, detail="候选已过期，请重新搜索")
            if candidate_book_id != book_id:
                raise HTTPException(status_code=400, detail="候选与本地书籍不匹配")
            candidate_cache.pop(candidate_id, None)
            selected_candidate = candidate_result

        source = selected_candidate.source if selected_candidate else body.get("source")
        source_id = selected_candidate.source_id if selected_candidate else body.get("source_id")
        confirmed_source_id = body.get("confirm_source_id")
        if source_id is not None and not isinstance(source_id, str):
            raise HTTPException(status_code=400, detail="source_id 必须是字符串")
        if confirmed_source_id is not None and not isinstance(confirmed_source_id, str):
            raise HTTPException(status_code=400, detail="confirm_source_id 必须是字符串")
        if source_id and not selected_candidate and confirmed_source_id != source_id:
            raise HTTPException(
                status_code=400,
                detail="远程专辑未确认，请提供与 source_id 完全一致的 confirm_source_id",
            )

        book = self._find_book(book_id)
        if not book:
            raise HTTPException(status_code=404, detail="未找到对应书籍")

        metadata = AudiobookMetadata.from_dict(metadata_dict)
        if selected_candidate and source and source_id:
            metadata = self._fetch_metadata(source, source_id)
        elif not metadata.title and source and source_id:
            metadata = self._fetch_metadata(source, source_id)
        metadata, _ = resolve_metadata(book, metadata, local_fallback=self._local_fallback_enabled)

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
        duplicate_targets = self._duplicate_target_paths(plan.changes)
        if duplicate_targets:
            plan.warnings.append("多个源文件指向相同目标，禁止应用：" + "、".join(duplicate_targets))
        self._plans_cache[plan.plan_id] = plan
        self.__dict__.setdefault("_active_preview", {})[book_id] = plan.to_dict()
        self.__dict__.get("_candidate_selection", {}).pop(book_id, None)
        return self._response(True, data=plan.to_dict())

    def api_apply(self, body: dict = None) -> schemas.Response[Dict[str, Any]]:
        if not self._enabled:
            raise HTTPException(status_code=503, detail="插件未启用")

        body = body or {}
        plan_ids = body.get("plan_ids") or []
        if not plan_ids:
            raise HTTPException(status_code=400, detail="未指定整理计划")

        target_root = Path(self._target_path or self._source_path)
        all_results: Dict[str, Any] = {"applied": [], "errors": [], "attempted": []}
        completed_plan_ids = set()
        success_count = 0
        failure_count = 0

        for plan_id in plan_ids:
            plan = self._plans_cache.get(plan_id)
            if not plan:
                all_results["errors"].append({"plan_id": plan_id, "error": "计划不存在或已过期"})
                failure_count += 1
                continue

            duplicate_targets = self._duplicate_target_paths(plan.changes)
            if duplicate_targets:
                conflict = {
                    "plan_id": plan_id,
                    "error": "多个源文件指向相同整理目标，已拒绝应用",
                    "targets": duplicate_targets,
                }
                all_results["errors"].append(conflict)
                failure_count += max(len(plan.changes), 1)
                preview = self.__dict__.get("_active_preview", {}).get(plan.book_id)
                if isinstance(preview, dict):
                    preview["last_error"] = {"errors": [conflict]}
                continue

            result = apply_plan(
                plan,
                target_root=target_root,
                cover_url=plan.metadata.cover_url,
                organize_mode=self._organize_mode,
            )
            if plan.metadata.cover_url and (not plan.cover_path or not Path(plan.cover_path).is_file()):
                cover_errors = result.get("errors")
                if not isinstance(cover_errors, list):
                    cover_errors = []
                if not any(item.get("cover") == plan.cover_path for item in cover_errors if isinstance(item, dict)):
                    cover_errors.append({"cover": plan.cover_path, "error": "封面文件未生成"})
                result["errors"] = cover_errors
            attempt = {"plan_id": plan_id, "book": plan.book_name, "result": result}
            all_results["attempted"].append(attempt)
            self._append_history(plan, result)
            raw_errors = result.get("errors")
            raw_skipped = result.get("skipped")
            raw_success = result.get("success")
            file_errors = raw_errors if isinstance(raw_errors, list) else []
            skipped = raw_skipped if isinstance(raw_skipped, list) else []
            success = raw_success if isinstance(raw_success, list) else []
            planned_pair_counts = Counter((change.source, change.target) for change in plan.changes)
            reported_pairs = [
                (item.get("source"), item.get("target"))
                for item in success
                if isinstance(item, dict)
                and isinstance(item.get("source"), str)
                and isinstance(item.get("target"), str)
            ]
            reported_pair_counts = Counter(reported_pairs)
            success_matches_plan = (
                len(success) == len(plan.changes)
                and reported_pair_counts == planned_pair_counts
            )
            blocked_pairs = set()
            blocked_sources = set()
            blocked_targets = set()
            for item in file_errors + skipped:
                if not isinstance(item, dict):
                    continue
                source = item.get("source") if isinstance(item.get("source"), str) else None
                target = item.get("target") if isinstance(item.get("target"), str) else None
                if source and target:
                    blocked_pairs.add((source, target))
                elif source:
                    blocked_sources.add(source)
                elif target:
                    blocked_targets.add(target)
            verified_success_counts = Counter({
                pair: min(reported_pair_counts[pair], planned_pair_counts[pair])
                for pair in planned_pair_counts
                if reported_pair_counts[pair] > 0
                and pair not in blocked_pairs
                and pair[0] not in blocked_sources
                and pair[1] not in blocked_targets
                and Path(pair[1]).is_file()
            })
            valid_success_count = sum(verified_success_counts.values())
            success_count += valid_success_count
            incomplete_count = max(
                len(file_errors) + len(skipped),
                len(plan.changes) - valid_success_count,
                0,
            )
            if not file_errors and not skipped and success_matches_plan:
                outputs_exist = all(Path(change.target).is_file() for change in plan.changes)
                cover_exists = not plan.metadata.cover_url or bool(
                    plan.cover_path and Path(plan.cover_path).is_file()
                )
                if outputs_exist and cover_exists:
                    all_results["applied"].append(attempt)
                    completed_plan_ids.add(plan_id)
                else:
                    incomplete_count = max(incomplete_count, 1)
                    failure_count += incomplete_count
                    result_errors = result.get("errors")
                    if not isinstance(result_errors, list):
                        result_errors = []
                    for change in plan.changes:
                        if not Path(change.target).is_file():
                            result_errors.append({"source": change.source, "target": change.target, "error": "整理输出不存在"})
                    result["errors"] = result_errors
                    all_results["errors"].append({
                        "plan_id": plan_id,
                        "error": "整理输出未通过完整性校验",
                        "result": result,
                    })
                    remaining_success_counts = verified_success_counts.copy()
                    remaining_changes = []
                    for change in plan.changes:
                        pair = (change.source, change.target)
                        if remaining_success_counts[pair] > 0:
                            remaining_success_counts[pair] -= 1
                        else:
                            remaining_changes.append(change)
                    plan.changes = remaining_changes
                    preview = self.__dict__.get("_active_preview", {}).get(plan.book_id)
                    if isinstance(preview, dict):
                        preview["changes"] = [change.to_dict() for change in remaining_changes]
                        preview["last_error"] = result
                    continue
            else:
                failure_count += incomplete_count
                remaining_success_counts = verified_success_counts.copy()
                remaining_changes = []
                for change in plan.changes:
                    pair = (change.source, change.target)
                    if remaining_success_counts[pair] > 0:
                        remaining_success_counts[pair] -= 1
                    else:
                        remaining_changes.append(change)
                plan.changes = remaining_changes
                preview = self.__dict__.get("_active_preview", {}).get(plan.book_id)
                if isinstance(preview, dict):
                    preview["changes"] = [change.to_dict() for change in remaining_changes]
                    preview["last_error"] = result
                all_results["errors"].append({
                    "plan_id": plan_id,
                    "error": "整理不完整",
                    "result": result,
                })

        warning_count = sum(
            len(attempt.get("result", {}).get("warnings", []))
            for attempt in all_results["attempted"]
            if isinstance(attempt.get("result"), dict)
            and isinstance(attempt.get("result", {}).get("warnings"), list)
        )
        message = f"整理完成：成功 {success_count} 个文件，失败 {failure_count} 个文件/计划"
        if warning_count:
            message += f"，另有 {warning_count} 个旧目标备份未清理（详情见结果）"
        active_previews = self.__dict__.get("_active_preview", {})
        for selected_book_id, preview in list(active_previews.items()):
            if preview.get("plan_id") in completed_plan_ids:
                active_previews.pop(selected_book_id, None)
        self.__dict__["_operation_feedback"] = message
        return self._response(True, data=all_results, message=message)

    def api_history(self, limit: int = 20) -> schemas.Response[Dict[str, Any]]:
        history = self.get_data("organize_history") or []
        return self._response(True, data={"history": history[:limit]})

    def api_organize(
        self,
        book_id: str = "",
        mode: str = "scrape",
    ) -> schemas.Response[Dict[str, Any]]:
        if not self._enabled:
            raise HTTPException(status_code=503, detail="插件未启用")
        if not self._source_path:
            raise HTTPException(status_code=400, detail="未配置源目录")

        book_id = (book_id or "").strip()
        mode = (mode or "scrape").strip().lower()
        if mode not in {"scrape", "local"}:
            raise HTTPException(status_code=400, detail="mode 仅支持 scrape 或 local")
        if not book_id:
            raise HTTPException(status_code=400, detail="缺少 book_id")

        book = self._find_book(book_id)
        if not book:
            raise HTTPException(status_code=404, detail="未找到对应书籍，请先扫描目录")

        if mode == "scrape":
            candidates = self.api_candidates(book_id)
            return self._response(
                True,
                data={"book_id": book_id, "requires_selection": True, **(candidates.data or {})},
                message="已找到候选版本，请选择后再预览和应用",
            )

        result = self._organize_book(book, mode=mode)
        if not result.get("ok"):
            return self._response(False, data=result, message=result.get("error") or "整理失败")

        label = "本地信息" if result.get("local") else "刮削元数据"
        deleted = int((result.get("cleanup") or {}).get("deleted_count") or 0)
        message = f"《{result.get('book')}》已按{label}整理为《{result.get('title')}》"
        if deleted:
            message += f"；已清理旧输出 {deleted} 个文件"
        return self._response(
            True,
            data=result,
            message=message,
        )

    def api_organize_all(
        self,
        mode: str = "local",
        force: str = "0",
    ) -> schemas.Response[Dict[str, Any]]:
        if not self._enabled:
            raise HTTPException(status_code=503, detail="插件未启用")
        if not self._source_path:
            raise HTTPException(status_code=400, detail="未配置源目录")

        mode = (mode or "local").strip().lower()
        if mode not in {"scrape", "local"}:
            raise HTTPException(status_code=400, detail="mode 仅支持 scrape 或 local")
        if mode == "scrape":
            return self._response(
                False,
                message="批量刮削已禁用，请逐本选择远程版本后再预览和应用",
            )
        force_all = str(force or "0").strip().lower() in {"1", "true", "yes"}

        last_scan = self.get_data("last_scan") or {}
        books_data = last_scan.get("books") or []
        if not books_data:
            return self._response(False, message="没有可整理的书籍，请先扫描目录")

        applied: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        for item in books_data:
            if not isinstance(item, dict):
                continue
            if not force_all and item.get("status") == "organized":
                continue
            book = self._find_book(item.get("book_id") or "")
            if not book:
                errors.append({"book": item.get("name"), "error": "书籍不存在"})
                continue
            result = self._organize_book(book, mode=mode)
            if result.get("ok"):
                applied.append(result)
            else:
                errors.append(result)

        message = f"批量整理完成：成功 {len(applied)}，失败 {len(errors)}"
        return self._response(
            True,
            data={"applied": applied, "errors": errors, "mode": mode, "force": force_all},
            message=message,
        )

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
                                        "props": {
                                            "model": "local_fallback_enabled",
                                            "label": "无刮削时本地整理",
                                            "hint": "刮削失败时按目录名/文件名整理",
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
            "local_fallback_enabled": True,
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
        plugin_id = self.__class__.__name__

        # PageRender 会向组件默认插槽写入子节点；VDataTable/VList 的 items 属性和
        # 默认插槽冲突时只显示分页不显示行。改用 content 显式渲染列表项。
        status_label = {
            "pending": "待整理",
            "previewed": "已预览",
            "organized": "已整理",
            "failed": "失败",
        }
        book_nodes: List[dict] = []
        pending_count = 0
        for item in books:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or "未命名"
            book_id = item.get("book_id") or ""
            file_count = item.get("file_count", len(item.get("files") or []))
            raw_status = item.get("status", "pending")
            status = status_label.get(raw_status, raw_status)
            if raw_status != "organized":
                pending_count += 1

            scrape_label = "重新刮削" if raw_status == "organized" else "刮削整理"
            local_label = "重新本地整理" if raw_status == "organized" else "本地整理"
            action_buttons: List[dict] = []
            if book_id:
                action_buttons = [
                    {
                        "component": "VBtn",
                        "props": {
                            "size": "small",
                            "color": "primary",
                            "variant": "tonal",
                            "class": "mr-2 mb-1",
                        },
                        "text": "选择刮削版本" if raw_status != "organized" else "重新选择刮削版本",
                        "events": {
                            "click": {
                                "api": f"plugin/{plugin_id}/candidates",
                                "method": "get",
                                "params": {"book_id": book_id},
                            }
                        },
                    },
                    {
                        "component": "VBtn",
                        "props": {
                            "size": "small",
                            "variant": "outlined",
                            "class": "mb-1",
                        },
                        "text": local_label,
                        "events": {
                            "click": {
                                "api": f"plugin/{plugin_id}/organize",
                                "method": "get",
                                "params": {"book_id": book_id, "mode": "local"},
                            }
                        },
                    },
                ]

            book_nodes.append(
                {
                    "component": "VSheet",
                    "props": {
                        "class": "d-flex flex-wrap align-center justify-space-between ga-2 py-3 px-1 border-b",
                        "color": "transparent",
                    },
                    "content": [
                        {
                            "component": "div",
                            "props": {"class": "flex-grow-1", "style": "min-width: 14rem;"},
                            "content": [
                                {
                                    "component": "div",
                                    "props": {"class": "text-body-1 font-weight-medium"},
                                    "text": name,
                                },
                                {
                                    "component": "div",
                                    "props": {"class": "text-caption text-medium-emphasis"},
                                    "text": f"{file_count} 个文件 · {status}",
                                },
                            ],
                        },
                        {
                            "component": "div",
                            "props": {"class": "d-flex flex-wrap align-center"},
                            "content": action_buttons
                            or [
                                {
                                    "component": "span",
                                    "props": {"class": "text-caption text-medium-emphasis"},
                                    "text": "缺少 book_id，请重新扫描",
                                }
                            ],
                        },
                    ],
                }
            )

        history_nodes: List[dict] = []
        for item in history:
            if not isinstance(item, dict):
                continue
            book = item.get("book") or ""
            title = item.get("metadata_title") or ""
            history_nodes.append(
                {
                    "component": "VListItem",
                    "props": {
                        "title": f"{book} → {title}" if title else book,
                        "subtitle": item.get("time") or "",
                        "lines": "two",
                    },
                }
            )

        bulk_buttons: List[dict] = [
            {
                "component": "VBtn",
                "props": {"color": "primary", "class": "mr-2 mb-2"},
                "text": "扫描目录",
                "events": {
                    "click": {
                        "api": f"plugin/{plugin_id}/scan",
                        "method": "get",
                    }
                },
            },
        ]
        if pending_count:
            bulk_buttons.extend(
                [
                    {
                        "component": "VBtn",
                        "props": {"variant": "outlined", "class": "mr-2 mb-2"},
                        "text": f"全部本地整理（{pending_count}）",
                        "events": {
                            "click": {
                                "api": f"plugin/{plugin_id}/organize_all",
                                "method": "get",
                                "params": {"mode": "local"},
                            }
                        },
                    },
                ]
            )
        if book_nodes:
            bulk_buttons.append(
                {
                    "component": "VBtn",
                    "props": {"variant": "text", "class": "mb-2"},
                    "text": f"全部重新本地整理（{len(book_nodes)}）",
                    "events": {
                        "click": {
                            "api": f"plugin/{plugin_id}/organize_all",
                            "method": "get",
                            "params": {"mode": "local", "force": "1"},
                        }
                    },
                }
            )

        selection_dialogs: List[dict] = []
        selections = self.__dict__.get("_candidate_selection", {})
        active_previews = self.__dict__.get("_active_preview", {})
        for selected_book_id, preview in active_previews.items():
            metadata = preview.get("metadata") or {}
            changes = preview.get("changes") or []
            last_result = preview.get("last_error") or {}
            failed_items = (last_result.get("errors") or []) + (last_result.get("skipped") or [])
            failure_text = "；".join(
                str(item.get("error") or item.get("reason") or item)
                for item in failed_items if isinstance(item, dict)
            )
            duplicate_targets = self._duplicate_target_paths(changes)
            preview_actions = [{
                "component": "VBtn",
                "props": {"variant": "text"},
                "text": "取消预览",
                "events": {"click": {"api": f"plugin/{plugin_id}/selection/clear", "method": "get", "params": {"book_id": selected_book_id}}},
            }]
            if duplicate_targets:
                preview_actions.append({"component": "span", "text": "目标路径冲突，无法应用；请调整命名模板后重新预览。"})
            else:
                preview_actions.append({
                    "component": "VBtn",
                    "props": {"color": "primary"},
                    "text": "确认并整理",
                    "events": {"click": {"api": f"plugin/{plugin_id}/apply", "method": "post", "params": {"plan_ids": [preview.get("plan_id")]}}},
                })
            selection_dialogs.append({
                "component": "VDialog",
                "props": {"modelValue": True, "max-width": 900},
                "content": [{
                    "component": "VCard",
                    "content": [
                        {"component": "VCardTitle", "text": "整理预览"},
                        {"component": "VCardText", "content": [
                            *([{"component": "VAlert", "props": {"type": "error", "variant": "tonal", "class": "mb-3"}, "text": f"上次整理未完成，可修复后重试：{failure_text}"}] if failure_text else []),
                            *([{"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "class": "mb-3"}, "text": "多个文件指向相同目标路径，本计划不能应用。"}] if duplicate_targets else []),
                            {"component": "div", "props": {"class": "text-body-1 font-weight-medium mb-2"}, "text": f"{metadata.get('title', '')} · {metadata.get('author', '')} · {metadata.get('narrator', '')}"},
                            {"component": "div", "props": {"class": "text-caption mb-2"}, "text": f"将处理 {len(changes)} 个文件；请确认版本与目标路径后再应用。"},
                            {"component": "VList", "props": {"density": "compact", "max-height": 320}, "content": [
                                {"component": "VListItem", "props": {"title": item.get("source", ""), "subtitle": item.get("target", ""), "lines": "two"}}
                                for item in changes[:50] if isinstance(item, dict)
                            ]},
                        ]},
                        {"component": "VCardActions", "content": preview_actions},
                    ],
                }],
            })
        for selected_book_id, candidates in selections.items():
            if selected_book_id in active_previews:
                continue
            candidate_rows = []
            for candidate in candidates:
                candidate_text = " · ".join(
                    part for part in (
                        candidate.get("title", ""),
                        candidate.get("author", ""),
                        candidate.get("narrator", ""),
                        f"{candidate.get('track_count', 0)} 集" if candidate.get("track_count") else "",
                        candidate.get("source", ""),
                    ) if part
                )
                candidate_rows.append({
                    "component": "VSheet",
                    "props": {"class": "d-flex flex-wrap align-center justify-space-between ga-2 py-2 border-b", "color": "transparent"},
                    "content": [
                        {"component": "div", "props": {"class": "flex-grow-1 mr-2"}, "text": candidate_text},
                        {
                            "component": "VBtn",
                            "props": {"size": "small", "color": "primary"},
                            "text": "选择此版本",
                            "events": {"click": {
                                "api": f"plugin/{plugin_id}/preview",
                                "method": "post",
                                "params": {"book_id": selected_book_id, "candidate_id": candidate.get("candidate_id")},
                            }},
                        },
                    ],
                })
            selection_dialogs.append({
                "component": "VDialog",
                "props": {"modelValue": True, "max-width": 800},
                "content": [{
                    "component": "VCard",
                    "content": [
                        {"component": "VCardTitle", "text": "选择刮削版本"},
                        {"component": "VCardText", "content": candidate_rows or [{"component": "span", "text": "没有找到候选版本"}]},
                        {"component": "VCardActions", "content": [{
                            "component": "VBtn",
                            "props": {"variant": "text"},
                            "text": "关闭",
                            "events": {"click": {
                                "api": f"plugin/{plugin_id}/selection/clear",
                                "method": "get",
                                "params": {"book_id": selected_book_id},
                            }},
                        }]},
                    ],
                }],
            })

        feedback = self.__dict__.get("_operation_feedback")
        feedback_nodes = []
        if feedback:
            feedback_nodes.append({
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "class": "mb-3"},
                "text": feedback,
            })

        return selection_dialogs + feedback_nodes + [
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
                                                "component": "div",
                                                "props": {"class": "mb-2"},
                                                "content": bulk_buttons,
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "text-body-2 text-medium-emphasis mb-1"},
                                                "text": f"上次扫描：{last_scan.get('time', '从未')}，共 {last_scan.get('count', 0)} 本",
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "text-caption text-medium-emphasis"},
                                                "text": "刮削整理：先搜豆瓣/喜马拉雅再整理；本地整理：按目录名整理（适合已命名好的做种目录）。",
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
                                        "text": f"待整理书籍（{len(book_nodes)}）",
                                    },
                                    {
                                        "component": "VCardText",
                                        "content": book_nodes
                                        or [
                                            {
                                                "component": "span",
                                                "text": "暂无待整理书籍，请先扫描目录",
                                            }
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
                                                "props": {"lines": "two", "class": "py-0"},
                                                "content": history_nodes,
                                            }
                                            if history_nodes
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

    @staticmethod
    def _source_fingerprint(book: BookEntry) -> List[Tuple[str, Optional[int], Optional[int], Optional[int]]]:
        """Capture file identity and metadata without reading large audio payloads."""
        source_files = []
        for audio_file in sorted(book.files, key=lambda item: item.path.as_posix()):
            try:
                stat = audio_file.path.stat()
                source_files.append((str(audio_file.path), stat.st_size, stat.st_mtime_ns, stat.st_ino))
            except OSError:
                source_files.append((str(audio_file.path), None, None, None))
        return source_files

    def _organization_signature(self, book: BookEntry, metadata: AudiobookMetadata) -> str:
        """Fingerprint source state, selected metadata and output-affecting settings."""
        payload = {
            "book_id": book.book_id,
            "source_files": self._source_fingerprint(book),
            "metadata": metadata.to_dict(),
            "target_path": self._target_path or self._source_path,
            "naming_template": self._naming_template,
            "organize_mode": self._organize_mode,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _invalidate_organization_signature(self, book_id: str) -> None:
        """Drop stale ownership data after cleanup so failed replacements cannot reuse it."""
        store = self.get_data("last_organize_targets") or {}
        if not isinstance(store, dict):
            store = {}
        store[book_id] = {
            "signature": "",
            "targets": [],
            "expected_targets": [],
            "output_fingerprints": {},
            "cover_path": "",
            "cleanup_completed": True,
        }
        self.save_data("last_organize_targets", store)

    @staticmethod
    def _duplicate_target_paths(changes: List[Any]) -> List[str]:
        normalized_targets = []
        for change in changes:
            target = change.get("target") if isinstance(change, dict) else getattr(change, "target", "")
            if not isinstance(target, str) or not target:
                continue
            try:
                normalized_targets.append(str(Path(target).resolve()))
            except OSError:
                normalized_targets.append(str(Path(target).absolute()))
        counts = Counter(normalized_targets)
        return sorted(path for path, count in counts.items() if count > 1)

    @staticmethod
    def _path_fingerprint(path: str) -> Optional[List[int]]:
        try:
            stat = Path(path).stat()
            return [stat.st_size, stat.st_mtime_ns, stat.st_ino]
        except OSError:
            return None

    def _outputs_match_signature(self, book_id: str, signature: str) -> bool:
        """Skip destructive rebuilds only when recorded outputs still match."""
        store = self.get_data("last_organize_targets") or {}
        previous = store.get(book_id) if isinstance(store, dict) else None
        if not isinstance(previous, dict) or previous.get("signature") != signature:
            return False
        targets = previous.get("targets") or []
        expected_targets = previous.get("expected_targets") or targets
        output_fingerprints = previous.get("output_fingerprints") or {}
        if not expected_targets or not isinstance(output_fingerprints, dict):
            return False
        if set(output_fingerprints) != set(expected_targets):
            return False
        if any(self._path_fingerprint(path) != output_fingerprints.get(path) for path in expected_targets):
            return False
        cover_path = previous.get("cover_path") or ""
        return not cover_path or Path(cover_path).is_file()

    def _scheduled_scan(self) -> None:
        if not self._enabled or not self._source_path:
            return

        books = scan_directory(self._source_path)
        if not books:
            if self.get_data("pending_notification_signatures"):
                self.save_data("pending_notification_signatures", {})
            return

        notify_lines: List[str] = []
        auto_applied: List[str] = []
        previous_pending = self.get_data("pending_notification_signatures") or {}
        if not isinstance(previous_pending, dict):
            previous_pending = {}
        current_pending: Dict[str, str] = {}

        def queue_pending(book: BookEntry, line: str) -> None:
            fingerprint = json.dumps(self._source_fingerprint(book), separators=(",", ":"))
            signature = hashlib.sha256(f"{line}\0{fingerprint}".encode("utf-8")).hexdigest()
            current_pending[book.book_id] = signature
            if previous_pending.get(book.book_id) != signature:
                notify_lines.append(line)

        for book in books:
            try:
                results = self._search_all(book.name)
            except Exception as exc:
                logger.warning(
                    f"[AudiobookOrganizer] 刮削《{_safe_log_text(book.name)}》失败，改用本地信息：{_safe_log_text(exc)}"
                )
                results = []
            metadata: Optional[AudiobookMetadata] = None
            used_local_fallback = False

            if results:
                best = results[0]
                try:
                    metadata = self._fetch_metadata(best.source, best.source_id)
                except Exception as exc:
                    logger.warning(
                        f"[AudiobookOrganizer] 获取《{_safe_log_text(book.name)}》元数据失败，改用本地信息：{_safe_log_text(exc)}"
                    )
                    metadata = None

            metadata, used_local_fallback = resolve_metadata(
                book,
                metadata,
                local_fallback=self._local_fallback_enabled,
            )

            if not metadata.title:
                queue_pending(book, f"• 《{book.name}》：未找到匹配元数据")
                continue

            confidence = 1.0 if used_local_fallback else compute_confidence(
                book.name, metadata, len(book.files)
            )

            if self._monitor_mode == "auto" and used_local_fallback and confidence >= self._confidence_threshold:
                target_root = Path(self._target_path or self._source_path)
                signature = self._organization_signature(book, metadata)
                if self._outputs_match_signature(book.book_id, signature):
                    logger.info(f"[AudiobookOrganizer] 《{_safe_log_text(book.name)}》输入和输出未变化，跳过重复整理")
                    continue
                plan = preview_plan(
                    book,
                    metadata,
                    source_root=Path(self._source_path),
                    target_root=target_root,
                    template=self._naming_template,
                    organize_mode=self._organize_mode,
                )
                cleanup_info = self._cleanup_before_organize(book, target_root)
                if cleanup_info.get("errors"):
                    logger.error(f"[AudiobookOrganizer] 无法清理《{_safe_log_text(book.name)}》的旧输出，跳过自动整理：{_safe_log_text(cleanup_info)}")
                    queue_pending(book, f"• 《{book.name}》：清理旧输出失败，未执行自动整理")
                    continue
                self._invalidate_organization_signature(book.book_id)
                result = apply_plan(
                    plan,
                    target_root=target_root,
                    cover_url=metadata.cover_url,
                    organize_mode=self._organize_mode,
                    replace_existing=True,
                )
                self._append_history(plan, result, cleanup=cleanup_info)
                raw_succeeded = result.get("success")
                raw_failed = result.get("errors")
                raw_skipped = result.get("skipped")
                succeeded = raw_succeeded if isinstance(raw_succeeded, list) else []
                failed = raw_failed if isinstance(raw_failed, list) else []
                skipped = raw_skipped if isinstance(raw_skipped, list) else []
                complete = (
                    not failed
                    and not skipped
                    and len(succeeded) == len(plan.changes)
                    and bool(plan.changes)
                    and all(Path(change.target).is_file() for change in plan.changes)
                    and (not plan.metadata.cover_url or bool(plan.cover_path and Path(plan.cover_path).is_file()))
                )
                if complete:
                    self._save_organize_targets(book.book_id, plan, result, signature=signature)
                    auto_applied.append(f"• 《{book.name}》（本地信息）")
                else:
                    if plan.metadata.cover_url and (not plan.cover_path or not Path(plan.cover_path).is_file()):
                        if not isinstance(raw_failed, list):
                            raw_failed = []
                        raw_failed.append({"cover": plan.cover_path, "error": "封面文件未生成"})
                        result["errors"] = raw_failed
                    logger.warning(f"[AudiobookOrganizer] 自动整理《{_safe_log_text(book.name)}》未完成：{_safe_log_text(result)}")
                    queue_pending(book, f"• 《{book.name}》：自动整理未完成，请检查失败文件后重试")
            else:
                if used_local_fallback:
                    queue_pending(book, f"• 《{book.name}》：无刮削结果，可按本地信息整理")
                else:
                    queue_pending(
                        book,
                        f"• 《{book.name}》→ {metadata.title}（置信度 {confidence:.0%}，需手动确认）",
                    )

        self.save_data("pending_notification_signatures", current_pending)
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

    def _organize_book(self, book: BookEntry, *, mode: str = "scrape") -> Dict[str, Any]:
        """手动整理单本。mode=scrape 先刮削；mode=local 仅用目录名。"""
        metadata: Optional[AudiobookMetadata] = None
        used_local_fallback = False

        if mode == "scrape":
            results = self._search_all(book.name)
            if results:
                best = results[0]
                metadata = self._fetch_metadata(best.source, best.source_id)
            metadata, used_local_fallback = resolve_metadata(
                book,
                metadata,
                local_fallback=self._local_fallback_enabled,
            )
        else:
            metadata, used_local_fallback = resolve_metadata(
                book,
                None,
                local_fallback=True,
            )

        if not metadata or not metadata.title:
            self._update_book_status(book.book_id, "failed")
            return {
                "ok": False,
                "book": book.name,
                "book_id": book.book_id,
                "error": "无可用元数据",
            }

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
        signature = self._organization_signature(book, metadata)
        if self._outputs_match_signature(book.book_id, signature):
            self._update_book_status(book.book_id, "organized")
            return {
                "ok": True,
                "book": book.name,
                "book_id": book.book_id,
                "title": metadata.title,
                "local": used_local_fallback or mode == "local",
                "mode": mode,
                "plan_id": "",
                "result": {"success": [], "skipped": [], "errors": []},
                "cleanup": {"deleted": [], "deleted_count": 0},
                "unchanged": True,
            }
        cleanup_info = self._cleanup_before_organize(book, target_root)
        if cleanup_info.get("errors"):
            self._update_book_status(book.book_id, "failed")
            return {
                "ok": False,
                "book": book.name,
                "book_id": book.book_id,
                "title": metadata.title,
                "local": used_local_fallback or mode == "local",
                "mode": mode,
                "plan_id": plan.plan_id,
                "cleanup": cleanup_info,
                "error": "清理旧输出失败，未执行整理",
            }
        self._invalidate_organization_signature(book.book_id)
        result = apply_plan(
            plan,
            target_root=target_root,
            cover_url=metadata.cover_url,
            organize_mode=self._organize_mode,
            replace_existing=True,
        )
        self._append_history(plan, result, cleanup=cleanup_info)
        raw_success = result.get("success")
        raw_errors = result.get("errors")
        raw_skipped = result.get("skipped")
        successes = raw_success if isinstance(raw_success, list) else []
        errors = raw_errors if isinstance(raw_errors, list) else []
        skipped = raw_skipped if isinstance(raw_skipped, list) else []
        complete = (
            bool(plan.changes)
            and len(successes) == len(plan.changes)
            and not errors
            and not skipped
            and all(Path(change.target).is_file() for change in plan.changes)
            and (not metadata.cover_url or bool(plan.cover_path and Path(plan.cover_path).is_file()))
        )
        if not complete:
            if metadata.cover_url and (not plan.cover_path or not Path(plan.cover_path).is_file()):
                if not isinstance(raw_errors, list):
                    raw_errors = []
                raw_errors.append({"cover": plan.cover_path, "error": "封面文件未生成"})
                result["errors"] = raw_errors
            self._update_book_status(book.book_id, "failed")
            return {
                "ok": False,
                "book": book.name,
                "book_id": book.book_id,
                "title": metadata.title,
                "local": used_local_fallback or mode == "local",
                "mode": mode,
                "plan_id": plan.plan_id,
                "result": result,
                "cleanup": cleanup_info,
                "error": "整理未完全成功",
            }
        self._save_organize_targets(book.book_id, plan, result, signature=signature)
        self._update_book_status(book.book_id, "organized")
        return {
            "ok": True,
            "book": book.name,
            "book_id": book.book_id,
            "title": metadata.title,
            "local": used_local_fallback or mode == "local",
            "mode": mode,
            "plan_id": plan.plan_id,
            "result": result,
            "cleanup": cleanup_info,
        }

    def _cleanup_before_organize(self, book: BookEntry, target_root: Path) -> Dict[str, Any]:
        """重新整理前清理目标目录中该书的旧输出。"""
        store = self.get_data("last_organize_targets") or {}
        prev = store.get(book.book_id) if isinstance(store, dict) else None
        has_manifest = isinstance(store, dict) and book.book_id in store
        previous_targets: List[str] = []
        previous_cover = ""
        if isinstance(prev, dict):
            previous_targets = list(prev.get("targets") or [])
            previous_cover = str(prev.get("cover_path") or "")

        # 兼容升级前未落库目标路径：从历史记录里尽量找回
        if not previous_targets and not has_manifest:
            for item in self.get_data("organize_history") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("book_id") == book.book_id or item.get("book") == book.name:
                    previous_targets = list(item.get("targets") or [])
                    previous_cover = previous_cover or str(item.get("cover_path") or "")
                    if previous_targets:
                        break

        return cleanup_previous_outputs(
            target_root=target_root,
            source_paths=[f.path for f in book.files],
            previous_targets=previous_targets,
            previous_cover=previous_cover,
        )

    def _save_organize_targets(
        self,
        book_id: str,
        plan: OrganizePlan,
        result: dict,
        *,
        signature: str = "",
    ) -> None:
        success = result.get("success") if isinstance(result, dict) else None
        errors = result.get("errors") if isinstance(result, dict) else None
        skipped = result.get("skipped") if isinstance(result, dict) else None
        expected_targets = [item.target for item in plan.changes]
        if (
            not expected_targets
            or not isinstance(success, list)
            or len(success) != len(expected_targets)
            or errors
            or skipped
            or not all(Path(path).is_file() for path in expected_targets)
            or (plan.metadata.cover_url and not (plan.cover_path and Path(plan.cover_path).is_file()))
        ):
            return

        store = self.get_data("last_organize_targets") or {}
        if not isinstance(store, dict):
            store = {}
        targets = [
            item.get("target")
            for item in (result.get("success") or [])
            if isinstance(item, dict) and item.get("target")
        ]
        store[book_id] = {
            "targets": targets,
            "expected_targets": expected_targets,
            "output_fingerprints": {path: self._path_fingerprint(path) for path in expected_targets},
            "cover_path": plan.cover_path or "",
            "title": plan.metadata.title,
            "book_name": plan.book_name,
            "signature": signature,
            "time": datetime.now(timezone.utc).isoformat(),
        }
        self.save_data("last_organize_targets", store)

    def _update_book_status(self, book_id: str, status: str) -> None:
        last_scan = self.get_data("last_scan") or {}
        books = last_scan.get("books") or []
        changed = False
        for item in books:
            if isinstance(item, dict) and item.get("book_id") == book_id:
                item["status"] = status
                changed = True
                break
        if changed:
            last_scan["books"] = books
            self.save_data("last_scan", last_scan)

        for book in self._books_cache:
            if book.book_id == book_id:
                book.status = status
                break

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

    def _append_history(
        self,
        plan: OrganizePlan,
        result: dict,
        cleanup: Optional[Dict[str, Any]] = None,
    ) -> None:
        history = self.get_data("organize_history") or []
        targets = [
            item.get("target")
            for item in (result.get("success") or [])
            if isinstance(item, dict) and item.get("target")
        ]
        history.insert(
            0,
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "book": plan.book_name,
                "book_id": plan.book_id,
                "plan_id": plan.plan_id,
                "metadata_title": plan.metadata.title,
                "success_count": len(result.get("success", [])),
                "error_count": len(result.get("errors", [])),
                "targets": targets,
                "cover_path": plan.cover_path or "",
                "cleanup_deleted": (cleanup or {}).get("deleted_count", 0),
            },
        )
        self.save_data("organize_history", history[:100])
