"""FastAPI app: batch video downloads and album-cover downloads (max 10 links each)."""
from __future__ import annotations

import os
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import services
from .links import MUSIC_HOSTS, VIDEO_HOSTS, LinkError, parse_batch

ROOT = Path(__file__).resolve().parent.parent
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", ROOT / "downloads"))
FILE_TTL_SECONDS = 3600
WORKERS = 4

app = FastAPI(title="Downloader")


class VideoBatch(BaseModel):
    urls: list[str]
    audio_only: bool = False


class MusicBatch(BaseModel):
    urls: list[str]


def _cleanup_old_batches() -> None:
    if not DOWNLOAD_DIR.exists():
        return
    cutoff = time.time() - FILE_TTL_SECONDS
    for d in DOWNLOAD_DIR.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)


def _new_batch_dir() -> tuple[str, Path]:
    _cleanup_old_batches()
    batch_id = uuid.uuid4().hex
    path = DOWNLOAD_DIR / batch_id
    path.mkdir(parents=True)
    return batch_id, path


def _parse(urls: list[str], table) -> list[dict]:
    try:
        return parse_batch(urls, table)
    except LinkError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _result(item: dict, batch_id: str, path: Path | None = None, error: str | None = None, **extra) -> dict:
    out = {"url": item["url"], "platform": item["platform"], "ok": error is None, **extra}
    if path is not None:
        out["file"] = path.name
        out["download_url"] = f"/files/{batch_id}/{path.name}"
    if error:
        out["error"] = error
    return out


@app.post("/api/video")
def download_videos(batch: VideoBatch) -> dict:
    items = _parse(batch.urls, VIDEO_HOSTS)
    batch_id, dest = _new_batch_dir()

    def work(item: dict) -> dict:
        if item["platform"] is None:
            return _result(item, batch_id, error="Unsupported link. Use YouTube, Instagram, TikTok or Pinterest.")
        # Each link gets its own subfolder so concurrent downloads never collide.
        sub = dest / uuid.uuid4().hex[:8]
        sub.mkdir()
        try:
            path = services.download_video(item["url"], sub, batch.audio_only)
            final = dest / path.name
            if final.exists():
                final = dest / f"{sub.name}-{path.name}"
            path.rename(final)
            return _result(item, batch_id, final)
        except Exception as e:  # yt-dlp raises many types; report per link
            return _result(item, batch_id, error=_short_error(e))
        finally:
            shutil.rmtree(sub, ignore_errors=True)

    with ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(work, items))
    return {"batch_id": batch_id, "results": results}


@app.post("/api/music/covers")
def download_covers(batch: MusicBatch) -> dict:
    items = _parse(batch.urls, MUSIC_HOSTS)
    batch_id, dest = _new_batch_dir()

    def work(item: dict) -> dict:
        if item["platform"] is None:
            return _result(item, batch_id, error="Unsupported link. Use Spotify or Apple Music.")
        try:
            with httpx.Client(timeout=services.HTTP_TIMEOUT, follow_redirects=True) as client:
                meta = services.lookup_cover(item["url"], item["platform"], client)
                path = services.save_cover(meta, dest, client)
            return _result(item, batch_id, path, title=meta["title"], artist=meta["artist"])
        except Exception as e:
            return _result(item, batch_id, error=_short_error(e))

    with ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(work, items))
    return {"batch_id": batch_id, "results": results}


@app.get("/files/{batch_id}/{name}")
def get_file(batch_id: str, name: str):
    base = DOWNLOAD_DIR.resolve()
    path = (base / batch_id / name).resolve()
    # Block path traversal (e.g. name="../../etc/passwd").
    if base not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found or expired.")
    return FileResponse(path, filename=name)


def _short_error(e: Exception) -> str:
    msg = str(e).replace("ERROR: ", "").strip() or type(e).__name__
    return msg[:300]


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
