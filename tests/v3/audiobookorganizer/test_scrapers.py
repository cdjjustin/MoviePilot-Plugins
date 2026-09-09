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


def test_ximalaya_search_supports_response_docs_envelope():
    scraper = XimalayaScraper()
    data = {
        "data": {
            "result": {
                "response": {
                    "docs": [
                        {
                            "id": "98765",
                            "title": "《剑来》上 | 原文无删减&大斌",
                            "nickname": "大斌",
                            "trackCount": 100,
                        }
                    ]
                }
            }
        }
    }

    results = scraper._parse_search_json(data, "剑来")

    assert len(results) == 1
    assert results[0].source_id == "98765"
    assert results[0].title == "《剑来》上 | 原文无删减&大斌"
    assert results[0].track_count == 100


def test_ximalaya_search_supports_result_docs_envelope():
    data = {
        "data": {
            "result": {
                "docs": [{"id": "54321", "title": "剑来", "trackCount": 10}]
            }
        }
    }

    results = XimalayaScraper()._parse_search_json(data, "剑来")

    assert [result.source_id for result in results] == ["54321"]


@pytest.mark.parametrize(
    "response",
    [
        {"docs": "unexpected"},
        {"docs": {"id": "123"}},
        {"docs": None},
        {"docs": "unexpected", "response": [{"id": "123", "title": "误入结果"}]},
    ],
)
def test_ximalaya_search_ignores_non_list_response_docs(response):
    data = {"data": {"result": {"response": response}}}

    assert XimalayaScraper()._parse_search_json(data, "剑来") == []


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


@pytest.mark.parametrize("payload", ["rate limited", ["unexpected"], None])
def test_ximalaya_search_ignores_malformed_result_shape(payload):
    scraper = XimalayaScraper()
    data = {"data": {"result": payload}}

    assert scraper._parse_search_json(data, "三体") == []


def test_ximalaya_search_normalizes_malformed_album_fields():
    scraper = XimalayaScraper()
    data = {
        "data": {
            "result": {
                "response": [
                    {
                        "id": "123",
                        "title": 123,
                        "nickname": {"unexpected": True},
                        "trackCount": "not-a-number",
                    }
                ]
            }
        }
    }

    results = scraper._parse_search_json(data, "三体")

    assert len(results) == 1
    assert results[0].title == ""
    assert results[0].author == ""
    assert results[0].track_count == 0


@patch("audiobookorganizer.scrapers.ximalaya.httpx.Client")
def test_ximalaya_tracks_skips_malformed_items_and_duration(mock_client_cls):
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "data": {
            "tracks": [
                "unexpected",
                {"title": "第01集 开篇", "duration": "not-a-number"},
            ]
        }
    }
    mock_client.get.return_value = response
    mock_client_cls.return_value = mock_client

    tracks = XimalayaScraper()._fetch_tracks("123")

    assert len(tracks) == 1
    assert tracks[0].episode == 1
    assert tracks[0].duration is None


def test_ximalaya_search_and_tracks_ignore_infinite_numeric_fields():
    scraper = XimalayaScraper()
    search_data = {
        "data": {
            "result": {
                "response": [
                    {"id": "123", "title": "三体", "trackCount": float("inf")}
                ]
            }
        }
    }
    tracks_data = {
        "data": {"tracks": [{"title": "第01集 开篇", "duration": float("inf")}]}
    }

    search_results = scraper._parse_search_json(search_data, "三体")
    with patch("audiobookorganizer.scrapers.ximalaya.httpx.Client") as client_cls:
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = tracks_data
        client.get.return_value = response
        client_cls.return_value = client
        tracks = scraper._fetch_tracks("123")

    assert search_results[0].track_count == 0
    assert tracks[0].duration is None


@pytest.mark.parametrize(
    "album_json",
    [
        [],
        {"data": "unexpected"},
        {"data": {"albumPageMainData": {"album": "unexpected"}}},
        {"data": {"albumPageMainData": {"album": {"albumTitle": 123, "intro": {}}}}},
    ],
)
@patch("audiobookorganizer.scrapers.ximalaya.httpx.Client")
def test_ximalaya_album_info_ignores_malformed_shapes(mock_client_cls, album_json):
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = album_json
    mock_client.get.return_value = response
    mock_client_cls.return_value = mock_client

    result = XimalayaScraper()._fetch_album_info("123")

    assert result.get("title", "") == ""
    assert result.get("author", "") == ""
    assert result.get("narrator", "") == ""
    assert result.get("series", "") == ""
    assert result.get("description", "") == ""
    assert result.get("cover_url", "") == ""


@patch("audiobookorganizer.scrapers.ximalaya.httpx.Client")
def test_ximalaya_album_info_supports_direct_data_envelope(mock_client_cls):
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "data": {
            "albumTitle": "直接专辑",
            "anchorName": "主播",
            "categoryTitle": "有声书",
            "intro": "简介",
        }
    }
    mock_client.get.return_value = response
    mock_client_cls.return_value = mock_client

    result = XimalayaScraper()._fetch_album_info("123")

    assert result["title"] == "直接专辑"
    assert result["narrator"] == "主播"
    assert result["author"] == "有声书"
    assert result["description"] == "简介"
