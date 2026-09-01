"""豆瓣书籍元数据刮削。"""

from __future__ import annotations

import re
from typing import List

import httpx
from bs4 import BeautifulSoup

from ..models import AudiobookMetadata, SearchResult
from .base import ScraperBase

_DOUBAN_SEARCH_URL = "https://book.douban.com/subject_search"
_DOUBAN_BOOK_URL = "https://book.douban.com/subject/{id}/"


class DoubanScraper(ScraperBase):
    """豆瓣书籍搜索与详情刮削。"""

    source_name = "douban"

    def search(self, keyword: str, limit: int = 10) -> List[SearchResult]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        try:
            with httpx.Client(
                headers=self._headers(), timeout=self.timeout, follow_redirects=True
            ) as client:
                resp = client.get(
                    _DOUBAN_SEARCH_URL,
                    params={"search_text": keyword, "cat": "1001"},
                )
                resp.raise_for_status()
        except Exception:
            return []

        return self._parse_search_html(resp.text, keyword, limit)

    def fetch(self, source_id: str) -> AudiobookMetadata:
        source_id = (source_id or "").strip()
        if not source_id:
            return AudiobookMetadata(title="", source=self.source_name)

        url = _DOUBAN_BOOK_URL.format(id=source_id)
        try:
            with httpx.Client(
                headers=self._headers(), timeout=self.timeout, follow_redirects=True
            ) as client:
                resp = client.get(url)
                resp.raise_for_status()
        except Exception:
            return AudiobookMetadata(title="", source=self.source_name, source_id=source_id)

        return self._parse_book_html(resp.text, source_id)

    def _parse_search_html(self, html: str, keyword: str, limit: int) -> List[SearchResult]:
        soup = BeautifulSoup(html, "html.parser")
        results: List[SearchResult] = []

        for item in soup.select(".item-root, .subject-item"):
            link = item.select_one("a[href*='/subject/']")
            if not link:
                continue
            href = link.get("href", "")
            m = re.search(r"/subject/(\d+)/", href)
            if not m:
                continue
            book_id = m.group(1)

            title_el = item.select_one(".title-text, .title a")
            title = title_el.get_text(strip=True) if title_el else link.get_text(strip=True)

            author_el = item.select_one(".abstract, .pub")
            author = ""
            if author_el:
                parts = author_el.get_text(" ", strip=True).split("/")
                author = parts[0].strip() if parts else ""

            cover_el = item.select_one("img")
            cover_url = cover_el.get("src", "") if cover_el else ""

            score = self._title_score(keyword, title)
            results.append(
                SearchResult(
                    source=self.source_name,
                    source_id=book_id,
                    title=title,
                    author=author,
                    cover_url=cover_url,
                    score=score,
                )
            )
            if len(results) >= limit:
                break

        results.sort(key=lambda r: r.score, reverse=True)
        return results

    def _parse_book_html(self, html: str, source_id: str) -> AudiobookMetadata:
        soup = BeautifulSoup(html, "html.parser")

        title_el = soup.select_one("h1 span")
        title = title_el.get_text(strip=True) if title_el else ""

        author = ""
        info = soup.select_one("#info")
        if info:
            for span in info.find_all("span", class_="pl"):
                label = span.get_text(strip=True)
                if "作者" in label:
                    author_links = span.find_next_siblings("a")
                    if author_links:
                        author = " / ".join(a.get_text(strip=True) for a in author_links)
                    else:
                        author = span.next_sibling.strip() if span.next_sibling else ""
                    break

        cover_el = soup.select_one("#mainpic img, #content img")
        cover_url = cover_el.get("src", "") if cover_el else ""

        desc_el = soup.select_one("#link-report .intro, #intro .intro")
        description = desc_el.get_text("\n", strip=True) if desc_el else ""

        return AudiobookMetadata(
            title=title,
            author=author,
            description=description,
            cover_url=cover_url,
            source=self.source_name,
            source_id=source_id,
        )

    @staticmethod
    def _title_score(keyword: str, title: str) -> float:
        keyword = keyword.lower().strip()
        title = title.lower().strip()
        if not keyword or not title:
            return 0.0
        if keyword == title:
            return 1.0
        if keyword in title or title in keyword:
            return 0.8
        kw_set = set(keyword)
        overlap = len(kw_set & set(title)) / max(len(kw_set), 1)
        return round(overlap * 0.6, 2)
