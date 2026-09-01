"""HTTP helpers for Apple Podcasts-compatible audio/cover serving.

Apple Podcasts requires:
- HTTP HEAD on enclosure URLs
- byte-range (Accept-Ranges / 206)
- enclosure type in its allowed MIME set (audio/mpeg, audio/x-m4a, ...)
- native file bytes, not a JSON envelope
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from fastapi.responses import FileResponse, Response

# Apple Podcasts enclosure `type` whitelist (see Apple Podcasts RSS spec)
APPLE_AUDIO_MIME: Dict[str, str] = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/x-m4a",
    ".m4b": "audio/x-m4a",
    ".aac": "audio/x-m4a",
    ".mp4": "audio/x-m4a",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".wma": "audio/x-ms-wma",
    ".aiff": "audio/aiff",
}

IMAGE_MIME: Dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def media_type_for(path: Path) -> str:
    """Return Apple-compatible MIME type for a media file path."""
    ext = path.suffix.lower()
    return APPLE_AUDIO_MIME.get(ext) or IMAGE_MIME.get(ext) or "application/octet-stream"


def media_headers() -> Dict[str, str]:
    """Headers Apple Podcasts / iOS AVPlayer expect on enclosure fetches."""
    return {
        "Accept-Ranges": "bytes",
        # Allow the client to store the episode; no-store can make iOS refuse playback.
        "Cache-Control": "public, max-age=300",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": (
            "Accept-Ranges, Content-Range, Content-Length, Content-Type"
        ),
        "X-Content-Type-Options": "nosniff",
    }


def serve_media_file(path: Path, media_type: Optional[str] = None) -> Response:
    """
    Serve audio/cover with HEAD + Range support via FileResponse.

    FileResponse (Starlette 0.24+) reads the Range header from the ASGI scope
    and returns 206 Partial Content. HEAD is handled by omitting the body.
    """
    mime = media_type or media_type_for(path)
    headers = media_headers()
    kwargs = {
        "path": str(path),
        "media_type": mime,
        "headers": headers,
    }
    try:
        return FileResponse(**kwargs, content_disposition_type="inline")
    except TypeError:
        return FileResponse(**kwargs)
