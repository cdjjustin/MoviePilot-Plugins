"""有声书元数据刮削器。"""

from .base import ScraperBase
from .douban import DoubanScraper
from .ximalaya import XimalayaScraper

__all__ = ["ScraperBase", "DoubanScraper", "XimalayaScraper"]
