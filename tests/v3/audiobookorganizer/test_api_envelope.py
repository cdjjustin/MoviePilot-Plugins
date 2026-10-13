"""验证 V3 详情页 API 必须返回宿主严格三段式 envelope。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from audiobookorganizer import AudiobookOrganizer
from audiobookorganizer import _safe_log_text
from audiobookorganizer.models import AudioFile, BookEntry, SearchResult
from fastapi import HTTPException


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
    p._candidate_cache = {}
    p.save_data = lambda key, val: p._saved.__setitem__(key, val)
    p.get_data = lambda key: p._saved.get(key)
    return p


def test_selection_clear_invalidates_preview_plan(plugin: AudiobookOrganizer):
    plugin._plans_cache = {"plan-1": object()}
    plugin._candidate_cache = {"candidate-1": ("book-1", object(), datetime.now(timezone.utc))}
    plugin.__dict__["_candidate_selection"] = {"book-1": [{"candidate_id": "candidate-1"}]}
    plugin.__dict__["_active_preview"] = {"book-1": {"plan_id": "plan-1"}}

    response = plugin.api_selection_clear("book-1")

    assert response.success
    assert "plan-1" not in plugin._plans_cache
    assert "candidate-1" not in plugin._candidate_cache
    assert "book-1" not in plugin.__dict__["_active_preview"]
    assert "book-1" not in plugin.__dict__["_candidate_selection"]


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
    assert "选择刮削版本" in labels
    assert "本地整理" in labels
    assert "全部本地整理（1）" in labels
    organize = next(b for b in buttons if b.get("text") == "选择刮削版本")
    assert organize["events"]["click"]["api"] == "plugin/AudiobookOrganizer/candidates"
    assert organize["events"]["click"]["params"]["book_id"] == "b1"


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


def test_api_preview_requires_explicit_source_confirmation(plugin: AudiobookOrganizer):
    plugin.api_scan()

    with pytest.raises(HTTPException) as exc:
        plugin.api_preview({
            "book_id": plugin._books_cache[0].book_id,
            "source": "ximalaya",
            "source_id": "9724463",
        })

    assert exc.value.status_code == 400
    assert "confirm_source_id" in str(exc.value.detail)


@pytest.mark.parametrize(
    "body",
    [
        {"source_id": " 9724463 ", "confirm_source_id": "9724463"},
        {"source_id": ["9724463"], "confirm_source_id": ["9724463"]},
        {"source_id": "9724463", "confirm_source_id": ["9724463"]},
    ],
)
def test_api_preview_rejects_non_exact_or_malformed_source_confirmation(plugin: AudiobookOrganizer, body: dict):
    plugin.api_scan()
    body["book_id"] = plugin._books_cache[0].book_id

    with pytest.raises(HTTPException) as exc:
        plugin.api_preview(body)

    assert exc.value.status_code == 400


def test_api_preview_passes_exact_source_id_unchanged(plugin: AudiobookOrganizer, monkeypatch):
    plugin.api_scan()
    seen = []

    def fetch_metadata(source, source_id):
        seen.append((source, source_id))
        from audiobookorganizer.models import AudiobookMetadata

        return AudiobookMetadata(title="三体", source=source, source_id=source_id)

    monkeypatch.setattr(plugin, "_fetch_metadata", fetch_metadata)
    response = plugin.api_preview({
        "book_id": plugin._books_cache[0].book_id,
        "source": "ximalaya",
        "source_id": " 9724463 ",
        "confirm_source_id": " 9724463 ",
    })

    assert response.success is True
    assert seen == [("ximalaya", " 9724463 ")]


def test_api_candidates_searches_by_book_and_returns_selectable_ids(plugin: AudiobookOrganizer, monkeypatch):
    plugin.api_scan()
    monkeypatch.setattr(
        plugin,
        "_search_all",
        lambda keyword: [
            SearchResult(
                source="ximalaya",
                source_id="9724463",
                title="《剑来》上",
                author="烽火戏诸侯",
                narrator="大斌",
            )
        ],
    )

    plugin._candidate_cache["expired"] = (
        "old-book",
        SearchResult(source="ximalaya", source_id="old", title="旧"),
        datetime.now(timezone.utc) - timedelta(seconds=901),
    )
    response = plugin.api_candidates(plugin._books_cache[0].book_id)

    assert "expired" not in plugin._candidate_cache
    assert response.data["keyword"] == "三体"
    assert len(response.data["candidates"]) == 1
    candidate = response.data["candidates"][0]
    assert candidate["candidate_id"]
    assert candidate["source_id"] == "9724463"

    with pytest.raises(HTTPException) as exc:
        plugin.api_preview({
            "book_id": plugin._books_cache[0].book_id,
            "candidate_id": candidate["candidate_id"],
            "metadata": {"title": "伪造"},
        })
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as mismatch:
        plugin.api_preview({"book_id": "another-book", "candidate_id": candidate["candidate_id"]})
    assert mismatch.value.status_code == 400
    assert candidate["candidate_id"] in plugin._candidate_cache

    seen = []
    monkeypatch.setattr(
        plugin,
        "_fetch_metadata",
        lambda source, source_id: seen.append((source, source_id))
        or __import__("audiobookorganizer.models", fromlist=["AudiobookMetadata"]).AudiobookMetadata(
            title="三体", source=source, source_id=source_id
        ),
    )
    preview = plugin.api_preview({
        "book_id": plugin._books_cache[0].book_id,
        "candidate_id": candidate["candidate_id"],
    })
    assert preview.success is True
    assert seen == [("ximalaya", "9724463")]


def test_candidate_search_opens_dialog_with_selectable_versions(plugin: AudiobookOrganizer, monkeypatch):
    plugin.api_scan()
    monkeypatch.setattr(
        plugin,
        "_search_all",
        lambda keyword: [
            SearchResult(
                source="ximalaya",
                source_id="9724463",
                title="盗墓笔记丨周建龙经典版",
                author="南派三叔",
                narrator="周建龙",
                track_count=80,
            )
        ],
    )
    book_id = plugin._books_cache[0].book_id
    plugin.api_candidates(book_id)
    page = plugin.get_page()

    def find(nodes, component):
        found = []
        if isinstance(nodes, dict):
            if nodes.get("component") == component:
                found.append(nodes)
            for value in nodes.values():
                found.extend(find(value, component))
        elif isinstance(nodes, list):
            for node in nodes:
                found.extend(find(node, component))
        return found

    dialogs = find(page, "VDialog")
    assert len(dialogs) == 1
    assert dialogs[0]["props"]["modelValue"] is True
    buttons = find(dialogs[0], "VBtn")
    choose = next(button for button in buttons if button.get("text") == "选择此版本")
    assert "盗墓笔记丨周建龙经典版" in str(dialogs[0])
    assert choose["events"]["click"] == {
        "api": "plugin/AudiobookOrganizer/preview",
        "method": "post",
        "params": {
            "book_id": book_id,
            "candidate_id": choose["events"]["click"]["params"]["candidate_id"],
        },
    }
    assert choose["events"]["click"]["params"]["candidate_id"]


def test_candidate_preview_shows_plan_confirmation(plugin: AudiobookOrganizer, monkeypatch):
    from audiobookorganizer.models import AudiobookMetadata

    plugin.api_scan()
    monkeypatch.setattr(
        plugin,
        "_search_all",
        lambda keyword: [SearchResult(source="ximalaya", source_id="album-1", title="三体·周建龙", author="刘慈欣", narrator="周建龙")],
    )
    monkeypatch.setattr(
        plugin,
        "_fetch_metadata",
        lambda source, source_id: AudiobookMetadata(title="三体", author="刘慈欣", narrator="周建龙", source=source, source_id=source_id),
    )
    book_id = plugin._books_cache[0].book_id
    candidate = plugin.api_candidates(book_id).data["candidates"][0]
    plan = plugin.api_preview({"book_id": book_id, "candidate_id": candidate["candidate_id"]}).data
    page = plugin.get_page()

    def find(nodes, component):
        if isinstance(nodes, dict):
            matches = [nodes] if nodes.get("component") == component else []
            return matches + [match for value in nodes.values() for match in find(value, component)]
        if isinstance(nodes, list):
            return [match for node in nodes for match in find(node, component)]
        return []

    dialogs = find(page, "VDialog")
    assert any("整理预览" in str(dialog) for dialog in dialogs)
    apply_button = next(button for dialog in dialogs for button in find(dialog, "VBtn") if button.get("text") == "确认并整理")
    assert apply_button["events"]["click"]["api"] == "plugin/AudiobookOrganizer/apply"
    assert apply_button["events"]["click"]["params"]["plan_ids"] == [plan["plan_id"]]


def test_partial_apply_keeps_preview_and_reports_file_failures(plugin: AudiobookOrganizer, monkeypatch):
    from audiobookorganizer.models import AudiobookMetadata
    import audiobookorganizer as module

    plugin.api_scan()
    monkeypatch.setattr(
        plugin,
        "_search_all",
        lambda keyword: [SearchResult(source="ximalaya", source_id="album-1", title="三体·周建龙")],
    )
    monkeypatch.setattr(
        plugin,
        "_fetch_metadata",
        lambda source, source_id: AudiobookMetadata(title="三体", source=source, source_id=source_id),
    )
    book_id = plugin._books_cache[0].book_id
    candidate = plugin.api_candidates(book_id).data["candidates"][0]
    plan = plugin.api_preview({"book_id": book_id, "candidate_id": candidate["candidate_id"]}).data
    monkeypatch.setattr(module, "apply_plan", lambda *args, **kwargs: {
        "success": [],
        "skipped": [],
        "errors": [{"source": "001.mp3", "error": "写入失败"}],
    })

    response = plugin.api_apply({"plan_ids": [plan["plan_id"]]})

    assert response.message == "整理完成：成功 0 个文件，失败 1 个文件/计划"
    assert response.data["errors"][0]["plan_id"] == plan["plan_id"]
    assert response.data["errors"][0]["result"]["errors"] == [{"source": "001.mp3", "error": "写入失败"}]
    assert plugin.__dict__["_active_preview"][book_id]["plan_id"] == plan["plan_id"]
    assert "整理预览" in str(plugin.get_page())


def test_api_apply_rejects_duplicate_success_targets(plugin: AudiobookOrganizer, monkeypatch):
    from audiobookorganizer.models import AudiobookMetadata, FileChange, OrganizePlan
    import audiobookorganizer as module

    plugin.api_scan()
    book = plugin._books_cache[0]
    metadata = AudiobookMetadata(title="三体")
    sources = [str(book.files[0].path), str(book.files[0].path.with_name("002.mp3"))]
    targets = [str(Path(plugin._target_path) / "001.mp3"), str(Path(plugin._target_path) / "002.mp3")]
    plan = OrganizePlan(
        "duplicate-success", book.book_id, book.name, metadata,
        changes=[FileChange(sources[0], targets[0]), FileChange(sources[1], targets[1])],
    )
    monkeypatch.setattr(plugin, "_search_all", lambda _: [SearchResult(source="ximalaya", source_id="id", title="三体")])
    monkeypatch.setattr(plugin, "_fetch_metadata", lambda *_: metadata)
    monkeypatch.setattr(module, "preview_plan", lambda *args, **kwargs: plan)
    candidate = plugin.api_candidates(book.book_id).data["candidates"][0]
    preview = plugin.api_preview({"book_id": book.book_id, "candidate_id": candidate["candidate_id"]}).data
    active_plan = plugin._plans_cache[preview["plan_id"]]
    active_plan.changes = plan.changes
    plugin.__dict__["_active_preview"][book.book_id]["changes"] = [change.to_dict() for change in plan.changes]

    for target in targets:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(b"audio")
    monkeypatch.setattr(module, "apply_plan", lambda *args, **kwargs: {
        "success": [{"source": sources[0], "target": targets[0]}, {"source": sources[0], "target": targets[0]}],
        "skipped": [],
        "errors": [],
    })

    response = plugin.api_apply({"plan_ids": [preview["plan_id"]]})

    assert not response.data["applied"]
    assert book.book_id in plugin.__dict__["_active_preview"]
    assert [change.target for change in active_plan.changes] == [targets[1]]


def test_apply_keeps_success_pair_if_same_source_or_target_has_error(plugin: AudiobookOrganizer, monkeypatch):
    from audiobookorganizer.models import AudiobookMetadata, FileChange, OrganizePlan
    import audiobookorganizer as module

    plugin.api_scan()
    book = plugin._books_cache[0]
    source = str(book.files[0].path)
    target_error = str(Path(plugin._target_path) / "001.mp3")
    target_success = str(Path(plugin._target_path) / "002.mp3")
    plan = OrganizePlan(
        "error-and-success", book.book_id, book.name, AudiobookMetadata(title="三体"),
        changes=[FileChange(source, target_error), FileChange(source, target_success)],
    )
    monkeypatch.setattr(plugin, "_search_all", lambda _: [SearchResult(source="ximalaya", source_id="id", title="三体")])
    monkeypatch.setattr(plugin, "_fetch_metadata", lambda *_: plan.metadata)
    monkeypatch.setattr(module, "preview_plan", lambda *args, **kwargs: plan)
    candidate = plugin.api_candidates(book.book_id).data["candidates"][0]
    preview = plugin.api_preview({"book_id": book.book_id, "candidate_id": candidate["candidate_id"]}).data
    active_plan = plugin._plans_cache[preview["plan_id"]]
    active_plan.changes = plan.changes
    plugin.__dict__["_active_preview"][book.book_id]["changes"] = [change.to_dict() for change in plan.changes]
    Path(target_success).parent.mkdir(parents=True, exist_ok=True)
    Path(target_success).write_bytes(b"new-output")
    monkeypatch.setattr(module, "apply_plan", lambda *args, **kwargs: {
        "success": [{"source": source, "target": target_success}],
        "skipped": [],
        "errors": [{"source": source, "target": target_error, "error": "tag write failed"}],
    })

    plugin.api_apply({"plan_ids": [preview["plan_id"]]})

    assert [(change.source, change.target) for change in active_plan.changes] == [(source, target_error)]


def test_output_integrity_failure_keeps_only_missing_change_for_retry(plugin: AudiobookOrganizer, monkeypatch):
    from audiobookorganizer.models import AudiobookMetadata, FileChange, OrganizePlan
    import audiobookorganizer as module

    plugin.api_scan()
    book = plugin._books_cache[0]
    metadata = AudiobookMetadata(title="三体")
    sources = [str(book.files[0].path), str(book.files[0].path.with_name("002.mp3"))]
    targets = [str(Path(plugin._target_path) / "001.mp3"), str(Path(plugin._target_path) / "002.mp3")]
    plan = OrganizePlan(
        "missing-output", book.book_id, book.name, metadata,
        changes=[FileChange(sources[i], targets[i]) for i in range(2)],
    )
    monkeypatch.setattr(plugin, "_search_all", lambda _: [SearchResult(source="ximalaya", source_id="id", title="三体")])
    monkeypatch.setattr(plugin, "_fetch_metadata", lambda *_: metadata)
    monkeypatch.setattr(module, "preview_plan", lambda *args, **kwargs: plan)
    candidate = plugin.api_candidates(book.book_id).data["candidates"][0]
    preview = plugin.api_preview({"book_id": book.book_id, "candidate_id": candidate["candidate_id"]}).data
    active_plan = plugin._plans_cache[preview["plan_id"]]
    active_plan.changes = plan.changes
    plugin.__dict__["_active_preview"][book.book_id]["changes"] = [change.to_dict() for change in plan.changes]
    Path(targets[0]).parent.mkdir(parents=True, exist_ok=True)
    Path(targets[0]).write_bytes(b"audio-1")
    monkeypatch.setattr(module, "apply_plan", lambda *args, **kwargs: {
        "success": [{"source": sources[i], "target": targets[i]} for i in range(2)],
        "skipped": [],
        "errors": [],
    })

    plugin.api_apply({"plan_ids": [preview["plan_id"]]})

    assert [(change.source, change.target) for change in active_plan.changes] == [(sources[1], targets[1])]


def test_api_apply_rejects_multiple_sources_for_one_target(plugin: AudiobookOrganizer, monkeypatch):
    from audiobookorganizer.models import AudiobookMetadata, FileChange, OrganizePlan
    import audiobookorganizer as module

    plugin.api_scan()
    book = plugin._books_cache[0]
    metadata = AudiobookMetadata(title="三体")
    source1 = str(book.files[0].path)
    source2 = str(book.files[0].path.with_name("002.mp3"))
    target = str(Path(plugin._target_path) / "shared.mp3")
    plan = OrganizePlan(
        "shared-target", book.book_id, book.name, metadata,
        changes=[FileChange(source1, target), FileChange(source2, target)],
    )
    monkeypatch.setattr(plugin, "_search_all", lambda _: [SearchResult(source="ximalaya", source_id="id", title="三体")])
    monkeypatch.setattr(plugin, "_fetch_metadata", lambda *_: metadata)
    monkeypatch.setattr(module, "preview_plan", lambda *args, **kwargs: plan)
    candidate = plugin.api_candidates(book.book_id).data["candidates"][0]
    preview = plugin.api_preview({"book_id": book.book_id, "candidate_id": candidate["candidate_id"]}).data
    active_plan = plugin._plans_cache[preview["plan_id"]]
    active_plan.changes = plan.changes
    plugin.__dict__["_active_preview"][book.book_id]["changes"] = [change.to_dict() for change in plan.changes]
    apply_calls = []

    def apply(*args, **kwargs):
        apply_calls.append(1)
        return {"success": [], "skipped": [], "errors": []}

    monkeypatch.setattr(module, "apply_plan", apply)

    response = plugin.api_apply({"plan_ids": [preview["plan_id"]]})

    assert not apply_calls
    assert len(response.data["errors"]) == 1
    assert [(change.source, change.target) for change in active_plan.changes] == [(source1, target), (source2, target)]


def test_cover_only_failure_remains_retryable_until_cover_exists(
    plugin: AudiobookOrganizer, tmp_path: Path, monkeypatch
):
    from audiobookorganizer.models import AudiobookMetadata, FileChange, OrganizePlan
    import audiobookorganizer as module

    cover_path = tmp_path / "out" / "book" / "cover.jpg"
    plan = OrganizePlan(
        "cover-retry", "book-id", "book", AudiobookMetadata(title="book", cover_url="https://example.invalid/cover.jpg"),
        changes=[FileChange(str(tmp_path / "source.mp3"), str(tmp_path / "out" / "audio.mp3"))],
        cover_path=str(cover_path),
    )
    plugin._plans_cache = {plan.plan_id: plan}
    plugin.__dict__["_active_preview"] = {
        plan.book_id: {"plan_id": plan.plan_id, "changes": [change.to_dict() for change in plan.changes]}
    }
    calls = []

    def apply(partial_plan, **kwargs):
        calls.append(len(partial_plan.changes))
        if len(calls) == 1:
            target = Path(partial_plan.changes[0].target)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"audio")
            return {
                "success": [{"source": partial_plan.changes[0].source, "target": partial_plan.changes[0].target}],
                "skipped": [],
                "errors": [{"cover": partial_plan.cover_path, "error": "write failed"}],
            }
        if len(calls) == 3:
            cover_path.parent.mkdir(parents=True, exist_ok=True)
            cover_path.write_bytes(b"cover")
        return {"success": [], "skipped": [], "errors": []}

    monkeypatch.setattr(module, "apply_plan", apply)

    first = plugin.api_apply({"plan_ids": [plan.plan_id]})
    assert plan.book_id in plugin.__dict__["_active_preview"]
    assert plugin._plans_cache[plan.plan_id].changes == []

    second = plugin.api_apply({"plan_ids": [plan.plan_id]})
    assert plan.book_id in plugin.__dict__["_active_preview"]
    assert second.data["errors"][0]["result"]["errors"][0]["cover"] == str(cover_path)

    third = plugin.api_apply({"plan_ids": [plan.plan_id]})
    assert plan.book_id not in plugin.__dict__["_active_preview"]
    assert calls == [1, 0, 0]
    assert third.data["applied"]



@pytest.mark.parametrize("organize_mode", ["hardlink", "copy", "move"])
def test_real_apply_plan_retries_failed_cover_save(
    plugin: AudiobookOrganizer, tmp_path: Path, monkeypatch, organize_mode: str
):
    import audiobookorganizer.organizer as organizer_module
    from audiobookorganizer.models import AudiobookMetadata, FileChange, OrganizePlan

    plugin.api_scan()
    plugin._organize_mode = organize_mode
    book = plugin._books_cache[0]
    source = str(book.files[0].path)
    target = str(Path(plugin._target_path) / "book" / "001.mp3")
    cover_path = Path(plugin._target_path) / "book" / "cover.jpg"
    metadata = AudiobookMetadata(title="book", cover_url="https://example.invalid/cover.jpg")
    plan = OrganizePlan(
        "real-cover-retry", book.book_id, book.name, metadata,
        changes=[FileChange(source, target)], cover_path=str(cover_path),
    )
    plugin._plans_cache = {plan.plan_id: plan}
    plugin.__dict__["_active_preview"] = {
        book.book_id: {"plan_id": plan.plan_id, "changes": [change.to_dict() for change in plan.changes]}
    }
    monkeypatch.setattr(organizer_module, "_download_cover", lambda _: b"cover-bytes")
    original_save_cover = organizer_module.save_cover
    save_calls = []

    def fail_once(data, directory):
        save_calls.append(1)
        if len(save_calls) == 1:
            raise OSError("temporary cover write failure")
        return original_save_cover(data, directory)

    monkeypatch.setattr(organizer_module, "save_cover", fail_once)

    first = plugin.api_apply({"plan_ids": [plan.plan_id]})
    assert first.data["errors"]
    assert book.book_id in plugin.__dict__["_active_preview"]
    assert plugin._plans_cache[plan.plan_id].changes == []
    assert Path(target).is_file()

    second = plugin.api_apply({"plan_ids": [plan.plan_id]})
    assert not second.data["errors"]
    assert cover_path.is_file()
    assert book.book_id not in plugin.__dict__["_active_preview"]
    if organize_mode == "hardlink":
        assert Path(source).stat().st_ino == Path(target).stat().st_ino
    elif organize_mode == "copy":
        assert Path(source).is_file()
        assert Path(source).stat().st_ino != Path(target).stat().st_ino
    else:
        assert not Path(source).exists()


@pytest.mark.parametrize("organize_mode", ["hardlink", "copy", "move"])
def test_partial_apply_retry_only_retries_unfinished_files(plugin: AudiobookOrganizer, monkeypatch, organize_mode):
    from audiobookorganizer.models import AudiobookMetadata, FileChange, OrganizePlan
    import audiobookorganizer as module

    plugin.api_scan()
    plugin._organize_mode = organize_mode
    book = plugin._books_cache[0]
    metadata = AudiobookMetadata(title="三体", source="ximalaya", source_id="album-1")
    source1 = str(book.files[0].path)
    source2 = str(book.files[0].path.with_name("002.mp3"))
    target1 = str(Path(plugin._target_path) / "001.mp3")
    target2 = str(Path(plugin._target_path) / "002.mp3")
    planned = OrganizePlan(
        "retry-plan", book.book_id, book.name, metadata,
        changes=[FileChange(source1, target1), FileChange(source2, target2)],
    )
    monkeypatch.setattr(plugin, "_search_all", lambda _: [SearchResult(source="ximalaya", source_id="album-1", title="三体")])
    monkeypatch.setattr(plugin, "_fetch_metadata", lambda source, source_id: metadata)
    monkeypatch.setattr(module, "preview_plan", lambda *args, **kwargs: planned)
    candidate = plugin.api_candidates(book.book_id).data["candidates"][0]
    plan_data = plugin.api_preview({"book_id": book.book_id, "candidate_id": candidate["candidate_id"]}).data
    plan = plugin._plans_cache[plan_data["plan_id"]]
    plan.changes = [FileChange(source1, target1), FileChange(source2, target2)]
    plugin.__dict__["_active_preview"][book.book_id]["changes"] = [
        change.to_dict() for change in plan.changes
    ]
    calls = []

    def apply(partial_plan, **kwargs):
        sources = [change.source for change in partial_plan.changes]
        calls.append(sources)
        if len(calls) == 1:
            Path(target1).parent.mkdir(parents=True, exist_ok=True)
            Path(target1).write_bytes(b"audio-1")
            return {
                "success": [{"source": source1, "target": target1}],
                "skipped": [],
                "errors": [{"source": source2, "error": "transient failure"}],
            }
        Path(target2).parent.mkdir(parents=True, exist_ok=True)
        Path(target2).write_bytes(b"audio-2")
        return {
            "success": [{"source": source2, "target": target2}],
            "skipped": [],
            "errors": [],
        }

    monkeypatch.setattr(module, "apply_plan", apply)
    first = plugin.api_apply({"plan_ids": [planned.plan_id]})
    assert first.message == "整理完成：成功 1 个文件，失败 1 个文件/计划"
    assert calls[0] == [source1, source2]
    assert [change.source for change in plugin._plans_cache[planned.plan_id].changes] == [source2]
    assert plugin.__dict__["_active_preview"][book.book_id]["plan_id"] == planned.plan_id

    second = plugin.api_apply({"plan_ids": [planned.plan_id]})

    assert second.message == "整理完成：成功 1 个文件，失败 0 个文件/计划"
    assert calls == [[source1, source2], [source2]]
    assert book.book_id not in plugin.__dict__["_active_preview"]
