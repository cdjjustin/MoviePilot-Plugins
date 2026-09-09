"""整理模块测试。"""

import os
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins.v3"
sys.path.insert(0, str(PLUGIN_DIR))

from audiobookorganizer.models import AudiobookMetadata, AudioFile, BookEntry, TrackInfo  # noqa: E402
from audiobookorganizer.organizer import (  # noqa: E402
    apply_plan,
    build_local_metadata,
    compute_confidence,
    match_tracks,
    merge_metadata,
    preview_plan,
    resolve_metadata,
)


@pytest.fixture()
def sample_book(tmp_path: Path) -> BookEntry:
    book_dir = tmp_path / "三体"
    book_dir.mkdir()
    files = []
    for i in range(1, 4):
        f = book_dir / f"第{i:02d}集.mp3"
        f.write_bytes(b"ID3" + b"\x00" * 100)
        files.append(
            AudioFile(
                path=f,
                relative_path=f.name,
                episode=i,
                episode_title=f"第{i:02d}集",
            )
        )
    return BookEntry(book_id="test123", name="三体", path=book_dir, files=files)


@pytest.fixture()
def sample_metadata() -> AudiobookMetadata:
    return AudiobookMetadata(
        title="三体",
        author="刘慈欣",
        narrator="某某",
        cover_url="https://example.com/cover.jpg",
        source="ximalaya",
        source_id="12345",
        tracks=[
            TrackInfo(episode=1, title="科学边界"),
            TrackInfo(episode=2, title="台球"),
            TrackInfo(episode=3, title="射手"),
        ],
    )


def test_merge_metadata_ximalaya_first(sample_metadata):
    douban = AudiobookMetadata(
        title="三体",
        author="刘慈欣",
        description="科幻小说",
        source="douban",
        source_id="999",
    )
    merged = merge_metadata(sample_metadata, douban, "ximalaya_first")
    assert merged.title == "三体"
    assert merged.author == "刘慈欣"
    assert merged.narrator == "某某"
    assert merged.description == "科幻小说"
    assert len(merged.tracks) == 3


def test_match_tracks_with_metadata(sample_book, sample_metadata):
    matched = match_tracks(sample_book.files, sample_metadata.tracks)
    assert len(matched) == 3
    assert matched[0][1].title == "科学边界"


def test_match_tracks_without_metadata(sample_book):
    matched = match_tracks(sample_book.files, [])
    assert len(matched) == 3


def test_preview_plan_no_changes(sample_book, sample_metadata, tmp_path: Path):
    target = tmp_path / "output"
    plan = preview_plan(
        sample_book,
        sample_metadata,
        source_root=sample_book.path,
        target_root=target,
    )
    assert plan.plan_id
    assert len(plan.changes) == 3
    for change in plan.changes:
        assert not Path(change.target).exists()


def test_apply_plan_dry_run(sample_book, sample_metadata, tmp_path: Path):
    target = tmp_path / "output"
    plan = preview_plan(
        sample_book,
        sample_metadata,
        source_root=sample_book.path,
        target_root=target,
    )
    result = apply_plan(plan, target_root=target, dry_run=True)
    assert len(result["success"]) == 3
    assert sample_book.files[0].path.exists()


def test_compute_confidence_high(sample_metadata):
    score = compute_confidence("三体", sample_metadata, 3)
    assert score >= 0.8


def test_compute_confidence_low():
    meta = AudiobookMetadata(title="完全不同的书")
    score = compute_confidence("三体", meta, 10)
    assert score < 0.5


def test_build_local_metadata(sample_book):
    meta = build_local_metadata(sample_book)
    assert meta.title == "三体"
    assert meta.source == "local"
    assert meta.author == ""


def test_resolve_metadata_with_fallback(sample_book):
    meta, used = resolve_metadata(sample_book, AudiobookMetadata(title=""), local_fallback=True)
    assert used is True
    assert meta.title == "三体"


def test_resolve_metadata_without_fallback(sample_book):
    meta, used = resolve_metadata(sample_book, AudiobookMetadata(title=""), local_fallback=False)
    assert used is False
    assert meta.title == ""


def test_assign_unique_episodes_extras_and_duplicate_集(tmp_path: Path):
    from audiobookorganizer.organizer import assign_unique_episodes, match_tracks
    import re

    book_dir = tmp_path / "drama"
    book_dir.mkdir()
    names = [
        "【主题曲】少年无恙 - 周笔畅.mp3",
        "第1集 局中挣扎.mp3",
        "第1集-解救狮子园.mp3",
        "【插曲】一步天涯 - 高嘉朗.mp3",
        "第2集 相亲相近.mp3",
        "第2集-佛道之辨.mp3",
    ]
    files = []
    for name in names:
        path = book_dir / name
        path.write_bytes(b"x")
        if "主题曲" in name or "插曲" in name:
            season, episode = 0, None
        else:
            season, episode = None, int(re.search(r"第(\d+)集", name).group(1))
        files.append(
            AudioFile(
                path=path,
                relative_path=name,
                season=season,
                episode=episode,
                episode_title=name.replace(".mp3", ""),
            )
        )

    matched = match_tracks(files, [])
    assigned = assign_unique_episodes(matched, default_season=1)
    keys = [(s, e) for _, _, s, e in assigned]
    assert len(keys) == len(set(keys))
    assert keys[0] == (0, 1)  # 主题曲 -> S00E01
    assert keys[1][0] == 1 and keys[2][0] == 1  # 两段第1集在 S01 且集号不同
    assert keys[1][1] != keys[2][1]
    assert keys[3] == (0, 2)  # 插曲 -> S00E02


def test_assign_unique_episodes_season2_from_scrape_title(tmp_path: Path):
    """文件名无季号时，仍应从刮削标题「第二季-015」得到 S02E15，而非连号 S01E58。"""
    from audiobookorganizer.organizer import assign_unique_episodes, match_tracks

    book_dir = tmp_path / "声娱"
    book_dir.mkdir()
    files = []
    tracks = []
    # 模拟前 57 集已对齐后的第 58 个文件
    for i in range(1, 58):
        path = book_dir / f"track{i:03d}.mp3"
        path.write_bytes(b"x")
        files.append(
            AudioFile(path=path, relative_path=path.name, season=None, episode=None)
        )
        tracks.append(TrackInfo(episode=i, title=f"声娱文化 - 第一季-{i:03d} 第{i}集"))

    path = book_dir / "track058.mp3"
    path.write_bytes(b"x")
    files.append(
        AudioFile(path=path, relative_path=path.name, season=None, episode=None)
    )
    tracks.append(TrackInfo(episode=58, title="声娱文化 - 第二季-015 剑气长城"))

    path = book_dir / "track059.mp3"
    path.write_bytes(b"x")
    files.append(
        AudioFile(path=path, relative_path=path.name, season=None, episode=None)
    )
    tracks.append(TrackInfo(episode=59, title="声娱文化 - 第二季-016 情意绵绵"))

    matched = match_tracks(files, tracks)
    assigned = assign_unique_episodes(matched, default_season=1)
    by_name = {Path(af.path).name: (s, e) for af, _, s, e in assigned}
    assert by_name["track001.mp3"] == (1, 1)
    assert by_name["track058.mp3"] == (2, 15)
    assert by_name["track059.mp3"] == (2, 16)


def test_assign_unique_episodes_allows_same_ep_across_seasons(tmp_path: Path):
    from audiobookorganizer.organizer import assign_unique_episodes, match_tracks

    book_dir = tmp_path / "multi"
    book_dir.mkdir()
    files = []
    for name, season, episode in [
        ("第一季-015 a.mp3", 1, 15),
        ("第二季-015 b.mp3", 2, 15),
    ]:
        path = book_dir / name
        path.write_bytes(b"x")
        files.append(
            AudioFile(
                path=path,
                relative_path=name,
                season=season,
                episode=episode,
                episode_title=name.replace(".mp3", ""),
            )
        )
    assigned = assign_unique_episodes(match_tracks(files, []), default_season=1)
    keys = [(s, e) for _, _, s, e in assigned]
    assert keys == [(1, 15), (2, 15)]


def test_preview_plan_subdir_seasons_like_podcast(tmp_path: Path):
    """子目录分季时，本地整理应产出 S01 / S02，而不是全部 S01 连号。"""
    book_dir = tmp_path / "剧"
    files = []
    for season_name, season_num, titles in [
        ("第一季", 1, ["开篇", "续章"]),
        ("第二季", 2, ["剑气长城", "情意绵绵"]),
    ]:
        d = book_dir / season_name
        d.mkdir(parents=True)
        for i, title in enumerate(titles, 1):
            src = d / f"{i:02d} {title}.mp3"
            src.write_bytes(b"ID3" + b"\x00" * 100)
            files.append(
                AudioFile(
                    path=src,
                    relative_path=f"{season_name}/{src.name}",
                    season=season_num,
                    episode=None,
                    episode_title=title,
                )
            )
    book = BookEntry(book_id="drama", name="剧", path=book_dir, files=files)
    meta = AudiobookMetadata(title="剧", author="作者", source="local")
    plan = preview_plan(
        book,
        meta,
        source_root=book_dir,
        target_root=tmp_path / "out",
    )
    targets = [Path(c.target).name for c in plan.changes]
    assert any(n.startswith("S01E") for n in targets)
    assert any(n.startswith("S02E") for n in targets)
    assert not any(n.startswith("S01E03") for n in targets)  # 不应把第二季连进 S01


def test_preview_plan_season2_in_target_name(tmp_path: Path):
    book_dir = tmp_path / "书"
    book_dir.mkdir()
    src = book_dir / "声娱文化 - 第二季-015 剑气长城.mp3"
    src.write_bytes(b"ID3" + b"\x00" * 100)
    book = BookEntry(
        book_id="s2",
        name="书",
        path=book_dir,
        files=[
            AudioFile(
                path=src,
                relative_path=src.name,
                season=2,
                episode=15,
                episode_title="声娱文化 - 第二季-015 剑气长城",
            )
        ],
    )
    meta = AudiobookMetadata(title="书", author="作者", source="local")
    plan = preview_plan(
        book,
        meta,
        source_root=book_dir,
        target_root=tmp_path / "out",
    )
    assert len(plan.changes) == 1
    assert "S02E15" in plan.changes[0].target


def test_cleanup_previous_outputs_removes_hardlinks_and_keeps_source(tmp_path: Path):
    from audiobookorganizer.organizer import cleanup_previous_outputs

    source_root = tmp_path / "seed"
    target_root = tmp_path / "library"
    source_root.mkdir()
    target_root.mkdir()
    src = source_root / "ep.mp3"
    src.write_bytes(b"audio")
    old = target_root / "作者" / "书名" / "S01E304 - old.mp3"
    old.parent.mkdir(parents=True)
    os.link(src, old)
    assert old.exists()
    assert src.stat().st_nlink >= 2

    result = cleanup_previous_outputs(
        target_root=target_root,
        source_paths=[src],
        previous_targets=[str(old)],
    )
    assert result["deleted_count"] >= 1
    assert not old.exists()
    assert src.exists()
    assert src.read_bytes() == b"audio"


def test_preview_plan_uses_filename_season_episode(tmp_path: Path):
    book_dir = tmp_path / "剑来"
    book_dir.mkdir()
    src = book_dir / "S01E304 - 149-第5季-146 酒肆筹备.mp3"
    src.write_bytes(b"ID3" + b"\x00" * 100)
    book = BookEntry(
        book_id="jianlai",
        name="剑来",
        path=book_dir,
        files=[
            AudioFile(
                path=src,
                relative_path=src.name,
                season=5,
                episode=146,
                episode_title="酒肆筹备",
            )
        ],
    )
    target = tmp_path / "out"
    plan = preview_plan(
        book,
        build_local_metadata(book),
        source_root=tmp_path,
        target_root=target,
        organize_mode="hardlink",
    )
    assert len(plan.changes) == 1
    assert "S05E146" in plan.changes[0].target
    assert "酒肆筹备" in plan.changes[0].target
    assert "S01E304" not in plan.changes[0].target



def test_apply_plan_hardlink_keeps_source(sample_book, sample_metadata, tmp_path: Path):
    source_root = sample_book.path.parent
    target = tmp_path / "library"
    plan = preview_plan(
        sample_book,
        sample_metadata,
        source_root=source_root,
        target_root=target,
        organize_mode="hardlink",
    )
    assert any("硬链接" in w for w in plan.warnings)

    original_paths = [f.path for f in sample_book.files]
    result = apply_plan(
        plan,
        target_root=target,
        organize_mode="hardlink",
    )
    assert len(result["success"]) == 3
    assert all(item["mode"] == "hardlink" for item in result["success"])

    for orig in original_paths:
        assert orig.exists(), "源文件应保留在原位"

    for change in plan.changes:
        dst = Path(change.target)
        assert dst.exists()
        assert dst.stat().st_ino == Path(change.source).stat().st_ino


def test_apply_plan_copy_keeps_source(sample_book, sample_metadata, tmp_path: Path):
    source_root = sample_book.path.parent
    target = tmp_path / "library"
    plan = preview_plan(
        sample_book,
        sample_metadata,
        source_root=source_root,
        target_root=target,
        organize_mode="copy",
    )
    result = apply_plan(plan, target_root=target, organize_mode="copy")
    assert len(result["success"]) == 3
    assert all(item["mode"] == "copy" for item in result["success"])
    assert sample_book.files[0].path.exists()

    for change in plan.changes:
        dst = Path(change.target)
        assert dst.exists()
        assert dst.stat().st_ino != Path(change.source).stat().st_ino
