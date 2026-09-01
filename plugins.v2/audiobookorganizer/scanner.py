"""有声书目录扫描。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import AudioFile, BookEntry

AUDIO_EXTENSIONS = frozenset(
    {".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".flac", ".wav", ".opus", ".wma", ".aiff", ".mp4"}
)

_CN_DIGIT_MAP: Dict[str, int] = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_NUM_RE = re.compile(
    r"[一二三四五六七八九]?十[一二三四五六七八九]?|[一二三四五六七八九]"
)
_FNAME_SEASON_RE = re.compile(r"第([一二三四五六七八九十百千万]+|\d+)季")
_FNAME_EPISODE_RE = re.compile(r"第0*(\d+)集")
_NAME_JUNK_RE = re.compile(
    r"[\s\-_—\[【（(]+(?:\d+\s*k(?:bps?|b?)?|mp[34]|flac|aac|wav)[\s\]】）)]*$",
    re.IGNORECASE,
)


def natural_key(s: str) -> list:
    """自然排序 key。"""

    def _cn_replace(m: re.Match) -> str:
        t = m.group()
        if t == "十":
            return "10"
        if t.startswith("十"):
            return str(10 + _CN_DIGIT_MAP.get(t[1:], 0))
        if "十" in t:
            idx = t.index("十")
            return str(_CN_DIGIT_MAP.get(t[:idx], 0) * 10 + _CN_DIGIT_MAP.get(t[idx + 1:], 0))
        return str(_CN_DIGIT_MAP.get(t, 0))

    normalized = _CN_NUM_RE.sub(_cn_replace, s)
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r"(\d+)", normalized)]


def cn_to_int(cn_text: str) -> int:
    if cn_text.isdigit():
        return int(cn_text)

    def _cn_replace(m: re.Match) -> str:
        t = m.group()
        if t == "十":
            return "10"
        if t.startswith("十"):
            return str(10 + _CN_DIGIT_MAP.get(t[1:], 0))
        if "十" in t:
            idx = t.index("十")
            return str(_CN_DIGIT_MAP.get(t[:idx], 0) * 10 + _CN_DIGIT_MAP.get(t[idx + 1:], 0))
        return str(_CN_DIGIT_MAP.get(t, 0))

    normalized = _CN_NUM_RE.sub(_cn_replace, cn_text)
    return int(normalized) if normalized.isdigit() else 0


def parse_season_ep_from_stem(stem: str) -> Tuple[Optional[int], Optional[int]]:
    season_m = _FNAME_SEASON_RE.search(stem)
    ep_m = _FNAME_EPISODE_RE.search(stem)
    if season_m and ep_m:
        s = cn_to_int(season_m.group(1))
        e = int(ep_m.group(1))
        if s > 0 and e > 0:
            return s, e
    if ep_m:
        return None, int(ep_m.group(1))
    return None, None


def clean_book_name(name: str) -> str:
    return _NAME_JUNK_RE.sub("", name).strip()


def make_book_id(name: str, path: Path) -> str:
    raw = f"{name}:{path.resolve()}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def scan_directory(source_path: str) -> List[BookEntry]:
    """扫描有声书源目录，返回待整理书籍列表。"""
    base = Path(source_path)
    if not base.is_dir():
        return []

    books: List[BookEntry] = []

    for item in sorted(base.iterdir(), key=lambda p: natural_key(p.name)):
        if not item.is_dir():
            continue
        audio_files = _collect_audio_files(item)
        if audio_files:
            name = clean_book_name(item.name)
            books.append(
                BookEntry(
                    book_id=make_book_id(name, item),
                    name=name,
                    path=item,
                    files=audio_files,
                )
            )

    root_files = _collect_audio_files(base, root_only=True)
    if root_files:
        books.insert(
            0,
            BookEntry(
                book_id=make_book_id("待整理", base),
                name="待整理",
                path=base,
                files=root_files,
            ),
        )

    return books


def _collect_audio_files(directory: Path, root_only: bool = False) -> List[AudioFile]:
    if root_only:
        candidates = [f for f in directory.iterdir() if f.is_file()]
    else:
        candidates = [f for f in directory.rglob("*") if f.is_file()]

    audio_paths = sorted(
        [f for f in candidates if f.suffix.lower() in AUDIO_EXTENSIONS],
        key=lambda f: natural_key(
            str(f.relative_to(directory)) if not root_only else f.name
        ),
    )

    files: List[AudioFile] = []
    for idx, f in enumerate(audio_paths, start=1):
        rel = str(f.relative_to(directory)).replace("\\", "/")
        season, episode = parse_season_ep_from_stem(f.stem)
        if episode is None:
            episode = idx
        files.append(
            AudioFile(
                path=f,
                relative_path=rel,
                season=season,
                episode=episode,
                episode_title=f.stem,
            )
        )
    return files
