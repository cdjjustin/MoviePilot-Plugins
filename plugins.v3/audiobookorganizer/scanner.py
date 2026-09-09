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
_SEP_CLASS = r"[-_./．·•—–－〜~]"
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
    r"(?:【[^】]*(?:主题曲|片头曲|片尾曲|插曲|片头|片尾|预告|花絮|广告|彩蛋|PV|BONUS|OP|ED)[^】]*】)"
    r"|(?:^|[\s\-_.．·•])(?:主题曲|片头曲|片尾曲|插曲|片头|片尾|预告|花絮|广告|彩蛋)(?:\s*\d*)?(?:$|[\s\-_.．·•])"
    r"|(?:^|[\s\-_.．·•])PV\s*\d*$",
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
    从文件名解析季/集（对齐有声书播客，并兼容常见变体）。

    优先级：
    1. ``第二季-015`` / ``第5季-146``（季号后紧跟集号）
    2. ``039.第二季.第002集.xxx``（播客同款：第X季 + 第N集）
    3. 仅 ``第002集`` / 仅 ``第5季``
    4. ``S01E12``（无中文季号时才用；同名有「第X季」则以中文为准）
    """
    text = stem or ""
    season_ep = _FNAME_SEASON_EP_RE.search(text)
    if season_ep:
        season = cn_to_int(season_ep.group(1))
        episode = int(season_ep.group(2))
        if season > 0 and episode > 0:
            return season, episode

    # 与有声书播客一致：同时命中「第X季」与「第N集」
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


def build_season_bucket_map(book_dir: Path, audio_paths: List[Path]) -> Dict[str, int]:
    """
    按书目录下「第一级子目录」分季（对齐有声书播客）。

    - 仅扁平文件（无子目录）→ 空映射，季号留给文件名解析
    - 子目录名含「第二季」/``S02`` → 用解析值
    - 否则按自然排序后的序号作为季号（CD1/CD2、上部/下部等）
    """
    keys: set[str] = set()
    for path in audio_paths:
        try:
            rel = path.relative_to(book_dir)
        except ValueError:
            continue
        key = rel.parts[0] if len(rel.parts) > 1 else ""
        keys.add(key)

    sorted_keys = sorted(keys, key=natural_key)
    has_seasons = len(sorted_keys) > 1 or (
        len(sorted_keys) == 1 and sorted_keys[0] != ""
    )
    if not has_seasons:
        return {}

    bucket_map: Dict[str, int] = {}
    used_seasons: set[int] = set()
    pending_keys: List[str] = []

    for key in sorted_keys:
        if not key:
            pending_keys.append(key)
            continue
        parsed = parse_season_from_dirname(key)
        if parsed is not None and parsed not in used_seasons:
            bucket_map[key] = parsed
            used_seasons.add(parsed)
        else:
            pending_keys.append(key)

    next_season = 1
    for key in pending_keys:
        while next_season in used_seasons:
            next_season += 1
        bucket_map[key] = next_season
        used_seasons.add(next_season)
        next_season += 1

    return bucket_map


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
    """
    收集音频并解析季/集。策略对齐有声书播客：

    1. 文件名解析（``第二季.第002集`` / ``第二季-015`` / ``S02E15``）
    2. 否则用第一级子目录分季（目录名含季号优先，否则自然排序编号）
    3. 更深路径里的「第X季」目录也可补季号
    """
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

    bucket_map: Dict[str, int] = {}
    if not root_only:
        bucket_map = build_season_bucket_map(directory, audio_paths)

    files: List[AudioFile] = []
    for f in audio_paths:
        rel = str(f.relative_to(directory)).replace("\\", "/")
        rel_parts = Path(rel).parts
        season, episode = parse_season_ep_from_stem(f.stem)

        if season is None and not root_only:
            if len(rel_parts) > 1:
                top_key = rel_parts[0]
                if top_key in bucket_map:
                    season = bucket_map[top_key]
            if season is None:
                for part in rel_parts[:-1]:
                    dir_season = parse_season_from_dirname(part)
                    if dir_season is not None:
                        season = dir_season
                        break

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
