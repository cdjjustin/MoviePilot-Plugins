"""刮削器抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from ..models import AudiobookMetadata, SearchResult


class ScraperBase(ABC):
    """元数据源刮削器接口。"""

    source_name: str = ""

    def __init__(self, cookie: str = "", timeout: float = 15.0):
        self.cookie = (cookie or "").strip()
        self.timeout = timeout

    @abstractmethod
    def search(self, keyword: str, limit: int = 10) -> List[SearchResult]:
        """按关键词搜索。"""

    @abstractmethod
    def fetch(self, source_id: str) -> AudiobookMetadata:
        """获取完整元数据。"""

    def _headers(self) -> dict:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers
