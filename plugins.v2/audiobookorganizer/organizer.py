"""有声书整理：预览与执行。"""

from __future__ import annotations

import hashlib
import shutil
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx

from .models import AudiobookMetadata, BookEntry, FileChange, OrganizePlan, TrackInfo
from .namer import build_file_path, sanitize_name
from .tagger import save_cover, write_tags


DEFAULT_TEMPLATE = "{author}/{title}/S{season:02d}E{episode:02d} - {episode_title}{ext}"


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
) -> OrganizePlan:
    """生成整理预览计划，不修改任何文件。"""
    plan_id = uuid.uuid4().hex[:12]
    warnings: List[str] = []
    changes: List[FileChange] = []

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
    dry_run: bool = False,
) -> Dict[str, object]:
    """执行整理计划。"""
    results = {"success": [], "skipped": [], "errors": []}

    cover_data: Optional[bytes] = None
    if cover_url:
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
            results["success"].append({"source": change.source, "target": change.target, "dry_run": True})
            continue

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.resolve() != dst.resolve():
                shutil.move(str(src), str(dst))
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
            results["success"].append({"source": change.source, "target": change.target})
        except Exception as exc:
            results["errors"].append({"source": change.source, "error": str(exc)})

    if cover_data and plan.cover_path and not dry_run:
        try:
            save_cover(cover_data, Path(plan.cover_path).parent)
        except Exception as exc:
            results["errors"].append({"cover": plan.cover_path, "error": str(exc)})

    return results


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
