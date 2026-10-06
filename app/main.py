"""FastAPI app: batch video downloads and album-cover downloads (max 10 links each)."""
from __future__ import annotations

import os
import re
import secrets
import shutil
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, services
from .links import MUSIC_HOSTS, VIDEO_HOSTS, LinkError, parse_batch

ROOT = Path(__file__).resolve().parent.parent
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", ROOT / "downloads"))
FILE_TTL_SECONDS = 3600
WORKERS = 4

app = FastAPI(title="Downloader")


login_limiter = auth.LoginLimiter()


@app.middleware("http")
async def require_login(request: Request, call_next):
    # Protection is on only when SITE_PASSWORD is set (it is unset for local dev and tests).
    password = auth.site_password()
    path = request.url.path
    if not password or auth.is_public(path) or auth.is_signed_in(request.cookies.get(auth.COOKIE_NAME), password):
        return await call_next(request)
    if path.startswith(("/api/", "/files/")):
        return JSONResponse({"detail": "Sign in first."}, status_code=401)
    target = path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)


class Login(BaseModel):
    password: str


@app.post("/api/login")
def login(body: Login, request: Request, response: Response) -> dict:
    password = auth.site_password()
    if not password:
        return {"ok": True}
    client = auth.client_id(request.headers, request.client.host if request.client else "unknown")
    wait = login_limiter.retry_after(client)
    if wait:
        minutes = (wait + 59) // 60
        raise HTTPException(status_code=429, detail=f"Too many wrong passwords. Try again in {minutes} min.")
    if not secrets.compare_digest(body.password.encode(), password.encode()):
        login_limiter.record_failure(client)
        raise HTTPException(status_code=401, detail="Wrong password.")
    login_limiter.reset(client)
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(auth.COOKIE_NAME, auth.session_token(password), max_age=auth.COOKIE_MAX_AGE,
                        httponly=True, secure=https, samesite="lax")
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(auth.COOKIE_NAME)
    return {"ok": True}


@app.get("/login")
def login_page():
    return FileResponse(ROOT / "static" / "login.html")


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


@app.get("/files/{batch_id}/all.zip")
def get_batch_zip(batch_id: str):
    base = DOWNLOAD_DIR.resolve()
    folder = (base / batch_id).resolve()
    if folder.parent != base or not folder.is_dir():
        raise HTTPException(status_code=404, detail="Batch not found or expired.")
    files = sorted(p for p in folder.iterdir()
                   if p.is_file() and p.name != "all.zip" and not p.name.endswith(".part"))
    if not files:
        raise HTTPException(status_code=404, detail="No finished files in this batch.")
    zip_path = folder / "all.zip"
    if not zip_path.exists():
        # Videos are already compressed, so store them as-is (fast, same size).
        tmp = folder / f"all.{uuid.uuid4().hex[:8]}.part"  # unique, so two taps can't clash
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
            for f in files:
                zf.write(f, f.name)
        tmp.replace(zip_path)
    return FileResponse(zip_path, filename=f"downloads-{batch_id[:6]}.zip")


@app.get("/files/{batch_id}/{name}")
def get_file(batch_id: str, name: str):
    base = DOWNLOAD_DIR.resolve()
    path = (base / batch_id / name).resolve()
    # Block path traversal (e.g. name="../../etc/passwd").
    if base not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found or expired.")
    return FileResponse(path, filename=name)


# Plain-language messages for the errors people actually hit, checked in order.
_FRIENDLY_ERRORS = [
    (("sign in to confirm", "not a bot"), "YouTube blocked this server (\"confirm you're not a bot\"). Try again later."),
    (("private video", "this video is private", "private account", "is private"), "This post is private."),
    (("login required", "requires authentication", "log in", "login_required", "cookies"), "This post can only be seen when logged in."),
    (("age-restricted", "age restricted", "confirm your age"), "This video is age-restricted."),
    (("max-filesize", "larger than max"), "This file is over 500 MB, so it was skipped."),
    (("video unavailable", "not available", "has been removed", "404"), "This post is unavailable or was deleted."),
    (("unsupported url",), "This link isn't a video page. Copy the link to the post itself."),
    (("unable to connect", "timed out", "connection", "proxy", "network is unreachable", "name resolution"),
     "Couldn't reach the site. Try again in a minute."),
]


def _short_error(e: Exception) -> str:
    raw = str(e).replace("ERROR: ", "").strip() or type(e).__name__
    low = raw.lower()
    for needles, message in _FRIENDLY_ERRORS:
        if any(n in low for n in needles):
            return message
    # Unknown error: keep the first line, drop yt-dlp's "[site] id:" prefix and bug-report footer.
    line = raw.splitlines()[0]
    line = re.sub(r"^\[[^\]]+\]\s*[^:]+:\s*", "", line)
    line = re.split(r";\s*please report", line, flags=re.I)[0]
    return line[:160]


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
