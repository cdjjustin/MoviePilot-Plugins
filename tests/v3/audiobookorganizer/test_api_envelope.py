"""验证 V3 详情页 API 必须返回宿主严格三段式 envelope。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from audiobookorganizer import AudiobookOrganizer
from audiobookorganizer import _safe_log_text
from audiobookorganizer.models import AudioFile, BookEntry


def test_safe_log_text_removes_control_characters():
    cleaned = _safe_log_text("book\n\r\t\x1b[2J\x7f")

    assert "\n" not in cleaned
    assert "\r" not in cleaned
    assert "\t" not in cleaned
    assert "\x1b" not in cleaned
    assert "\x7f" not in cleaned


def _is_strict_envelope(payload: dict) -> bool:
    keys = set(payload)
    return keys == {"success", "message", "data"} and isinstance(payload["success"], bool) and isinstance(
        payload["message"], str
    )


def _as_dict(resp) -> dict:
    return {"success": resp.success, "message": resp.message, "data": resp.data}


@pytest.fixture
def plugin(tmp_path: Path) -> AudiobookOrganizer:
    book_dir = tmp_path / "三体"
    book_dir.mkdir()
    (book_dir / "001.mp3").write_bytes(b"x")

    p = AudiobookOrganizer()
    p._enabled = True
    p._source_path = str(tmp_path)
    p._target_path = str(tmp_path / "out")
    p._saved = {}
    p.save_data = lambda key, val: p._saved.__setitem__(key, val)
    p.get_data = lambda key: p._saved.get(key)
    return p


def test_api_scan_returns_strict_envelope(plugin: AudiobookOrganizer):
    resp = plugin.api_scan()
    payload = _as_dict(resp)
    assert _is_strict_envelope(payload)
    assert payload["success"] is True
    assert payload["data"]["total"] == 1
    assert payload["data"]["books"][0]["name"] == "三体"
    assert "扫描完成" in payload["message"]


def test_get_page_renders_book_list_items(plugin: AudiobookOrganizer):
    book = BookEntry(
        book_id="b1",
        name="三体",
        path=Path("/tmp/三体"),
        files=[AudioFile(path=Path("/tmp/三体/1.mp3"), relative_path="1.mp3")],
    )
    plugin._saved["last_scan"] = {
        "time": "t",
        "count": 1,
        "books": [book.to_dict()],
    }
    page = plugin.get_page()

    def find(nodes, name):
        found = []
        if isinstance(nodes, dict):
            if nodes.get("component") == name:
                found.append(nodes)
            for v in nodes.values():
                found.extend(find(v, name))
        elif isinstance(nodes, list):
            for n in nodes:
                found.extend(find(n, name))
        return found

    assert find(page, "VDataTable") == []
    buttons = find(page, "VBtn")
    labels = [b.get("text") for b in buttons]
    assert "刮削整理" in labels
    assert "本地整理" in labels
    assert "全部本地整理（1）" in labels
    organize = next(b for b in buttons if b.get("text") == "刮削整理")
    assert organize["events"]["click"]["params"]["book_id"] == "b1"
    assert organize["events"]["click"]["params"]["mode"] == "scrape"


def test_api_organize_local(plugin: AudiobookOrganizer, tmp_path: Path, monkeypatch):
    book_dir = tmp_path / "三体"
    out = tmp_path / "out"
    out.mkdir()
    # rebuild plugin paths against real files from fixture
    plugin._target_path = str(out)
    books = __import__("audiobookorganizer.scanner", fromlist=["scan_directory"]).scan_directory(str(tmp_path))
    assert books
    plugin._books_cache = books
    plugin._saved["last_scan"] = {
        "time": "t",
        "count": len(books),
        "books": [b.to_dict() for b in books],
    }

    resp = plugin.api_organize(book_id=books[0].book_id, mode="local")
    assert resp.success is True
    assert resp.data["ok"] is True
    assert resp.data["local"] is True
    assert plugin._saved["last_scan"]["books"][0]["status"] == "organized"
    assert plugin._saved.get("organize_history")
