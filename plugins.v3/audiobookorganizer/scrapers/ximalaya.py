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

        album = (data.get("data") or {}).get("albumPageMainData") or {}
        album_data = album.get("album") or album.get("albumInfo") or album
        if not album_data:
            album_data = data.get("data") or {}

        title = album_data.get("albumTitle") or album_data.get("title") or ""
        cover = album_data.get("coverPath") or album_data.get("coverLarge") or ""
        if cover and cover.startswith("//"):
            cover = "https:" + cover

        intro = album_data.get("richIntro") or album_data.get("intro") or ""
        if intro and "<" in intro:
            from bs4 import BeautifulSoup

            intro = BeautifulSoup(intro, "html.parser").get_text("\n", strip=True)

        anchor = album_data.get("anchorName") or album_data.get("nickname") or ""
        category = album_data.get("categoryTitle") or album_data.get("categoryName") or ""

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

            track_list = (data.get("data") or {}).get("tracks") or []
            if not track_list:
                break

            for idx, t in enumerate(track_list, start=len(tracks) + 1):
                title = t.get("title") or t.get("trackTitle") or f"第{idx}集"
                ep = self._parse_episode(title, idx)
                duration = t.get("duration")
                tracks.append(
                    TrackInfo(
                        episode=ep,
                        title=title,
                        duration=int(duration) if duration else None,
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
        items = (data.get("data") or {}).get("result") or {}
        albums = items.get("response") or items.get("docs") or []

        for item in albums:
            album_id = str(item.get("id") or item.get("albumId") or "")
            if not album_id:
                continue

            title = item.get("title") or item.get("albumTitle") or ""
            author = item.get("nickname") or item.get("anchorName") or ""
            cover = item.get("cover_path") or item.get("coverPath") or ""
            if cover and cover.startswith("//"):
                cover = "https:" + cover

            track_count = int(item.get("trackCount") or item.get("include_track_count") or 0)
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
