from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main, services
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
    assert res[1]["error"] == "Private video"
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
