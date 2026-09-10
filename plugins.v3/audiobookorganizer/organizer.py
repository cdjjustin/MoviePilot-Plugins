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
from .scanner import is_extra_track, resolve_season_episode
from .tagger import save_cover, write_tags


DEFAULT_TEMPLATE = "{author}/{title}/S{season:02d}E{episode:02d} - {episode_title}{ext}"
OrganizeMode = Literal["move", "hardlink", "copy"]


def build_local_metadata(book: BookEntry) -> AudiobookMetadata:
    """从本地目录名与文件名构建最小元数据（无刮削结果时使用）。"""
    return AudiobookMetadata(
        title=book.name,
        source="local",
        source_id=book.book_id,
    )


def resolve_metadata(
    book: BookEntry,
    metadata: Optional[AudiobookMetadata],
    *,
    local_fallback: bool = False,
) -> Tuple[AudiobookMetadata, bool]:
    """
    解析最终用于整理的元数据。
    返回 (metadata, used_local_fallback)。
    """
    if metadata and metadata.title:
        return metadata, False
    if local_fallback:
        return build_local_metadata(book), True
    return metadata or AudiobookMetadata(title=""), False


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


def _regular_audio_files(files: List) -> List:
    """返回需要与远程正集分集绑定的文件，排除 S00 附属音轨。"""
    return [
        f
        for f in files
        if getattr(f, "season", None) != 0
        and not is_extra_track(getattr(getattr(f, "path", None), "stem", ""))
        and not is_extra_track(getattr(f, "episode_title", ""))
    ]


def match_tracks(
    files: List,
    tracks: List[TrackInfo],
) -> List[Tuple[object, TrackInfo]]:
    """将本地文件与远程分集列表对齐。"""
    if not tracks:
        # 无远程分集时保留文件名信息；最终季/集号由 assign_unique_episodes 统一分配
        return [
            (
                f,
                TrackInfo(
                    episode=getattr(f, "episode", None) or (i + 1),
                    title=getattr(f, "episode_title", None) or f"第{(getattr(f, 'episode', None) or i + 1):02d}集",
                ),
            )
            for i, f in enumerate(files)
        ]

    # S00 附属音轨没有对应的远程正集；不能用 episode 作为唯一键，
    # 否则 S00E01 会和 S01E01 覆盖到同一个远程 track。远程分集
    # 按正集文件的稳定顺序绑定，附属音轨保留本地标题。
    regular_files = _regular_audio_files(files)
    regular_file_ids = {id(f) for f in regular_files}
    regular_tracks = iter(tracks)
    matched: List[Tuple[object, TrackInfo]] = []
    regular_position = 0
    extra_position = 0
    for f in files:
        if id(f) in regular_file_ids:
            regular_position += 1
            track = next(regular_tracks, None)
            if track is not None:
                matched.append((f, track))
                continue
            ep = getattr(f, "episode", None) or regular_position
        else:
            extra_position += 1
            ep = getattr(f, "episode", None) or extra_position
        matched.append((f, TrackInfo(episode=ep, title=getattr(f, "episode_title", "") or f"第{ep:02d}集")))
    return matched


def assign_unique_episodes(
    matched: List[Tuple[object, TrackInfo]],
    *,
    default_season: int = 1,
) -> List[Tuple[object, TrackInfo, int, int]]:
    """
    为每个文件分配不重复的 (season, episode)。

    - 主题曲/插曲等附属音轨 → S00E01, S00E02...
    - 正集：若 (季,集) 互不冲突则沿用解析值；否则按文件顺序在季内顺排
    - 会二次从标题/路径解析「第二季-015」，避免已错标成 S01 后丢季号
    """
    extras: List[Tuple[object, TrackInfo]] = []
    regular: List[Tuple[object, TrackInfo]] = []
    for audio_file, track in matched:
        stem = getattr(audio_file, "path", None)
        stem_name = stem.stem if stem is not None else ""
        title = getattr(audio_file, "episode_title", None) or track.title or ""
        if getattr(audio_file, "season", None) == 0 or is_extra_track(stem_name) or is_extra_track(title):
            extras.append((audio_file, track))
        else:
            regular.append((audio_file, track))

    assigned: List[Tuple[object, TrackInfo, int, int]] = []
    used: set[Tuple[int, int]] = set()

    extra_ep = 1
    for audio_file, track in extras:
        while (0, extra_ep) in used:
            extra_ep += 1
        used.add((0, extra_ep))
        assigned.append((audio_file, track, 0, extra_ep))
        extra_ep += 1

    parsed_rows: List[Tuple[object, TrackInfo, int, Optional[int]]] = []
    for audio_file, track in regular:
        path = getattr(audio_file, "path", None)
        rel = getattr(audio_file, "relative_path", "") or ""
        texts = [
            getattr(path, "stem", None) or "",
            getattr(audio_file, "episode_title", None) or "",
            track.title or "",
            *Path(rel).parts[:-1],
            *(list(getattr(path, "parts", ()))[-3:-1] if path is not None else []),
        ]
        # 不把 file 上可能错误的 season/episode 当作初值，避免挡住标题里的「第二季-015」
        season, episode = resolve_season_episode(*texts)
        if season is None:
            season = getattr(audio_file, "season", None)
        if episode is None:
            episode = getattr(audio_file, "episode", None)
        if season is None:
            season = default_season
        parsed_rows.append((audio_file, track, season, episode))

    # 按 (季, 集) 判重：跨季允许出现相同集号（S01E15 与 S02E15 都合法）
    pair_list = [
        (season, episode)
        for _, _, season, episode in parsed_rows
        if episode is not None
    ]
    unique_ok = (
        len(parsed_rows) > 0
        and len(pair_list) == len(parsed_rows)
        and len(pair_list) == len(set(pair_list))
    )

    if unique_ok:
        for audio_file, track, season, episode in parsed_rows:
            assert episode is not None
            ep = episode
            while (season, ep) in used:
                ep += 1
            used.add((season, ep))
            assigned.append((audio_file, track, season, ep))
    else:
        # 同季多段冲突或缺少集号：优先保留已解析集号，冲突再顺延
        season_counters: Dict[int, int] = {}
        for audio_file, track, season, episode in parsed_rows:
            if episode is not None and (season, episode) not in used:
                used.add((season, episode))
                season_counters[season] = max(season_counters.get(season, 0), episode)
                assigned.append((audio_file, track, season, episode))
                continue
            season_counters[season] = season_counters.get(season, 0) + 1
            ep = season_counters[season]
            while (season, ep) in used:
                ep += 1
                season_counters[season] = ep
            used.add((season, ep))
            assigned.append((audio_file, track, season, ep))

    # 目录创建与媒体库扫描必须遵循稳定的季/集顺序；否则媒体库会按
    # 扫描到的创建顺序保存章节，导致 S04 后跳到 S07 等错乱。
    assigned.sort(key=lambda row: (row[2], row[3], str(getattr(row[0], "path", ""))))
    return assigned


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
    regular_files = _regular_audio_files(book.files)
    if metadata.tracks and len(metadata.tracks) != len(regular_files):
        warnings.append(
            f"远程正集分集数({len(metadata.tracks)})与本地正集文件数({len(regular_files)})不一致"
        )

    assigned = assign_unique_episodes(matched, default_season=season)
    if any(ep_season == 0 for _, _, ep_season, _ in assigned):
        warnings.append("已将主题曲/插曲等附属音轨整理到 S00，避免与正集集号冲突")

    used_targets: Dict[str, str] = {}
    for audio_file, track, ep_season, ep in assigned:
        ep_title = track.title
        if not metadata.tracks and audio_file.episode_title:
            ep_title = audio_file.episode_title
        elif audio_file.episode_title and (
            not ep_title or ep_title.startswith("第") and ep_title.endswith("集")
        ):
            ep_title = audio_file.episode_title

        target = build_file_path(
            template,
            target_root,
            author=author,
            title=title,
            narrator=metadata.narrator,
            series=metadata.series or title,
            season=ep_season,
            episode=ep,
            episode_title=ep_title,
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
            "title": ep_title,
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
    replace_existing: bool = False,
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
            if replace_existing and _is_safe_path(dst, target_root):
                try:
                    dst.unlink()
                except OSError as exc:
                    results["errors"].append({"target": change.target, "error": f"无法覆盖目标: {exc}"})
                    continue
            else:
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


def cleanup_previous_outputs(
    *,
    target_root: Path,
    source_paths: List[Path],
    previous_targets: Optional[List[str]] = None,
    previous_cover: str = "",
) -> Dict[str, Any]:
    """
    删除目标目录中该书上次整理产生的输出。

    - 优先删除 ``previous_targets`` 记录的路径
    - 硬链接模式下，额外清理目标树内指向同一源文件 inode 的链接
      （升级后首次重整理也能清掉错误的 S01… 输出）
    - 只 unlink 目标路径，不会删除做种源文件
    """
    deleted: List[str] = []
    errors: List[str] = []
    root = target_root.resolve()
    source_resolved = {p.resolve() for p in source_paths if p.exists()}

    def _unlink_target(path: Path) -> None:
        try:
            if not path.exists() and not path.is_symlink():
                return
            if not _is_safe_path(path, root):
                errors.append(f"拒绝删除目标外路径: {path}")
                return
            resolved = path.resolve()
            if resolved in source_resolved:
                return
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)
                deleted.append(str(path))
        except OSError as exc:
            errors.append(f"{path}: {exc}")

    for item in previous_targets or []:
        _unlink_target(Path(item))

    if previous_cover:
        _unlink_target(Path(previous_cover))

    inode_to_source: Dict[Tuple[int, int], Path] = {}
    for src in source_paths:
        try:
            st = src.stat()
            inode_to_source[(st.st_dev, st.st_ino)] = src.resolve()
        except OSError:
            continue

    if inode_to_source and root.is_dir():
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                if not _is_safe_path(path, root):
                    continue
                resolved = path.resolve()
                if resolved in source_resolved:
                    continue
                st = path.stat()
                src_resolved = inode_to_source.get((st.st_dev, st.st_ino))
                if src_resolved is not None and resolved != src_resolved:
                    path.unlink(missing_ok=True)
                    deleted.append(str(path))
            except OSError as exc:
                errors.append(f"{path}: {exc}")

    parents = {Path(p).parent for p in deleted}
    for directory in sorted(parents, key=lambda p: len(p.parts), reverse=True):
        _prune_empty_dirs(directory, root)

    # unique preserve order
    seen = set()
    unique_deleted = []
    for item in deleted:
        if item not in seen:
            seen.add(item)
            unique_deleted.append(item)

    return {
        "deleted": unique_deleted,
        "deleted_count": len(unique_deleted),
        "errors": errors,
    }


def _prune_empty_dirs(directory: Path, root: Path) -> None:
    """自下而上删除空目录，不越过 target_root。"""
    current = directory
    root = root.resolve()
    while True:
        try:
            resolved = current.resolve()
            resolved.relative_to(root)
        except Exception:
            break
        if resolved == root:
            break
        try:
            if current.is_dir() and not any(current.iterdir()):
                current.rmdir()
                current = current.parent
                continue
        except OSError:
            break
        break


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
