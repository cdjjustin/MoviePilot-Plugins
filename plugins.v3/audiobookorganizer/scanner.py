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
    "零": 0, "〇": 0, "两": 2,
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_NUM_RE = re.compile(
    r"[一二三四五六七八九两]?十[一二三四五六七八九]?|[一二三四五六七八九两零〇]"
)
_SEP_CLASS = r"[-_./．—–－〜~]"
_FNAME_SEASON_RE = re.compile(
    rf"第\s*([一二三四五六七八九十百千万两零〇]+|\d+)\s*季"
)
_FNAME_SEASON_EP_RE = re.compile(
    rf"第\s*([一二三四五六七八九十百千万两零〇]+|\d+)\s*季\s*{_SEP_CLASS}?\s*(?:第\s*)?0*(\d+)\s*(?:集)?"
)
_FNAME_EPISODE_RE = re.compile(r"第\s*0*(\d+)\s*集")
_FNAME_SXXEXX_RE = re.compile(r"S(\d{1,2})E(\d{1,4})", re.IGNORECASE)
_DIR_SEASON_RE = re.compile(
    r"(?i)(?:^|[\s_\-.(（【])(?:season|s|se)[\s._-]*0*(\d{1,2})(?:$|[\s_\-.)）】])"
)
_LEADING_EP_RE = re.compile(rf"^(?:.*{_SEP_CLASS})?0*(\d{{1,4}})(?:\s*{_SEP_CLASS}|\s+|$)")
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
    1. ``第5季-146`` / ``第二季-015``（中文季号 + 紧随集号，含全角横线）
    2. ``第二季.第002集``
    3. 仅 ``第002集``（无季号）
    4. 仅 ``第5季``（集号可再从前置数字推断）
    5. ``S01E12``（无中文季号时才用；若同名已有「第X季」则以中文为准）
    """
    text = stem or ""
    season_ep = _FNAME_SEASON_EP_RE.search(text)
    if season_ep:
        season = cn_to_int(season_ep.group(1))
        episode = int(season_ep.group(2))
        if season > 0 and episode > 0:
            return season, episode

    season_m = _FNAME_SEASON_RE.search(text)
    ep_m = _FNAME_EPISODE_RE.search(text)
    season = cn_to_int(season_m.group(1)) if season_m else None
    if season is not None and season <= 0:
        season = None
    episode = int(ep_m.group(1)) if ep_m else None

    if season and episode is None:
        # 「第二季-015」若因特殊字符没命中组合正则，尝试季号后再取数字
        after = text[season_m.end():] if season_m else ""
        trailing = re.match(rf"^\s*{_SEP_CLASS}?\s*0*(\d{{1,4}})", after)
        if trailing:
            episode = int(trailing.group(1))

    if season and episode:
        return season, episode
    if episode:
        return season, episode
    if season:
        return season, None

    sxx = _FNAME_SXXEXX_RE.search(text)
    if sxx:
        return int(sxx.group(1)), int(sxx.group(2))
    return None, None


def parse_season_from_dirname(name: str) -> Optional[int]:
    """从目录名解析季号：第二季 / Season 2 / S02。"""
    text = name or ""
    season_m = _FNAME_SEASON_RE.search(text)
    if season_m:
        season = cn_to_int(season_m.group(1))
        if season > 0:
            return season
    dir_m = _DIR_SEASON_RE.search(f" {text} ")
    if dir_m:
        season = int(dir_m.group(1))
        if season > 0:
            return season
    return None


def resolve_season_episode(
    *texts: str,
    season: Optional[int] = None,
    episode: Optional[int] = None,
) -> Tuple[Optional[int], Optional[int]]:
    """
    从多个文本片段补全季/集（文件名、标题、目录名）。

    文本中的完整「第二季-015」/「S02E15」优先于传入的 season/episode
    （避免刮削对齐或旧 S01 前缀把后续季锁死在第一季）。
    """
    found_season: Optional[int] = None
    found_episode: Optional[int] = None

    for text in texts:
        if not text:
            continue
        parsed_season, parsed_episode = parse_season_ep_from_stem(text)
        # 完整季+集：直接采用（中文季号优先于同名里的 S01Exx）
        if parsed_season is not None and parsed_episode is not None:
            return parsed_season, parsed_episode
        if found_season is None and parsed_season is not None:
            found_season = parsed_season
        if found_episode is None and parsed_episode is not None:
            found_episode = parsed_episode
        if found_season is None:
            dir_season = parse_season_from_dirname(text)
            if dir_season is not None:
                found_season = dir_season

    if found_season is None:
        found_season = season
    if found_episode is None:
        found_episode = episode
    return found_season, found_episode


def clean_episode_title(stem: str) -> str:
    """去掉 SxxExx / 第X季-N / 第N集 等前缀，留下可读集标题。"""
    title = stem.strip()
    title = _FNAME_SXXEXX_RE.sub(" ", title, count=1)
    title = re.sub(r"^\s*[-–—_]\s*", "", title)
    season_ep = _FNAME_SEASON_EP_RE.search(title)
    if season_ep:
        title = f"{title[:season_ep.start()]} {title[season_ep.end():]}"
    else:
        season_only = _FNAME_SEASON_RE.search(title)
        if season_only:
            title = f"{title[:season_only.start()]} {title[season_only.end():]}"
    title = re.sub(r"^.*?(第\s*0*\d+\s*集)\s*[.·\-—–－_]?\s*", "", title, count=1)
    title = re.sub(r"^\d+\s*[-–—–－_.]\s*", "", title)
    title = re.sub(rf"\s*{_SEP_CLASS}\s*", " - ", title)
    title = re.sub(r"\s+", " ", title).strip(" .-_—–－")
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
        if season is None and not root_only:
            for part in Path(rel).parts[:-1]:
                dir_season = parse_season_from_dirname(part)
                if dir_season is not None:
                    season = dir_season
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
