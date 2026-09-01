"""目录扫描测试。"""

import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins.v2"
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


def test_clean_book_name():
    assert clean_book_name("三体 128kbps") == "三体"
    assert clean_book_name("活着 [FLAC]") == "活着"


def test_scan_empty_dir(tmp_path: Path):
    assert scan_directory(str(tmp_path)) == []


def test_scan_nonexistent_dir():
    assert scan_directory("/nonexistent/path/xyz") == []
