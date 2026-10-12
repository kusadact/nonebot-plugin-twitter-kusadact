"""Resolve Nitter image proxy paths to their returned Twitter CDN resource."""

from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse


def image_candidates(url: str) -> list[str]:
    candidates = [url]
    path = unquote(urlparse(url).path)
    if path.startswith("/pic/"):
        media = path.removeprefix("/pic/")
        if media.startswith(("http://", "https://")):
            candidates.append(media)
        elif media.startswith("orig/media/"):
            filename = PurePosixPath(media.removeprefix("orig/media/"))
            candidates.append(f"https://pbs.twimg.com/media/{filename.stem}?format={filename.suffix.lstrip('.')}&name=large")
        elif media:
            candidates.append("https://pbs.twimg.com/" + media.lstrip("/"))
    return list(dict.fromkeys(candidates))
