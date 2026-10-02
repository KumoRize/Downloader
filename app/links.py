"""URL parsing and validation. Pure functions, no network access."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

MAX_LINKS = 10

VIDEO_HOSTS = {
    "youtube": ("youtube.com", "youtu.be", "music.youtube.com"),
    "instagram": ("instagram.com",),
    "tiktok": ("tiktok.com",),
    "pinterest": ("pinterest.com", "pin.it"),
}
MUSIC_HOSTS = {
    "spotify": ("open.spotify.com", "spotify.link"),
    "apple_music": ("music.apple.com",),
}


class LinkError(ValueError):
    """Raised when a batch of links is invalid as a whole."""


def _host_matches(host: str, domains: tuple[str, ...]) -> bool:
    host = host.lower().split(":")[0]
    return any(host == d or host.endswith("." + d) for d in domains)


def detect_platform(url: str, table: dict[str, tuple[str, ...]]) -> str | None:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    # Also matches regional Pinterest domains such as pinterest.co.uk.
    host = re.sub(r"^(?:[a-z]{2}\.)?pinterest\.[a-z.]+$", "pinterest.com", parsed.netloc.lower())
    for name, domains in table.items():
        if _host_matches(host, domains):
            return name
    return None


def parse_batch(raw: list[str], table: dict[str, tuple[str, ...]]) -> list[dict]:
    """Clean, dedupe and classify up to MAX_LINKS links.

    Returns one dict per unique link: {"url", "platform"}; platform is None
    for unsupported links so the caller can report them per item.
    """
    seen: list[str] = []
    for item in raw:
        url = (item or "").strip()
        if url and url not in seen:
            seen.append(url)
    if not seen:
        raise LinkError("Paste at least one link.")
    if len(seen) > MAX_LINKS:
        raise LinkError(f"At most {MAX_LINKS} links per batch (got {len(seen)}).")
    return [{"url": u, "platform": detect_platform(u, table)} for u in seen]


def apple_music_ids(url: str) -> tuple[str | None, str | None]:
    """Return (track_id, collection_id) from an Apple Music URL.

    https://music.apple.com/us/album/name/1440857781?i=1440857791
      -> ("1440857791", "1440857781")
    https://music.apple.com/us/song/name/1440857791 -> ("1440857791", None)
    """
    parsed = urlparse(url)
    track = parse_qs(parsed.query).get("i", [None])[0]
    m = re.search(r"/(album|song|playlist)/(?:[^/]+/)?(\d+)", parsed.path)
    if not m:
        return track, None
    kind, num = m.groups()
    if kind == "song":
        return num, None
    return track, num


def apple_artwork_hd(url: str, size: int = 3000) -> str:
    """Upscale an iTunes artwork URL (e.g. .../100x100bb.jpg) to size x size."""
    return re.sub(r"/\d+x\d+(bb)?\.(jpg|png|webp)$", rf"/{size}x{size}bb.jpg", url)


def safe_filename(name: str, default: str = "file") -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name).strip(" .")
    return name[:120] or default
