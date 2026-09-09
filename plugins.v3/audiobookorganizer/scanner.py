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
_FNAME_SEASON_EP_RE = re.compile(
    r"第([一二三四五六七八九十百千万]+|\d+)季\s*[-_./]?\s*0*(\d+)"
)
_FNAME_EPISODE_RE = re.compile(r"第0*(\d+)集")
_FNAME_SXXEXX_RE = re.compile(r"S(\d{1,2})E(\d{1,4})", re.IGNORECASE)
_EXTRA_TRACK_RE = re.compile(
    r"(?:【[^】]*(?:主题曲|片头曲|片尾曲|插曲|片头|片尾|预告|花絮|广告|彩蛋|BONUS|OP|ED)[^】]*】)"
    r"|(?:^|[\s\-_.．])(?:主题曲|片头曲|片尾曲|插曲|片头|片尾|预告|花絮|广告|彩蛋)(?:$|[\s\-_.．])",
    re.IGNORECASE,
)
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
    """
    从文件名解析季/集。

    优先级：
    1. ``第5季-146`` / ``第5季 146``（中文季号 + 紧随集号）
    2. ``第二季.第002集``
    3. 仅 ``第002集``（无季号）
    4. 仅 ``第5季``（集号留给调用方用序号兜底）
    5. ``S01E12``（无中文季号时才用）
    """
    season_ep = _FNAME_SEASON_EP_RE.search(stem)
    if season_ep:
        season = cn_to_int(season_ep.group(1))
        episode = int(season_ep.group(2))
        if season > 0 and episode > 0:
            return season, episode

    season_m = _FNAME_SEASON_RE.search(stem)
    ep_m = _FNAME_EPISODE_RE.search(stem)
    season = cn_to_int(season_m.group(1)) if season_m else None
    if season is not None and season <= 0:
        season = None
    episode = int(ep_m.group(1)) if ep_m else None

    if season and episode:
        return season, episode
    if episode:
        return season, episode
    if season:
        return season, None

    sxx = _FNAME_SXXEXX_RE.search(stem)
    if sxx:
        return int(sxx.group(1)), int(sxx.group(2))
    return None, None


def clean_episode_title(stem: str) -> str:
    """去掉 SxxExx / 第X季-N / 第N集 等前缀，留下可读集标题。"""
    title = stem.strip()
    title = _FNAME_SXXEXX_RE.sub(" ", title, count=1)
    title = re.sub(r"^\s*[-–—_]\s*", "", title)
    title = re.sub(
        r"^.*?(第(?:[一二三四五六七八九十百千万]+|\d+)季\s*[-_./]?\s*0*\d+)\s*",
        "",
        title,
        count=1,
    )
    title = re.sub(r"^.*?(第0*\d+集)\s*[.·\-_]?\s*", "", title, count=1)
    title = re.sub(r"^\d+\s*[-–—_.]\s*", "", title)
    title = re.sub(r"\s+", " ", title).strip(" .-_—")
    return title or stem


def is_extra_track(name: str) -> bool:
    """主题曲/插曲/片头片尾等附属音轨。"""
    return bool(_EXTRA_TRACK_RE.search(name or ""))


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
        if is_extra_track(f.stem):
            # 附属音轨单独进 S00，避免和「第N集」抢同一集号
            season = 0
            episode = None
        files.append(
            AudioFile(
                path=f,
                relative_path=rel,
                season=season,
                episode=episode,
                episode_title=clean_episode_title(f.stem),
            )
        )
    return files
