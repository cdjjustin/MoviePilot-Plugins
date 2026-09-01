"""Apple Podcasts enclosure serving: HEAD, Range, MIME."""

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins.v2" / "audiobookpodcast"
sys.path.insert(0, str(PLUGIN_DIR))

from media_http import (  # noqa: E402
    APPLE_AUDIO_MIME,
    check_apikey,
    media_headers,
    media_type_for,
    serve_media_file,
)


@pytest.fixture()
def mp3_file(tmp_path: Path) -> Path:
    payload = b"ID3" + b"\x00" * 2048 + b"\xff\xfb" + b"\x11" * 4096
    path = tmp_path / "ep01.mp3"
    path.write_bytes(payload)
    return path


@pytest.fixture()
def client(mp3_file: Path) -> TestClient:
    app = FastAPI()

    @app.api_route("/audio", methods=["GET", "HEAD"])
    def audio(request: Request):
        return serve_media_file(mp3_file)

    return TestClient(app)


def test_apple_mime_whitelist():
    assert APPLE_AUDIO_MIME[".mp3"] == "audio/mpeg"
    assert APPLE_AUDIO_MIME[".m4a"] == "audio/x-m4a"
    assert APPLE_AUDIO_MIME[".m4b"] == "audio/x-m4a"
    assert "codecs" not in APPLE_AUDIO_MIME[".opus"]
    assert media_type_for(Path("cover.jpg")) == "image/jpeg"


def test_media_headers_allow_caching_and_ranges():
    headers = media_headers()
    assert headers["Accept-Ranges"] == "bytes"
    assert "no-store" not in headers["Cache-Control"]
    assert "public" in headers["Cache-Control"]


def test_head_returns_length_without_body(client: TestClient, mp3_file: Path):
    response = client.head("/audio")
    assert response.status_code == 200
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert response.headers["content-length"] == str(mp3_file.stat().st_size)
    assert response.content == b""


def test_get_returns_full_file(client: TestClient, mp3_file: Path):
    response = client.get("/audio")
    assert response.status_code == 200
    assert response.content == mp3_file.read_bytes()
    assert "transfer-encoding" not in response.headers
    assert response.headers["content-length"] == str(len(response.content))


def test_range_request_returns_206(client: TestClient, mp3_file: Path):
    response = client.get("/audio", headers={"Range": "bytes=0-99"})
    assert response.status_code == 206
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-range"].startswith("bytes 0-99/")
    assert response.content == mp3_file.read_bytes()[:100]
    assert response.headers["content-length"] == "100"


def test_check_apikey_accepts_matching_token():
    assert check_apikey("2kYfiDOEwe4SolNvxVezeQ", "2kYfiDOEwe4SolNvxVezeQ")
    assert check_apikey("  abc  ", "abc")


def test_check_apikey_rejects_mismatch_or_empty():
    assert not check_apikey("wrong", "2kYfiDOEwe4SolNvxVezeQ")
    assert not check_apikey("", "2kYfiDOEwe4SolNvxVezeQ")
    assert not check_apikey("2kYfiDOEwe4SolNvxVezeQ", "")
    assert not check_apikey(None, "x")


def test_plugin_api_declares_native_head_routes():
    source = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    assert '"methods": ["GET", "HEAD"]' in source
    assert '"response_model": None' in source
    assert "response_class" in source
    assert '"allow_anonymous": True' in source
    assert 'plugin_version = "1.0.8"' in source

