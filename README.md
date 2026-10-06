# Downloader

A small web app that downloads public videos from **YouTube, Instagram, TikTok and Pinterest**
(up to 10 links at once, MP4 or MP3), and saves **album covers** from **Spotify and Apple Music**
links (up to 10 at once, Apple artwork at up to 3000×3000).

## Run

```bash
pip install -r requirements.txt     # ffmpeg must also be installed (merging video/audio, MP3)
uvicorn app.main:app --port 8000    # open http://localhost:8000
pytest -q                           # no network needed
```

## Password

Set the `SITE_PASSWORD` environment variable to require a password. Visitors see a sign-in screen
and stay signed in for 30 days; changing the password signs everyone out. After 10 wrong passwords
in 15 minutes, sign-in pauses for that visitor. With no `SITE_PASSWORD` set (local use, tests) the site is open.

## Install as an app

The site is a web app (PWA), so it can be added to a phone's home screen and opens full screen.

- **Android (Chrome):** tap **Install** in the top bar, or menu → **Add to Home screen**. Once installed,
  "Downloader" appears in the share sheet: share a TikTok/YouTube/Instagram link to it and the link is filled in.
- **iPhone/iPad (Safari):** Share → **Add to Home Screen**.
- **Desktop (Chrome/Edge):** the install icon in the address bar.

Files from a batch can be saved one by one or together with **Save all (ZIP)**.

## API

| Method | Path | Body |
|---|---|---|
| POST | `/api/video` | `{"urls": [...≤10], "audio_only": false}` |
| POST | `/api/music/covers` | `{"urls": [...≤10]}` |
| GET | `/files/{batch_id}/{name}` | serves a finished file |
| GET | `/files/{batch_id}/all.zip` | every finished file in a batch as one ZIP |
| POST | `/api/login` | `{"password": "..."}` sets the sign-in cookie |

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
