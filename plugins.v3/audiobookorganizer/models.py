"""有声书刮削整理插件 — 数据模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class TrackInfo:
    """单集信息。"""

    episode: int
    title: str
    duration: Optional[int] = None  # 秒

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SearchResult:
    """刮削搜索结果摘要。"""

    source: str  # douban / ximalaya
    source_id: str
    title: str
    author: str = ""
    narrator: str = ""
    cover_url: str = ""
    track_count: int = 0
    score: float = 0.0  # 与搜索词的匹配度 0-1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AudiobookMetadata:
    """统一有声书元数据。"""

    title: str
    author: str = ""
    narrator: str = ""
    series: str = ""
    season: int = 1
    description: str = ""
    cover_url: str = ""
    source: str = ""
    source_id: str = ""
    tracks: List[TrackInfo] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["tracks"] = [t.to_dict() for t in self.tracks]
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AudiobookMetadata":
        tracks = [TrackInfo(**t) for t in data.get("tracks", [])]
        return cls(
            title=data.get("title", ""),
            author=data.get("author", ""),
            narrator=data.get("narrator", ""),
            series=data.get("series", ""),
            season=int(data.get("season") or 1),
            description=data.get("description", ""),
            cover_url=data.get("cover_url", ""),
            source=data.get("source", ""),
            source_id=data.get("source_id", ""),
            tracks=tracks,
        )


@dataclass
class AudioFile:
    """本地音频文件。"""

    path: Path
    relative_path: str
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_title: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": str(self.path),
            "relative_path": self.relative_path,
            "season": self.season,
            "episode": self.episode,
            "episode_title": self.episode_title,
        }


@dataclass
class BookEntry:
    """扫描到的一本待整理有声书。"""

    book_id: str
    name: str
    path: Path
    files: List[AudioFile]
    status: str = "pending"  # pending / previewed / organized / failed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "book_id": self.book_id,
            "name": self.name,
            "path": str(self.path),
            "files": [f.to_dict() for f in self.files],
            "status": self.status,
            "file_count": len(self.files),
        }


@dataclass
class FileChange:
    """单文件变更。"""

    source: str
    target: str
    tags: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OrganizePlan:
    """整理预览计划。"""

    plan_id: str
    book_id: str
    book_name: str
    metadata: AudiobookMetadata
    changes: List[FileChange] = field(default_factory=list)
    cover_path: str = ""
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "book_id": self.book_id,
            "book_name": self.book_name,
            "metadata": self.metadata.to_dict(),
            "changes": [c.to_dict() for c in self.changes],
            "cover_path": self.cover_path,
            "warnings": self.warnings,
        }
