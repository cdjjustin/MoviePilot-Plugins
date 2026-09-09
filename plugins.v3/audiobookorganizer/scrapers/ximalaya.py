"""喜马拉雅专辑元数据刮削。"""

from __future__ import annotations

import re
from typing import Any, Dict, List

import httpx

from ..models import AudiobookMetadata, SearchResult, TrackInfo
from .base import ScraperBase

_XIMALAYA_SEARCH_API = "https://www.ximalaya.com/revision/search"
_XIMALAYA_ALBUM_API = "https://www.ximalaya.com/revision/album/v1/getTracksList"
_XIMALAYA_ALBUM_INFO = "https://www.ximalaya.com/revision/album/v1/simple"


class XimalayaScraper(ScraperBase):
    """喜马拉雅专辑搜索与分集刮削。"""

    source_name = "ximalaya"

    def search(self, keyword: str, limit: int = 10) -> List[SearchResult]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        try:
            with httpx.Client(
                headers=self._headers(), timeout=self.timeout, follow_redirects=True
            ) as client:
                resp = client.get(
                    _XIMALAYA_SEARCH_API,
                    params={"core": "album", "kw": keyword, "page": 1, "rows": limit},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            return []

        return self._parse_search_json(data, keyword)

    def fetch(self, source_id: str) -> AudiobookMetadata:
        source_id = (source_id or "").strip()
        if not source_id:
            return AudiobookMetadata(title="", source=self.source_name)

        album_info = self._fetch_album_info(source_id)
        tracks = self._fetch_tracks(source_id)

        return AudiobookMetadata(
            title=album_info.get("title", ""),
            author=album_info.get("author", ""),
            narrator=album_info.get("narrator", ""),
            series=album_info.get("series", ""),
            description=album_info.get("description", ""),
            cover_url=album_info.get("cover_url", ""),
            source=self.source_name,
            source_id=source_id,
            tracks=tracks,
        )

    def _fetch_album_info(self, album_id: str) -> Dict[str, Any]:
        try:
            with httpx.Client(
                headers=self._headers(), timeout=self.timeout, follow_redirects=True
            ) as client:
                resp = client.get(
                    _XIMALAYA_ALBUM_INFO,
                    params={"albumId": album_id},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            return {}

        if not isinstance(data, dict):
            return {}
        payload = data.get("data")
        if not isinstance(payload, dict):
            return {}
        album_page = payload.get("albumPageMainData")
        if isinstance(album_page, dict):
            album_data = album_page.get("album") or album_page.get("albumInfo") or album_page
        else:
            # 喜马拉雅部分响应直接把专辑字段放在 data 下。
            album_data = payload
        if not isinstance(album_data, dict):
            return {}

        title = album_data.get("albumTitle") or album_data.get("title") or ""
        cover = album_data.get("coverPath") or album_data.get("coverLarge") or ""
        intro = album_data.get("richIntro") or album_data.get("intro") or ""
        anchor = album_data.get("anchorName") or album_data.get("nickname") or ""
        category = album_data.get("categoryTitle") or album_data.get("categoryName") or ""
        title = title if isinstance(title, str) else ""
        cover = cover if isinstance(cover, str) else ""
        intro = intro if isinstance(intro, str) else ""
        anchor = anchor if isinstance(anchor, str) else ""
        category = category if isinstance(category, str) else ""
        if cover and cover.startswith("//"):
            cover = "https:" + cover

        if intro and "<" in intro:
            from bs4 import BeautifulSoup

            intro = BeautifulSoup(intro, "html.parser").get_text("\n", strip=True)

        return {
            "title": title,
            "author": category,
            "narrator": anchor,
            "series": title,
            "description": intro,
            "cover_url": cover,
        }

    def _fetch_tracks(self, album_id: str) -> List[TrackInfo]:
        tracks: List[TrackInfo] = []
        page = 1
        page_size = 30

        while True:
            try:
                with httpx.Client(
                    headers=self._headers(), timeout=self.timeout, follow_redirects=True
                ) as client:
                    resp = client.get(
                        _XIMALAYA_ALBUM_API,
                        params={
                            "albumId": album_id,
                            "pageNum": page,
                            "pageSize": page_size,
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
            except Exception:
                break

            if not isinstance(data, dict):
                break
            payload = data.get("data")
            if not isinstance(payload, dict):
                break
            track_list = payload.get("tracks") or []
            if not isinstance(track_list, list) or not track_list:
                break

            for idx, t in enumerate(track_list, start=len(tracks) + 1):
                if not isinstance(t, dict):
                    continue
                title = t.get("title") or t.get("trackTitle") or f"第{idx}集"
                title = title if isinstance(title, str) else f"第{idx}集"
                ep = self._parse_episode(title, idx)
                duration = t.get("duration")
                try:
                    duration_value = int(duration) if duration is not None else None
                except (TypeError, ValueError, OverflowError):
                    duration_value = None
                tracks.append(
                    TrackInfo(
                        episode=ep,
                        title=title,
                        duration=duration_value,
                    )
                )

            if len(track_list) < page_size:
                break
            page += 1
            if page > 50:
                break

        return tracks

    def _parse_search_json(self, data: dict, keyword: str) -> List[SearchResult]:
        results: List[SearchResult] = []
        if not isinstance(data, dict):
            return results
        payload = data.get("data")
        if not isinstance(payload, dict):
            return results
        items = payload.get("result")
        if not isinstance(items, dict):
            return results
        albums = items.get("response") or items.get("docs") or []
        if not isinstance(albums, list):
            return results

        for item in albums:
            if not isinstance(item, dict):
                continue
            album_id = str(item.get("id") or item.get("albumId") or "")
            if not album_id:
                continue

            title = item.get("title") or item.get("albumTitle") or ""
            author = item.get("nickname") or item.get("anchorName") or ""
            cover = item.get("cover_path") or item.get("coverPath") or ""
            title = title if isinstance(title, str) else ""
            author = author if isinstance(author, str) else ""
            cover = cover if isinstance(cover, str) else ""
            if cover and cover.startswith("//"):
                cover = "https:" + cover

            raw_track_count = item.get("trackCount") or item.get("include_track_count") or 0
            try:
                track_count = int(raw_track_count)
            except (TypeError, ValueError, OverflowError):
                track_count = 0
            score = self._title_score(keyword, title)

            results.append(
                SearchResult(
                    source=self.source_name,
                    source_id=album_id,
                    title=title,
                    author=author,
                    narrator=author,
                    cover_url=cover,
                    track_count=track_count,
                    score=score,
                )
            )

        results.sort(key=lambda r: r.score, reverse=True)
        return results

    @staticmethod
    def _parse_episode(title: str, fallback: int) -> int:
        m = re.search(r"第\s*0*(\d+)\s*集", title)
        if m:
            return int(m.group(1))
        m = re.search(r"0*(\d+)", title)
        if m:
            return int(m.group(1))
        return fallback

    @staticmethod
    def _title_score(keyword: str, title: str) -> float:
        keyword = keyword.lower().strip()
        title = title.lower().strip()
        if not keyword or not title:
            return 0.0
        if keyword == title:
            return 1.0
        if keyword in title or title in keyword:
            return 0.85
        kw_set = set(keyword)
        overlap = len(kw_set & set(title)) / max(len(kw_set), 1)
        return round(overlap * 0.6, 2)
