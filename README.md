# Downloader

A small web app that downloads public videos from **YouTube, Instagram, TikTok and Pinterest**
(up to 10 links at once, MP4 or MP3), and saves **album covers** from **Spotify and Apple Music**
links (up to 10 at once, Apple artwork at up to 3000×3000).

## Run

```bash
pip install -r requirements.txt     # ffmpeg must also be installed (merging video/audio, MP3)
uvicorn app.main:app --port 8000    # open http://localhost:8000
pytest -q                           # 19 tests, no network needed
```

## API

| Method | Path | Body |
|---|---|---|
| POST | `/api/video` | `{"urls": [...≤10], "audio_only": false}` |
| POST | `/api/music/covers` | `{"urls": [...≤10]}` |
| GET | `/files/{batch_id}/{name}` | serves a finished file |

Each link gets its own result (`ok`, `download_url` or `error`), so one bad link never fails the batch.
Duplicate links are removed. More than 10 links returns HTTP 400. Files are deleted after 1 hour.

## Limits

- Only public posts work. Private, age-restricted or login-only posts return an error for that link.
- Downloads run 4 at a time. Files over 500 MB are skipped.
- The app does **not** download audio from Spotify or Apple Music. Those streams are DRM-protected,
  and getting around that breaks their terms and copyright law in most countries.
- Downloading from YouTube, Instagram, TikTok or Pinterest may break their terms of service.
  Only download content you own or have permission to save.
- Keep `yt-dlp` updated (`pip install -U yt-dlp`). Sites change often and old versions stop working.
