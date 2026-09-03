"""音频标签写入。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".m4b", ".aac", ".mp4"}


def write_tags(
    path: Path,
    *,
    title: str,
    author: str = "",
    narrator: str = "",
    album: str = "",
    track_number: int = 0,
    description: str = "",
    cover_data: Optional[bytes] = None,
) -> bool:
    """写入音频元数据标签，成功返回 True。"""
    ext = path.suffix.lower()
    if ext not in AUDIO_EXTENSIONS:
        return False

    try:
        if ext == ".mp3":
            return _write_mp3_tags(
                path,
                title=title,
                author=author,
                narrator=narrator,
                album=album,
                track_number=track_number,
                description=description,
                cover_data=cover_data,
            )
        return _write_mp4_tags(
            path,
            title=title,
            author=author,
            narrator=narrator,
            album=album,
            track_number=track_number,
            description=description,
            cover_data=cover_data,
        )
    except Exception:
        return False


def _write_mp3_tags(
    path: Path,
    *,
    title: str,
    author: str,
    narrator: str,
    album: str,
    track_number: int,
    description: str,
    cover_data: Optional[bytes],
) -> bool:
    from mutagen.id3 import APIC, COMM, ID3, TALB, TIT2, TPE1, TPE2, TRCK

    try:
        tags = ID3(path)
    except Exception:
        tags = ID3()

    tags.delall("TIT2")
    tags.delall("TPE1")
    tags.delall("TPE2")
    tags.delall("TALB")
    tags.delall("TRCK")
    tags.delall("COMM")
    tags.delall("APIC")

    tags.add(TIT2(encoding=3, text=title))
    if author:
        tags.add(TPE1(encoding=3, text=author))
    if narrator:
        tags.add(TPE2(encoding=3, text=narrator))
    if album:
        tags.add(TALB(encoding=3, text=album))
    if track_number > 0:
        tags.add(TRCK(encoding=3, text=str(track_number)))
    if description:
        tags.add(COMM(encoding=3, lang="zho", desc="desc", text=description))
    if cover_data:
        tags.add(
            APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,
                desc="Cover",
                data=cover_data,
            )
        )

    tags.save(path)
    return True


def _write_mp4_tags(
    path: Path,
    *,
    title: str,
    author: str,
    narrator: str,
    album: str,
    track_number: int,
    description: str,
    cover_data: Optional[bytes],
) -> bool:
    from mutagen.mp4 import MP4, MP4Cover

    try:
        audio = MP4(path)
    except Exception:
        return False

    audio.tags = audio.tags or {}
    audio.tags["\xa9nam"] = [title]
    if author:
        audio.tags["\xa9ART"] = [author]
    if narrator:
        audio.tags["aART"] = [narrator]
    if album:
        audio.tags["\xa9alb"] = [album]
    if track_number > 0:
        audio.tags["trkn"] = [(track_number, 0)]
    if description:
        audio.tags["\xa9cmt"] = [description]
    if cover_data:
        audio.tags["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]

    audio.save()
    return True


def save_cover(cover_data: bytes, target_dir: Path) -> Path:
    """保存封面到目录，返回封面路径。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    cover_path = target_dir / "cover.jpg"
    cover_path.write_bytes(cover_data)
    return cover_path
