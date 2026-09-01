"""整理模块测试。"""

import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins.v2"
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


def test_preview_plan_local_metadata_only(sample_book, tmp_path: Path):
    target = tmp_path / "output"
    local_meta = build_local_metadata(sample_book)
    plan = preview_plan(
        sample_book,
        local_meta,
        source_root=sample_book.path.parent,
        target_root=target,
        organize_mode="hardlink",
    )
    assert len(plan.changes) == 3
    assert any("未知作者" in c.target for c in plan.changes)
    assert any("三体" in c.target for c in plan.changes)


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
