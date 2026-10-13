from pathlib import Path
import os
from unittest.mock import MagicMock

from audiobookorganizer import AudiobookOrganizer
from audiobookorganizer.models import AudioFile, AudiobookMetadata, BookEntry, FileChange, OrganizePlan


def _plugin(tmp_path: Path) -> AudiobookOrganizer:
    source = tmp_path / "source"
    source.mkdir()
    audio = source / "001.mp3"
    audio.write_bytes(b"audio")
    plugin = AudiobookOrganizer()
    plugin._enabled = True
    plugin._source_path = str(source)
    plugin._target_path = str(tmp_path / "target")
    plugin._monitor_enabled = True
    plugin._monitor_mode = "notify"
    plugin._local_fallback_enabled = False
    plugin._saved = {}
    plugin.save_data = lambda key, value: plugin._saved.__setitem__(key, value)
    plugin.get_data = lambda key: plugin._saved.get(key)
    plugin.post_message = MagicMock()
    return plugin


def test_scheduled_scan_does_not_repeat_identical_pending_notification(tmp_path, monkeypatch):
    import audiobookorganizer as module

    plugin = _plugin(tmp_path)
    source = Path(plugin._source_path)
    audio = source / "001.mp3"
    book = BookEntry("book-1", "三体", source, [AudioFile(audio, "001.mp3")])
    monkeypatch.setattr(module, "scan_directory", lambda _: [book])
    monkeypatch.setattr(plugin, "_search_all", lambda _: [])
    monkeypatch.setattr(module, "resolve_metadata", lambda *args, **kwargs: (AudiobookMetadata(title="三体"), True))

    plugin._scheduled_scan()
    plugin._scheduled_scan()
    stat = audio.stat()
    os.utime(audio, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    plugin._scheduled_scan()

    assert plugin.post_message.call_count == 2


def test_scheduled_auto_organize_skips_unchanged_outputs(tmp_path, monkeypatch):
    import audiobookorganizer as module

    plugin = _plugin(tmp_path)
    plugin._monitor_mode = "auto"
    plugin._local_fallback_enabled = True
    source = Path(plugin._source_path)
    audio = source / "001.mp3"
    book = BookEntry("book-1", "三体", source, [AudioFile(audio, "001.mp3")])
    target = Path(plugin._target_path) / "三体.mp3"
    plan = OrganizePlan("plan-1", "book-1", "三体", AudiobookMetadata(title="三体"), changes=[FileChange(str(audio), str(target))])
    calls = {"cleanup": 0, "apply": 0}

    monkeypatch.setattr(module, "scan_directory", lambda _: [book])
    monkeypatch.setattr(plugin, "_search_all", lambda _: [])
    monkeypatch.setattr(module, "resolve_metadata", lambda *args, **kwargs: (AudiobookMetadata(title="三体"), True))
    monkeypatch.setattr(module, "compute_confidence", lambda *args: 1.0)
    monkeypatch.setattr(module, "preview_plan", lambda *args, **kwargs: plan)

    def cleanup(*args, **kwargs):
        calls["cleanup"] += 1
        return {"deleted_count": 0}

    def apply(*args, **kwargs):
        calls["apply"] += 1
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"output")
        return {"success": [{"target": str(target)}], "errors": [], "skipped": []}

    monkeypatch.setattr(plugin, "_cleanup_before_organize", cleanup)
    monkeypatch.setattr(module, "apply_plan", apply)

    plugin._scheduled_scan()
    plugin._scheduled_scan()
    assert calls == {"cleanup": 1, "apply": 1}
    assert target.read_bytes() == b"output"

    target.write_bytes(b"tampered-output")
    plugin._scheduled_scan()

    assert calls == {"cleanup": 2, "apply": 2}
    assert target.read_bytes() == b"output"


def test_cleanup_tombstone_prevents_stale_manifest_redeleting_reused_path(tmp_path, monkeypatch):
    import audiobookorganizer as module

    plugin = _plugin(tmp_path)
    source = Path(plugin._source_path)
    target = Path(plugin._target_path) / "三体.mp3"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"old-output")
    plugin._saved["last_organize_targets"] = {
        "book-1": {"signature": "old-signature", "targets": [str(target)], "cover_path": ""}
    }
    plugin._saved["organize_history"] = [{"book_id": "book-1", "targets": [str(target)]}]
    book = BookEntry("book-1", "三体", source, [AudioFile(source / "001.mp3", "001.mp3")])
    cleanup_targets = []

    def cleanup_previous_outputs(**kwargs):
        targets = kwargs["previous_targets"]
        cleanup_targets.append(list(targets))
        for path in targets:
            Path(path).unlink(missing_ok=True)
        return {"deleted": list(targets), "deleted_count": len(targets)}

    monkeypatch.setattr(module, "cleanup_previous_outputs", cleanup_previous_outputs)

    plugin._cleanup_before_organize(book, Path(plugin._target_path))
    plugin._invalidate_organization_signature(book.book_id)
    target.write_bytes(b"new-unrelated-output")
    plugin._cleanup_before_organize(book, Path(plugin._target_path))

    assert cleanup_targets == [[str(target)], []]
    assert target.read_bytes() == b"new-unrelated-output"


def test_pending_signature_clears_when_books_disappear(tmp_path, monkeypatch):
    import audiobookorganizer as module

    plugin = _plugin(tmp_path)
    source = Path(plugin._source_path)
    book = BookEntry("book-1", "三体", source, [AudioFile(source / "001.mp3", "001.mp3")])
    books = [book]
    monkeypatch.setattr(module, "scan_directory", lambda _: list(books))
    monkeypatch.setattr(plugin, "_search_all", lambda _: [])
    monkeypatch.setattr(module, "resolve_metadata", lambda *args, **kwargs: (AudiobookMetadata(title="三体"), True))

    plugin._scheduled_scan()
    books.clear()
    plugin._scheduled_scan()
    assert plugin._saved["pending_notification_signatures"] == {}
    books.append(book)
    plugin._scheduled_scan()

    assert plugin.post_message.call_count == 2


def test_failed_cleanup_preserves_owned_manifest_and_skips_apply(tmp_path, monkeypatch):
    import audiobookorganizer as module

    plugin = _plugin(tmp_path)
    plugin._monitor_mode = "auto"
    plugin._local_fallback_enabled = True
    source = Path(plugin._source_path)
    audio = source / "001.mp3"
    book = BookEntry("book-1", "三体", source, [AudioFile(audio, "001.mp3")])
    old_target = Path(plugin._target_path) / "old-output.mp3"
    manifest = {"signature": "old", "targets": [str(old_target)], "cover_path": ""}
    plugin._saved["last_organize_targets"] = {"book-1": manifest.copy()}
    plan = OrganizePlan(
        "cleanup-fails", "book-1", "三体", AudiobookMetadata(title="三体"),
        changes=[FileChange(str(audio), str(Path(plugin._target_path) / "new.mp3"))],
    )
    calls = {"apply": 0}

    monkeypatch.setattr(module, "scan_directory", lambda _: [book])
    monkeypatch.setattr(plugin, "_search_all", lambda _: [])
    monkeypatch.setattr(module, "resolve_metadata", lambda *args, **kwargs: (AudiobookMetadata(title="三体"), True))
    monkeypatch.setattr(module, "compute_confidence", lambda *args: 1.0)
    monkeypatch.setattr(module, "preview_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(plugin, "_cleanup_before_organize", lambda *args, **kwargs: {
        "deleted": [], "deleted_count": 0, "errors": ["permission denied"]
    })

    def apply(*args, **kwargs):
        calls["apply"] += 1
        return {"success": [], "errors": [], "skipped": []}

    monkeypatch.setattr(module, "apply_plan", apply)
    plugin._scheduled_scan()

    assert calls["apply"] == 0
    assert plugin._saved["last_organize_targets"]["book-1"] == manifest


def test_failed_auto_organize_does_not_persist_success_signature(tmp_path, monkeypatch):
    import audiobookorganizer as module

    plugin = _plugin(tmp_path)
    plugin._monitor_mode = "auto"
    plugin._local_fallback_enabled = True
    source = Path(plugin._source_path)
    audio = source / "001.mp3"
    book = BookEntry("book-1", "三体", source, [AudioFile(audio, "001.mp3")])
    target = Path(plugin._target_path) / "三体.mp3"
    plan = OrganizePlan("failed-plan", "book-1", "三体", AudiobookMetadata(title="三体"), changes=[FileChange(str(audio), str(target))])
    calls = {"apply": 0}

    monkeypatch.setattr(module, "scan_directory", lambda _: [book])
    monkeypatch.setattr(plugin, "_search_all", lambda _: [])
    monkeypatch.setattr(module, "resolve_metadata", lambda *args, **kwargs: (AudiobookMetadata(title="三体"), True))
    monkeypatch.setattr(module, "compute_confidence", lambda *args: 1.0)
    monkeypatch.setattr(module, "preview_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(plugin, "_cleanup_before_organize", lambda *args, **kwargs: {"deleted_count": 0})

    def fail(*args, **kwargs):
        calls["apply"] += 1
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"stale")
        return {"success": [], "errors": [{"target": str(target), "error": "write failed"}], "skipped": []}

    monkeypatch.setattr(module, "apply_plan", fail)
    plugin._scheduled_scan()
    plugin._scheduled_scan()

    signature = plugin._organization_signature(book, AudiobookMetadata(title="三体"))
    assert calls["apply"] == 2
    assert not plugin._outputs_match_signature("book-1", signature)
