"""命名模板引擎测试。"""

import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins.v2"
sys.path.insert(0, str(PLUGIN_DIR))

from audiobookorganizer.namer import build_file_path, render_template, sanitize_name  # noqa: E402


def test_sanitize_name_removes_invalid_chars():
    assert sanitize_name('test<>:"/\\|?*name') == "test_________name"
    assert sanitize_name("  hello  ") == "hello"


def test_render_template_basic():
    result = render_template(
        "{author}/{title}/E{episode:02d}{ext}",
        {"author": "刘慈欣", "title": "三体", "episode": 1, "ext": ".mp3"},
    )
    assert result == "刘慈欣/三体/E01.mp3"


def test_build_file_path():
    target = build_file_path(
        "{author}/{title}/S{season:02d}E{episode:02d} - {episode_title}{ext}",
        Path("/tmp/out"),
        author="作者",
        title="书名",
        season=1,
        episode=2,
        episode_title="第二章",
        ext=".mp3",
    )
    assert target == Path("/tmp/out/作者/书名/S01E02 - 第二章.mp3")


def test_render_template_missing_var():
    result = render_template("{author}/{unknown}", {"author": "test"})
    assert "test" in result
