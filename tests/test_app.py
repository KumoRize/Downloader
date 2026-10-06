from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, main, services
from app.links import (MUSIC_HOSTS, VIDEO_HOSTS, LinkError, apple_artwork_hd,
                       apple_music_ids, detect_platform, parse_batch, safe_filename)


@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
    ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
    ("https://www.instagram.com/reel/abc123/", "instagram"),
    ("https://www.tiktok.com/@user/video/123", "tiktok"),
    ("https://vm.tiktok.com/ZM123/", "tiktok"),
    ("https://www.pinterest.com/pin/123/", "pinterest"),
    ("https://uk.pinterest.co.uk/pin/123/", "pinterest"),
    ("https://pin.it/abc", "pinterest"),
    ("https://evilyoutube.com/watch?v=x", None),
    ("ftp://youtube.com/x", None),
    ("not a url", None),
])
def test_detect_video_platform(url, expected):
    assert detect_platform(url, VIDEO_HOSTS) == expected


def test_detect_music_platform():
    assert detect_platform("https://open.spotify.com/track/abc", MUSIC_HOSTS) == "spotify"
    assert detect_platform("https://music.apple.com/us/album/x/1", MUSIC_HOSTS) == "apple_music"
    assert detect_platform("https://www.youtube.com/watch?v=x", MUSIC_HOSTS) is None


def test_parse_batch_dedupes_and_limits():
    out = parse_batch(["https://youtu.be/a", " https://youtu.be/a ", "", "https://x.com/b"], VIDEO_HOSTS)
    assert [o["platform"] for o in out] == ["youtube", None]
    with pytest.raises(LinkError):
        parse_batch(["", "  "], VIDEO_HOSTS)
    with pytest.raises(LinkError):
        parse_batch([f"https://youtu.be/{i}" for i in range(11)], VIDEO_HOSTS)
    assert len(parse_batch([f"https://youtu.be/{i}" for i in range(10)], VIDEO_HOSTS)) == 10


def test_apple_ids_and_artwork():
    assert apple_music_ids("https://music.apple.com/us/album/name/1440857781?i=1440857791") == ("1440857791", "1440857781")
    assert apple_music_ids("https://music.apple.com/us/album/name/1440857781") == (None, "1440857781")
    assert apple_music_ids("https://music.apple.com/us/song/name/1440857791") == ("1440857791", None)
    assert apple_artwork_hd("https://is1.mzstatic.com/a/b/100x100bb.jpg").endswith("/3000x3000bb.jpg")


def test_safe_filename():
    assert safe_filename('a/b:c*?"d') == "a_b_c_d"
    assert safe_filename("...") == "file"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path)
    return TestClient(main.app)


def test_video_endpoint_reports_per_link(client, monkeypatch):
    def fake_download(url, dest: Path, audio_only):
        if "fail" in url:
            raise RuntimeError("ERROR: Private video")
        p = dest / ("song.mp3" if audio_only else "clip.mp4")
        p.write_bytes(b"data")
        return p

    monkeypatch.setattr(services, "download_video", fake_download)
    r = client.post("/api/video", json={"urls": [
        "https://youtu.be/ok", "https://youtu.be/fail", "https://example.com/x"]})
    assert r.status_code == 200
    res = r.json()["results"]
    assert res[0]["ok"] and res[0]["file"] == "clip.mp4"
    assert res[1]["error"] == "This post is private."
    assert "Unsupported" in res[2]["error"]
    assert client.get(res[0]["download_url"]).content == b"data"


def test_video_endpoint_rejects_more_than_10(client):
    r = client.post("/api/video", json={"urls": [f"https://youtu.be/{i}" for i in range(11)]})
    assert r.status_code == 400


def test_cover_endpoint(client, monkeypatch):
    monkeypatch.setattr(services, "lookup_cover", lambda url, platform, c: {
        "title": "Song", "artist": "Artist", "cover_url": "https://img/x.jpg"})

    def fake_save(meta, dest, c):
        p = dest / "Artist - Song.jpg"
        p.write_bytes(b"jpg")
        return p

    monkeypatch.setattr(services, "save_cover", fake_save)
    r = client.post("/api/music/covers", json={"urls": ["https://open.spotify.com/track/abc"]})
    item = r.json()["results"][0]
    assert item["ok"] and item["artist"] == "Artist"
    assert client.get(item["download_url"]).content == b"jpg"


def test_file_route_blocks_traversal(client):
    assert client.get("/files/x/..%2F..%2Fetc%2Fpasswd").status_code == 404


def test_login_flow(client, monkeypatch):
    monkeypatch.setenv("SITE_PASSWORD", "s3cret")
    main.login_limiter.reset("testclient")

    # Pages redirect to the login screen and keep where you were going (incl. shared links).
    r = client.get("/?url=https%3A%2F%2Fyoutu.be%2Fa", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2F%3Furl%3Dhttps%253A%252F%252Fyoutu.be%252Fa"
    # API and files answer 401 instead of redirecting.
    assert client.post("/api/video", json={"urls": ["https://youtu.be/a"]}).status_code == 401
    assert client.get("/files/x/y.mp4").status_code == 401
    # What the browser needs to install the app stays public.
    for path in ("/login", "/app.css", "/manifest.webmanifest", "/sw.js", "/icons/icon-192.png", "/offline.html"):
        assert client.get(path).status_code == 200, path

    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    r = client.post("/api/login", json={"password": "s3cret"})
    assert r.status_code == 200
    assert "httponly" in r.headers["set-cookie"].lower()
    assert client.get("/").status_code == 200

    client.post("/api/logout")
    assert client.get("/", follow_redirects=False).status_code == 303


def test_changing_password_signs_everyone_out(client, monkeypatch):
    monkeypatch.setenv("SITE_PASSWORD", "old")
    main.login_limiter.reset("testclient")
    client.post("/api/login", json={"password": "old"})
    assert client.get("/").status_code == 200
    monkeypatch.setenv("SITE_PASSWORD", "new")
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_is_rate_limited(client, monkeypatch):
    monkeypatch.setenv("SITE_PASSWORD", "s3cret")
    main.login_limiter.reset("testclient")
    for _ in range(auth.MAX_FAILURES):
        assert client.post("/api/login", json={"password": "bad"}).status_code == 401
    r = client.post("/api/login", json={"password": "s3cret"})  # even the right one waits
    assert r.status_code == 429 and "Try again" in r.json()["detail"]
    main.login_limiter.reset("testclient")


def test_limiter_window_expires():
    lim = auth.LoginLimiter(max_failures=2, window=60)
    lim.record_failure("ip", now=0)
    lim.record_failure("ip", now=1)
    assert lim.retry_after("ip", now=2) == 58
    assert lim.retry_after("ip", now=61) == 0


@pytest.mark.parametrize("target,expected", [
    ("/?tab=music", "/?tab=music"), (None, "/"), ("//evil.com", "/"),
    ("https://evil.com", "/"), ("/\\evil.com", "/"),
])
def test_safe_next(target, expected):
    assert auth.safe_next(target) == expected


def test_client_id_uses_last_forwarded_ip():
    assert auth.client_id({"x-forwarded-for": "6.6.6.6, 1.2.3.4"}, "x") == "1.2.3.4"
    assert auth.client_id({}, "fallback") == "fallback"


def test_no_password_when_unset(client, monkeypatch):
    monkeypatch.delenv("SITE_PASSWORD", raising=False)
    assert client.get("/").status_code == 200


def test_zip_of_batch(client, monkeypatch, tmp_path):
    import io
    import zipfile

    def fake_download(url, dest: Path, audio_only):
        p = dest / f"{url[-1]}.mp4"
        p.write_bytes(url.encode())
        return p

    monkeypatch.setattr(services, "download_video", fake_download)
    r = client.post("/api/video", json={"urls": ["https://youtu.be/a", "https://youtu.be/b"]})
    batch_id = r.json()["batch_id"]
    z = client.get(f"/files/{batch_id}/all.zip")
    assert z.status_code == 200
    names = sorted(zipfile.ZipFile(io.BytesIO(z.content)).namelist())
    assert names == ["a.mp4", "b.mp4"]
    assert client.get(f"/files/{batch_id}/all.zip").status_code == 200  # cached copy
    assert client.get("/files/nope/all.zip").status_code == 404
    assert client.get("/files/..%2F..%2Fetc/all.zip").status_code == 404


@pytest.mark.parametrize("raw,expected", [
    ("ERROR: [youtube] abc: Sign in to confirm you're not a bot", "YouTube blocked"),
    ("ERROR: [Instagram] x: Unable to download webpage: ('Unable to connect to proxy', ...)", "Couldn't reach"),
    ("ERROR: [generic] Unsupported URL: https://example.com", "This link isn't a video page"),
    ("ERROR: [TikTok] 1: Something odd happened; please report this issue on https://github.com/yt-dlp", "Something odd happened"),
])
def test_friendly_errors(raw, expected):
    assert main._short_error(RuntimeError(raw)).startswith(expected)
