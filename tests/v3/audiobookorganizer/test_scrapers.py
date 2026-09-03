"""刮削器测试（mock HTTP）。"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins.v3"
sys.path.insert(0, str(PLUGIN_DIR))

from audiobookorganizer.scrapers.douban import DoubanScraper  # noqa: E402
from audiobookorganizer.scrapers.ximalaya import XimalayaScraper  # noqa: E402

DOUBAN_SEARCH_HTML = """
<html><body>
<div class="item-root">
  <a href="https://book.douban.com/subject/2567698/"><span class="title-text">三体</span></a>
  <div class="abstract">刘慈欣 / 重庆出版社</div>
  <img src="https://img.douban.com/cover.jpg" />
</div>
</body></html>
"""

DOUBAN_BOOK_HTML = """
<html><body>
<h1><span>三体</span></h1>
<div id="info">
  <span class="pl">作者</span>: <a>刘慈欣</a>
</div>
<div id="mainpic"><img src="https://img.douban.com/book.jpg" /></div>
<div id="link-report"><div class="intro">地球往事三部曲第一部</div></div>
</body></html>
"""

XIMALAYA_SEARCH_JSON = {
    "data": {
        "result": {
            "response": [
                {
                    "id": "12345",
                    "title": "三体（有声书）",
                    "nickname": "某某主播",
                    "cover_path": "//image.ximalaya.com/cover.jpg",
                    "trackCount": 50,
                }
            ]
        }
    }
}

XIMALAYA_ALBUM_JSON = {
    "data": {
        "albumPageMainData": {
            "album": {
                "albumTitle": "三体（有声书）",
                "anchorName": "某某主播",
                "coverPath": "//image.ximalaya.com/cover.jpg",
                "intro": "科幻有声书",
            }
        }
    }
}

XIMALAYA_TRACKS_JSON = {
    "data": {
        "tracks": [
            {"title": "第01集 科学边界", "duration": 3600},
            {"title": "第02集 台球", "duration": 3200},
        ]
    }
}


def _mock_client(responses: dict):
    """创建按 URL 关键字返回预设响应的 mock client。"""
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)

    def _get(url, **kwargs):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if "subject_search" in url:
            resp.text = responses.get("search_html", "")
        elif "/subject/" in url:
            resp.text = responses.get("book_html", "")
        elif "revision/search" in url:
            resp.json.return_value = responses.get("search_json", {})
        elif "getTracksList" in url:
            resp.json.return_value = responses.get("tracks_json", {"data": {"tracks": []}})
        elif "album/v1/simple" in url:
            resp.json.return_value = responses.get("album_json", {})
        else:
            resp.text = ""
            resp.json.return_value = {}
        return resp

    mock_client.get = _get
    return mock_client


@patch("audiobookorganizer.scrapers.douban.httpx.Client")
def test_douban_search(mock_client_cls):
    mock_client_cls.return_value = _mock_client({"search_html": DOUBAN_SEARCH_HTML})
    scraper = DoubanScraper()
    results = scraper.search("三体")
    assert len(results) == 1
    assert results[0].title == "三体"
    assert results[0].source_id == "2567698"
    assert results[0].score > 0


@patch("audiobookorganizer.scrapers.douban.httpx.Client")
def test_douban_fetch(mock_client_cls):
    mock_client_cls.return_value = _mock_client({"book_html": DOUBAN_BOOK_HTML})
    scraper = DoubanScraper()
    meta = scraper.fetch("2567698")
    assert meta.title == "三体"
    assert "刘慈欣" in meta.author
    assert meta.cover_url


@patch("audiobookorganizer.scrapers.ximalaya.httpx.Client")
def test_ximalaya_search(mock_client_cls):
    mock_client_cls.return_value = _mock_client({"search_json": XIMALAYA_SEARCH_JSON})
    scraper = XimalayaScraper()
    results = scraper.search("三体")
    assert len(results) == 1
    assert results[0].source_id == "12345"
    assert results[0].track_count == 50


@patch("audiobookorganizer.scrapers.ximalaya.httpx.Client")
def test_ximalaya_fetch(mock_client_cls):
    def _get(url, **kwargs):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if "getTracksList" in url:
            resp.json.return_value = XIMALAYA_TRACKS_JSON
        else:
            resp.json.return_value = XIMALAYA_ALBUM_JSON
        return resp

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = _get
    mock_client_cls.return_value = mock_client

    scraper = XimalayaScraper()
    meta = scraper.fetch("12345")
    assert meta.title == "三体（有声书）"
    assert meta.narrator == "某某主播"
    assert len(meta.tracks) == 2
    assert meta.tracks[0].episode == 1


def test_title_score_exact_match():
    assert DoubanScraper._title_score("三体", "三体") == 1.0
    assert XimalayaScraper._title_score("三体", "三体（有声书）") == 0.85
