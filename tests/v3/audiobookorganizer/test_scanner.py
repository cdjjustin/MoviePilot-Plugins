"""目录扫描测试。"""

import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins.v3"
sys.path.insert(0, str(PLUGIN_DIR))

from audiobookorganizer.scanner import (  # noqa: E402
    clean_book_name,
    parse_season_ep_from_stem,
    scan_directory,
)


@pytest.fixture()
def audiobook_tree(tmp_path: Path) -> Path:
    book_a = tmp_path / "三体 128kbps"
    book_a.mkdir()
    (book_a / "001-第一章.mp3").write_bytes(b"fake mp3")
    (book_a / "002-第二章.mp3").write_bytes(b"fake mp3")

    book_b = tmp_path / "盗墓笔记"
    book_b.mkdir()
    cd1 = book_b / "CD1"
    cd1.mkdir()
    (cd1 / "039.第二季.第002集.秦岭神树.mp3").write_bytes(b"fake mp3")

    (tmp_path / "散装.mp3").write_bytes(b"fake mp3")
    return tmp_path


def test_scan_directory_finds_books(audiobook_tree: Path):
    books = scan_directory(str(audiobook_tree))
    names = [b.name for b in books]
    assert "三体 128kbps" in names or "三体" in [clean_book_name(n) for n in names]
    assert "盗墓笔记" in names
    assert "待整理" in names


def test_scan_directory_file_counts(audiobook_tree: Path):
    books = scan_directory(str(audiobook_tree))
    book_map = {b.name: b for b in books}
    assert len(book_map["盗墓笔记"].files) == 1
    assert book_map["待整理"].files[0].relative_path == "散装.mp3"


def test_parse_season_ep_from_stem():
    season, ep = parse_season_ep_from_stem("039.第二季.第002集.秦岭神树")
    assert season == 2
    assert ep == 2


def test_parse_season_ep_chinese_season_with_trailing_number():
    season, ep = parse_season_ep_from_stem("149-第5季-146 酒肆筹备")
    assert season == 5
    assert ep == 146


@pytest.mark.parametrize(
    "stem,expect",
    [
        ("声娱文化 - 第二季-015 剑气长城", (2, 15)),
        ("第二季－015 剑气长城", (2, 15)),  # 全角横线
        ("第二季—016 情意绵绵", (2, 16)),
        ("声娱文化 - 第二季 - 017 十年之约", (2, 17)),
        ("S01E58 - 声娱文化 - 第二季-015 剑气长城", (2, 15)),
        ("第三季-001 开篇", (3, 1)),
    ],
)
def test_parse_season2_dash_episode(stem, expect):
    assert parse_season_ep_from_stem(stem) == expect


def test_resolve_season_episode_prefers_title_over_stale_attrs():
    from audiobookorganizer.scanner import resolve_season_episode

    season, ep = resolve_season_episode(
        "track058.mp3",
        "声娱文化 - 第二季-015 剑气长城",
        season=1,
        episode=58,
    )
    assert (season, ep) == (2, 15)


def test_parse_season_from_dirname():
    from audiobookorganizer.scanner import parse_season_from_dirname

    assert parse_season_from_dirname("第二季") == 2
    assert parse_season_from_dirname("Season 02") == 2
    assert parse_season_from_dirname("S02") == 2


def test_scan_inherits_season_from_parent_dir(tmp_path: Path):
    book = tmp_path / "有声剧"
    season_dir = book / "第二季"
    season_dir.mkdir(parents=True)
    (season_dir / "015 剑气长城.mp3").write_bytes(b"x")
    books = scan_directory(str(tmp_path))
    book_map = {b.name: b for b in books}
    assert book_map["有声剧"].files[0].season == 2


def test_scan_season_buckets_like_podcast(tmp_path: Path):
    """一级子目录分季：目录名含季号用解析值；CD1/CD2 按自然序编号。"""
    book = tmp_path / "多季书"
    for name in ("第一季", "第二季"):
        d = book / name
        d.mkdir(parents=True)
        (d / "01.mp3").write_bytes(b"x")
        (d / "02.mp3").write_bytes(b"x")
    books = scan_directory(str(tmp_path))
    files = {f.relative_path: f for f in books[0].files}
    assert files["第一季/01.mp3"].season == 1
    assert files["第二季/01.mp3"].season == 2
    assert files["第二季/01.mp3"].episode is None  # 集号留给整理阶段顺排

    book2 = tmp_path / "CD分卷"
    for name in ("CD1", "CD2"):
        d = book2 / name
        d.mkdir(parents=True)
        (d / "a.mp3").write_bytes(b"x")
    books2 = scan_directory(str(tmp_path))
    cd = next(b for b in books2 if b.name == "CD分卷")
    cd_files = {f.relative_path: f for f in cd.files}
    assert cd_files["CD1/a.mp3"].season == 1
    assert cd_files["CD2/a.mp3"].season == 2


def test_scan_flat_podcast_style_filename(tmp_path: Path):
    book = tmp_path / "盗墓笔记"
    book.mkdir()
    (book / "039.第二季.第002集.秦岭神树.mp3").write_bytes(b"x")
    (book / "001.第一季.第001集.开篇.mp3").write_bytes(b"x")
    books = scan_directory(str(tmp_path))
    book_map = {b.name: b for b in books}
    by_name = {Path(f.path).name: f for f in book_map["盗墓笔记"].files}
    assert (by_name["001.第一季.第001集.开篇.mp3"].season, by_name["001.第一季.第001集.开篇.mp3"].episode) == (1, 1)
    assert (by_name["039.第二季.第002集.秦岭神树.mp3"].season, by_name["039.第二季.第002集.秦岭神树.mp3"].episode) == (2, 2)


def test_build_season_bucket_map_prefers_dirname_over_index(tmp_path: Path):
    from audiobookorganizer.scanner import build_season_bucket_map

    book = tmp_path / "书"
    paths = []
    for name in ("第二季", "第三季"):
        d = book / name
        d.mkdir(parents=True)
        p = d / "a.mp3"
        p.write_bytes(b"x")
        paths.append(p)
    mapping = build_season_bucket_map(book, paths)
    assert mapping["第二季"] == 2
    assert mapping["第三季"] == 3


def test_parse_season_ep_prefers_chinese_over_sxxexx_prefix():
    # 已错误整理成 S01E304 后，仍应从「第5季」恢复正确季/集
    season, ep = parse_season_ep_from_stem("S01E304 - 149-第5季-146 酒肆筹备")
    assert season == 5
    assert ep == 146


def test_clean_episode_title_strips_prefixes():
    from audiobookorganizer.scanner import clean_episode_title

    assert clean_episode_title("S01E304 - 149-第5季-146 酒肆筹备") == "酒肆筹备"
    assert clean_episode_title("149-第5季-146 酒肆开业") == "酒肆开业"
    assert clean_episode_title("039.第二季.第002集.秦岭神树") == "秦岭神树"


def test_is_extra_track():
    from audiobookorganizer.scanner import is_extra_track

    assert is_extra_track("【主题曲】少年无恙 - 周笔畅")
    assert is_extra_track("【插曲】一步天涯")
    assert not is_extra_track("第1集 局中挣扎")


def test_clean_book_name():
    assert clean_book_name("三体 128kbps") == "三体"
    assert clean_book_name("活着 [FLAC]") == "活着"


def test_scan_empty_dir(tmp_path: Path):
    assert scan_directory(str(tmp_path)) == []


def test_scan_nonexistent_dir():
    assert scan_directory("/nonexistent/path/xyz") == []
