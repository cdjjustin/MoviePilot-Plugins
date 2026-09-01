"""有声书整理：预览与执行。"""

from __future__ import annotations

import errno
import os
import shutil
import uuid
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple

import httpx

from .models import AudiobookMetadata, BookEntry, FileChange, OrganizePlan, TrackInfo
from .namer import build_file_path, sanitize_name
from .tagger import save_cover, write_tags


DEFAULT_TEMPLATE = "{author}/{title}/S{season:02d}E{episode:02d} - {episode_title}{ext}"
OrganizeMode = Literal["move", "hardlink", "copy"]


def merge_metadata(
    ximalaya: Optional[AudiobookMetadata],
    douban: Optional[AudiobookMetadata],
    priority: str = "ximalaya_first",
) -> AudiobookMetadata:
    """合并两个数据源的元数据。"""
    if priority == "douban_first":
        primary, secondary = douban, ximalaya
    else:
        primary, secondary = ximalaya, douban

    if not primary and not secondary:
        return AudiobookMetadata(title="")
    if not primary:
        return secondary  # type: ignore[return-value]
    if not secondary:
        return primary

    return AudiobookMetadata(
        title=primary.title or secondary.title,
        author=secondary.author or primary.author,
        narrator=primary.narrator or secondary.narrator,
        series=primary.series or secondary.series or primary.title,
        season=primary.season or secondary.season or 1,
        description=secondary.description or primary.description,
        cover_url=primary.cover_url or secondary.cover_url,
        source=primary.source,
        source_id=primary.source_id,
        tracks=primary.tracks or secondary.tracks,
    )


def match_tracks(
    files: List,
    tracks: List[TrackInfo],
) -> List[Tuple[object, TrackInfo]]:
    """将本地文件与远程分集列表对齐。"""
    if not tracks:
        return [(f, TrackInfo(episode=i + 1, title=f.episode_title or f"第{i+1}集")) for i, f in enumerate(files)]

    track_by_ep = {t.episode: t for t in tracks}
    matched: List[Tuple[object, TrackInfo]] = []

    for idx, f in enumerate(files):
        ep = f.episode or (idx + 1)
        track = track_by_ep.get(ep)
        if not track and idx < len(tracks):
            track = tracks[idx]
        if not track:
            track = TrackInfo(episode=ep, title=f.episode_title or f"第{ep:02d}集")
        matched.append((f, track))

    return matched


def preview_plan(
    book: BookEntry,
    metadata: AudiobookMetadata,
    *,
    source_root: Path,
    target_root: Path,
    template: str = DEFAULT_TEMPLATE,
    organize_mode: OrganizeMode = "hardlink",
) -> OrganizePlan:
    """生成整理预览计划，不修改任何文件。"""
    plan_id = uuid.uuid4().hex[:12]
    warnings: List[str] = []
    changes: List[FileChange] = []

    if organize_mode == "hardlink":
        if source_root.resolve() == target_root.resolve():
            warnings.append("源目录与目标目录相同，硬链接无意义，将按移动模式处理")
        else:
            warnings.append(
                "硬链接模式：源文件保持原位（不影响做种），仅在目标目录创建硬链接；"
                "不写入音频标签（硬链接与源文件共享数据，写入标签会改变文件哈希）"
            )
    elif organize_mode == "copy":
        warnings.append("复制模式：源文件保持不变，在目标目录创建副本并写入标签")
    else:
        warnings.append("移动模式：源文件将被移动/重命名到目标目录")

    if not metadata.title:
        warnings.append("元数据缺少书名，将使用目录名")

    title = metadata.title or book.name
    author = metadata.author or "未知作者"
    season = metadata.season or 1

    matched = match_tracks(book.files, metadata.tracks)
    if metadata.tracks and len(metadata.tracks) != len(book.files):
        warnings.append(
            f"远程分集数({len(metadata.tracks)})与本地文件数({len(book.files)})不一致"
        )

    used_targets: Dict[str, str] = {}
    for audio_file, track in matched:
        ep = track.episode or audio_file.episode or 1
        ep_season = audio_file.season or season
        target = build_file_path(
            template,
            target_root,
            author=author,
            title=title,
            narrator=metadata.narrator,
            series=metadata.series or title,
            season=ep_season,
            episode=ep,
            episode_title=track.title,
            ext=audio_file.path.suffix,
        )

        target_str = str(target)
        if target_str in used_targets:
            warnings.append(f"目标路径冲突：{target_str}")
            continue
        used_targets[target_str] = str(audio_file.path)

        if not _is_safe_path(target, target_root):
            warnings.append(f"不安全的目标路径：{target_str}")
            continue

        tags = {
            "title": track.title,
            "author": author,
            "narrator": metadata.narrator,
            "album": title,
            "track_number": str(ep),
        }
        changes.append(
            FileChange(
                source=str(audio_file.path),
                target=target_str,
                tags=tags,
            )
        )

    cover_path = ""
    if metadata.cover_url:
        cover_dir = target_root / sanitize_name(author) / sanitize_name(title)
        cover_path = str(cover_dir / "cover.jpg")

    return OrganizePlan(
        plan_id=plan_id,
        book_id=book.book_id,
        book_name=book.name,
        metadata=metadata,
        changes=changes,
        cover_path=cover_path,
        warnings=warnings,
    )


def apply_plan(
    plan: OrganizePlan,
    *,
    target_root: Path,
    cover_url: str = "",
    organize_mode: OrganizeMode = "hardlink",
    dry_run: bool = False,
) -> Dict[str, object]:
    """执行整理计划。"""
    results = {"success": [], "skipped": [], "errors": []}

    effective_mode = _effective_mode(organize_mode, plan.changes)
    write_tags_enabled = effective_mode in ("move", "copy")

    cover_data: Optional[bytes] = None
    if cover_url and write_tags_enabled:
        cover_data = _download_cover(cover_url)

    for change in plan.changes:
        src = Path(change.source)
        dst = Path(change.target)

        if not src.is_file():
            results["skipped"].append({"source": change.source, "reason": "源文件不存在"})
            continue

        if dst.exists() and dst.resolve() != src.resolve():
            results["skipped"].append({"target": change.target, "reason": "目标已存在"})
            continue

        if dry_run:
            results["success"].append({
                "source": change.source,
                "target": change.target,
                "mode": effective_mode,
                "dry_run": True,
            })
            continue

        try:
            used_mode = _place_file(src, dst, effective_mode)
            if write_tags_enabled or used_mode == "copy_fallback":
                write_tags(
                    dst,
                    title=change.tags.get("title", ""),
                    author=change.tags.get("author", ""),
                    narrator=change.tags.get("narrator", ""),
                    album=change.tags.get("album", ""),
                    track_number=int(change.tags.get("track_number", 0) or 0),
                    description=plan.metadata.description,
                    cover_data=cover_data,
                )
            results["success"].append({
                "source": change.source,
                "target": change.target,
                "mode": used_mode,
            })
        except Exception as exc:
            results["errors"].append({"source": change.source, "error": str(exc)})

    if cover_data and plan.cover_path and write_tags_enabled and not dry_run:
        try:
            save_cover(cover_data, Path(plan.cover_path).parent)
        except Exception as exc:
            results["errors"].append({"cover": plan.cover_path, "error": str(exc)})
    elif plan.cover_path and organize_mode == "hardlink" and cover_url and not dry_run:
        # 硬链接模式仍可在目标目录保存封面图（独立文件，不影响源）
        cover_data = cover_data or _download_cover(cover_url)
        if cover_data:
            try:
                save_cover(cover_data, Path(plan.cover_path).parent)
            except Exception as exc:
                results["errors"].append({"cover": plan.cover_path, "error": str(exc)})

    return results


def _effective_mode(mode: OrganizeMode, changes: List[FileChange]) -> OrganizeMode:
    """源与目标为同一文件时无需操作；同目录硬链接降级为移动。"""
    if mode != "hardlink" or not changes:
        return mode
    sources = {Path(c.source).parent.resolve() for c in changes}
    targets = {Path(c.target).parent.resolve() for c in changes}
    if sources == targets:
        return "move"
    return mode


def _place_file(src: Path, dst: Path, mode: OrganizeMode) -> str:
    """将源文件放置到目标路径，返回实际使用的模式。"""
    if src.resolve() == dst.resolve():
        return "skip"

    dst.parent.mkdir(parents=True, exist_ok=True)

    if mode == "move":
        shutil.move(str(src), str(dst))
        return "move"

    if mode == "hardlink":
        try:
            os.link(src, dst)
            return "hardlink"
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                shutil.copy2(str(src), str(dst))
                return "copy_fallback"
            raise

    if mode == "copy":
        shutil.copy2(str(src), str(dst))
        return "copy"

    raise ValueError(f"未知的整理模式: {mode}")


def _download_cover(url: str) -> Optional[bytes]:
    if not url:
        return None
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.content
    except Exception:
        return None


def _is_safe_path(target: Path, root: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def compute_confidence(book_name: str, metadata: AudiobookMetadata, file_count: int) -> float:
    """计算自动整理的置信度。"""
    if not metadata.title:
        return 0.0

    name = book_name.lower().strip()
    title = metadata.title.lower().strip()
    score = 0.0

    if name == title:
        score += 0.5
    elif name in title or title in name:
        score += 0.35

    if metadata.tracks:
        if len(metadata.tracks) == file_count:
            score += 0.4
        elif abs(len(metadata.tracks) - file_count) <= 2:
            score += 0.2

    if metadata.author:
        score += 0.1

    return min(score, 1.0)
