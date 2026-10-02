"""Network-facing work: video downloads (yt-dlp) and album-cover lookup."""
from __future__ import annotations

from pathlib import Path

import httpx

from .links import apple_artwork_hd, apple_music_ids, safe_filename

HTTP_TIMEOUT = 20.0
MAX_VIDEO_BYTES = 500 * 1024 * 1024  # skip anything larger than 500 MB


def download_video(url: str, dest: Path, audio_only: bool = False) -> Path:
    """Download one public video with yt-dlp and return the file path."""
    import yt_dlp  # imported lazily so tests can run without network

    opts = {
        "outtmpl": str(dest / "%(title).80s [%(id)s].%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": False,
        "max_filesize": MAX_VIDEO_BYTES,
        "socket_timeout": 30,
    }
    if audio_only:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}]
    else:
        # Prefer a single MP4 that plays everywhere; merge with ffmpeg if needed.
        opts["format"] = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"
        opts["merge_output_format"] = "mp4"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
    if audio_only:
        path = path.with_suffix(".mp3")
    if not path.exists():  # merged output may have a different extension
        matches = sorted(dest.glob(f"*[{info['id']}].*"))
        if not matches:
            raise RuntimeError("Download finished but no file was produced.")
        path = matches[0]
    return path


def lookup_cover(url: str, platform: str, client: httpx.Client) -> dict:
    """Return {"title", "artist", "cover_url"} for a Spotify or Apple Music link."""
    if platform == "spotify":
        r = client.get("https://open.spotify.com/oembed", params={"url": url})
        r.raise_for_status()
        data = r.json()
        if not data.get("thumbnail_url"):
            raise RuntimeError("Spotify returned no artwork for this link.")
        return {"title": data.get("title", "Spotify"), "artist": "", "cover_url": data["thumbnail_url"]}

    if platform == "apple_music":
        track_id, collection_id = apple_music_ids(url)
        lookup_id = track_id or collection_id
        if not lookup_id:
            raise RuntimeError("Could not find a track or album ID in this link.")
        r = client.get("https://itunes.apple.com/lookup", params={"id": lookup_id})
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results or not results[0].get("artworkUrl100"):
            raise RuntimeError("Apple Music returned no artwork for this link.")
        item = results[0]
        return {
            "title": item.get("trackName") or item.get("collectionName", "Apple Music"),
            "artist": item.get("artistName", ""),
            "cover_url": apple_artwork_hd(item["artworkUrl100"]),
        }

    raise RuntimeError("Unsupported music link.")


def save_cover(meta: dict, dest: Path, client: httpx.Client) -> Path:
    r = client.get(meta["cover_url"], follow_redirects=True)
    r.raise_for_status()
    ext = ".png" if "png" in r.headers.get("content-type", "") else ".jpg"
    base = safe_filename(f"{meta['artist']} - {meta['title']}" if meta["artist"] else meta["title"], "cover")
    path = dest / f"{base}{ext}"
    path.write_bytes(r.content)
    return path
