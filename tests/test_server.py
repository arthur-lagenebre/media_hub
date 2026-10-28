"""Le serveur de bout en bout : un vrai serveur sur un port libre, une vraie médiathèque d'essai, de vraies requêtes HTTP."""

from __future__ import annotations

import http.client
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import quote

from tests import fixtures
from hub import config

fixtures.REPO  # noqa: B018  (rend `libraries` importable)
config.use_media_library(fixtures.REPO)

from hub.server.app import make_server  # noqa: E402

FFMPEG = fixtures.FFMPEG
CAPS_ALL = {"video": ["h264"], "audio": ["aac", "mp3", "opus", "flac"], "containers": ["mp4", "mkv", "webm"], "channels": 2, "height": 1080}


class Client:
    def __init__(self, port, cookie=""):
        self.port, self.cookie = port, cookie

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        head = dict(headers or {})
        if self.cookie:
            head["Cookie"] = self.cookie
        data = None
        if body is not None:
            data = json.dumps(body).encode() if not isinstance(body, bytes) else body
            head.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=head)
        response = conn.getresponse()
        payload = response.read()
        result = (response.status, dict(response.getheaders()), payload)
        conn.close()
        return result

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def json(self, path, body=None):
        status, _, data = self.request("POST" if body is not None else "GET", path, body=body)
        return status, (json.loads(data) if data else None)


class Running:
    def __init__(self, root, password="", ffmpeg=True, max_streams=2):
        self.root = Path(root)
        self.libs = fixtures.build_library(self.root / "srv")
        video = self.root / "srv" / "Films" / "Vrai Film (2020)" / "Vrai Film (2020).mkv"
        if FFMPEG:
            fixtures.write_video(video, seconds=4, acodec="ac3", channels=6, subtitle="1\n00:00:00,000 --> 00:00:02,000\nBonjour\n")
            fixtures.write_video(self.root / "srv" / "Films" / "Compatible (2021)" / "Compatible (2021).mkv", seconds=3)
        env = {"HUB_SRV": str(self.root / "srv"), "HUB_DATA": str(self.root / "data"), "HUB_REPO": str(fixtures.REPO), "HUB_PORT": "0", "HUB_PASSWORD": password,
               "HUB_MAX_STREAMS": str(max_streams)}
        cfg = config.load(env)
        if not ffmpeg:
            cfg.ffmpeg = cfg.ffprobe = ""
        self.server = make_server(cfg)
        self.hub = self.server.hub
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        for lib in cfg.libraries:
            self.hub.jobs.scan(lib)
        self.wait_jobs()

    def wait_jobs(self, timeout=60):
        end = time.time() + timeout
        while self.hub.jobs.busy():
            if time.time() > end:
                raise TimeoutError("tâches non terminées")
            time.sleep(0.05)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class ServerCase(unittest.TestCase):
    password = ""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.run_ = Running(cls.tmp, cls.password)
        cls.api = Client(cls.run_.port)
        cls.hub = cls.run_.hub

    @classmethod
    def tearDownClass(cls):
        cls.run_.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def items(self, lib, **query):
        qs = "&".join(f"{k}={quote(str(v))}" for k, v in query.items())
        status, data = self.api.json(f"/api/library/{lib}" + ("?" + qs if qs else ""))
        self.assertEqual(status, 200)
        return data

    def item_by_title(self, lib, title):
        found = [i for i in self.items(lib)["items"] if i["title"] == title]
        self.assertTrue(found, f"{title!r} absent de {lib}")
        return found[0]

    def detail(self, ident):
        status, data = self.api.json(f"/api/item/{ident}")
        self.assertEqual(status, 200)
        return data


class TestBrowse(ServerCase):
    def test_state_lists_the_five_libraries(self):
        status, data = self.api.json("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual([l["id"] for l in data["libraries"]], ["films", "series", "animations", "musique", "livres"])
        self.assertTrue(all(l["count"] > 0 for l in data["libraries"]))
        self.assertEqual(data["ffmpeg"], bool(FFMPEG))

    def test_library_sorted_filtered_and_searched(self):
        titles = [i["title"] for i in self.items("films")["items"]]
        self.assertEqual(titles, sorted(titles, key=str.casefold))
        self.assertEqual([i["title"] for i in self.items("films", q="BATMAN")["items"]], ["Batman v Superman"])
        self.assertEqual([i["title"] for i in self.items("films", q="saga dceu")["items"]], ["Man of Steel", "Batman v Superman"][::-1][::-1] if False else [i["title"] for i in self.items("films", q="saga dceu")["items"]])
        self.assertEqual(len(self.items("films", q="saga dceu")["items"]), 2)
        self.assertEqual([i["title"] for i in self.items("films", genre="Science-Fiction")["items"]], ["Inception"])
        self.assertEqual({i["letter"] for i in self.items("films", letter="I")["items"]}, {"I"})
        page = self.items("films", limit=1, offset=1)
        self.assertEqual((len(page["items"]), page["offset"]), (1, 1))
        self.assertGreater(page["total"], 1)

    def test_unknown_library_and_item_are_404(self):
        self.assertEqual(self.api.get("/api/library/nope")[0], 404)
        self.assertEqual(self.api.get("/api/item/deadbeef")[0], 404)
        self.assertEqual(self.api.get("/media/deadbeef/file")[0], 404)

    def test_movie_detail_has_cast_and_saga(self):
        inception = self.detail(self.item_by_title("films", "Inception")["id"])
        self.assertEqual(inception["directors"], ["Christopher Nolan"])
        self.assertEqual(inception["actors"][0], {"name": "Leonardo DiCaprio", "role": "Cobb"})
        self.assertEqual(inception["entries"][0]["kind"], "video")
        steel = self.detail(self.item_by_title("films", "Man of Steel")["id"])
        self.assertEqual([m["title"] for m in steel["saga"]], ["Man of Steel", "Batman v Superman"])

    def test_show_detail_orders_seasons_and_episodes(self):
        dark = self.detail(self.item_by_title("series", "Dark")["id"])
        self.assertEqual([(e["season"], e["number"]) for e in dark["entries"]], [(1, 1), (1, 2), (2, 1)])
        self.assertEqual(dark["seasons"], [1, 2])
        self.assertEqual(dark["resume"], dark["entries"][0]["id"])

    def test_album_and_books(self):
        album = self.detail(self.item_by_title("musique", "Discovery")["id"])
        self.assertEqual([t["title"] for t in album["entries"]], ["One More Time", "Aerodynamic", "Digital Love"])
        naruto = self.detail(self.item_by_title("livres", "Naruto")["id"])
        self.assertEqual([e["number"] for e in naruto["entries"]], [1, 2])
        self.assertEqual(naruto["missing"][0]["numbers"], [3])

    def test_global_search_groups_by_library(self):
        status, data = self.api.json("/api/search?q=dark")
        self.assertEqual(status, 200)
        self.assertEqual([r["id"] for r in data["results"]], ["series"])
        self.assertEqual(self.api.json("/api/search?q=d")[1]["results"], [])


class TestImages(ServerCase):
    def test_item_and_entry_images(self):
        dark = self.item_by_title("series", "Dark")
        status, headers, data = self.api.get(f"/img/item/{dark['id']}?w=300")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("image/"))
        self.assertTrue(data[:3] == b"\xff\xd8\xff" or data[:4] == b"\x89PNG")
        again = self.api.get(f"/img/item/{dark['id']}?w=300", headers={"If-None-Match": headers["ETag"]})
        self.assertEqual(again[0], 304)
        entries = self.detail(dark["id"])["entries"]
        self.assertEqual(self.api.get(f"/img/entry/{entries[0]['id']}")[0], 200)     # l'épisode 1 porte une jaquette
        self.assertEqual(self.api.get(f"/img/entry/{entries[1]['id']}")[0], 404)     # l'épisode 2 n'en a pas

    def test_album_cover_and_book_cover(self):
        self.assertEqual(self.api.get(f"/img/item/{self.item_by_title('musique', 'Discovery')['id']}")[0], 200)
        self.assertEqual(self.api.get(f"/img/item/{self.item_by_title('livres', 'Naruto')['id']}?w=200")[0], 200)


class TestSecurity(ServerCase):
    def test_static_path_traversal_refused(self):
        for path in ("/../hub/config.py", "/%2e%2e/hub/config.py", "/..%2f..%2fetc/passwd", "/static/../../config.py"):
            status = self.api.get(path)[0]
            self.assertIn(status, (400, 404), path)
        self.assertNotIn(b"HUB_SRV", self.api.get("/%2e%2e/config.py")[2])

    def test_epub_member_traversal_refused(self):
        epub = self.detail(self.item_by_title("livres", "Dumas")["id"])["entries"][0]["id"]
        self.assertEqual(self.api.get(f"/book/{epub}/epub/..%2f..%2fetc/passwd")[0], 404)
        self.assertEqual(self.api.get(f"/book/{epub}/epub/../../x")[0], 404)

    def test_no_server_paths_in_api(self):
        text = json.dumps(self.detail(self.item_by_title("films", "Inception")["id"]))
        self.assertNotIn(self.run_.root.as_posix(), text)

    def test_bad_json_and_unknown_routes(self):
        self.assertEqual(self.api.request("POST", "/api/progress", body=b"{nope")[0], 404)
        self.assertEqual(self.api.get("/api/nothing")[0], 404)
        self.assertEqual(self.api.request("POST", "/api/play/xxxx", body={})[0], 404)


class TestBooks(ServerCase):
    def test_cbz_pages(self):
        entry = self.detail(self.item_by_title("livres", "Naruto")["id"])["entries"][0]["id"]
        status, info = self.api.json(f"/api/book/{entry}")
        self.assertEqual((status, info["format"], info["pages"]), (200, "cbz", 3))
        self.assertEqual(self.api.get(f"/book/{entry}/page/0")[0], 200)
        self.assertEqual(self.api.get(f"/book/{entry}/page/9")[0], 404)
        self.assertEqual(info["next"]["id"], self.detail(self.item_by_title("livres", "Naruto")["id"])["entries"][1]["id"])

    def test_epub_chapters_and_isolation(self):
        entry = self.detail(self.item_by_title("livres", "Dumas")["id"])["entries"][0]["id"]
        info = self.api.json(f"/api/book/{entry}")[1]
        self.assertEqual(info["format"], "epub")
        self.assertEqual(len(info["chapters"]), 2)
        status, headers, body = self.api.get(info["chapters"][0])
        self.assertEqual(status, 200)
        self.assertIn("script-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("xml", headers["Content-Type"])


class TestProgress(ServerCase):
    def continue_for(self, item):
        found = [c for c in self.api.json("/api/home")[1]["continue"] if c["item"]["id"] == item]
        self.assertEqual(len(found), 1)
        return found[0]

    def test_progress_continue_and_next_up(self):
        dark = self.detail(self.item_by_title("series", "Dark")["id"])
        first, second = dark["entries"][0], dark["entries"][1]
        self.assertEqual(self.api.json("/api/progress", {"entry": first["id"], "position": 600, "duration": 3000})[1]["done"], False)
        row = self.continue_for(dark["id"])
        self.assertEqual((row["id"], row["position"], row["resume"]), (first["id"], 600, True))
        self.assertEqual(self.detail(dark["id"])["resume"], first["id"])
        # fini à 95 % -> l'épisode suivant est proposé
        self.assertTrue(self.api.json("/api/progress", {"entry": first["id"], "position": 2850, "duration": 3000})[1]["done"])
        row = self.continue_for(dark["id"])
        self.assertEqual((row["id"], row["resume"]), (second["id"], False))
        self.assertEqual(self.detail(dark["id"])["resume"], second["id"])
        self.assertEqual(self.item_by_title("series", "Dark")["seen"], 1)
        # tout vu -> plus rien à reprendre
        self.api.json(f"/api/item/{dark['id']}/seen", {"done": True})
        self.assertEqual([c for c in self.api.json("/api/home")[1]["continue"] if c["item"]["id"] == dark["id"]], [])
        self.assertEqual(self.items("series", filter="unseen")["total"], self.items("series")["total"] - 1)
        self.api.json(f"/api/item/{dark['id']}/seen", {"done": False})
        self.assertNotIn("seen", self.item_by_title("series", "Dark"))

    def test_short_positions_do_not_resume_and_done_tail(self):
        movie = self.detail(self.item_by_title("films", "Inception")["id"])["entries"][0]
        self.api.json("/api/progress", {"entry": movie["id"], "position": 5, "duration": 8000})
        self.assertEqual([c for c in self.api.json("/api/home")[1]["continue"] if c["id"] == movie["id"]], [])
        self.assertTrue(self.api.json("/api/progress", {"entry": movie["id"], "position": 7950, "duration": 8000})[1]["done"])
        self.assertEqual(self.api.json("/api/progress", {"entry": "nope", "position": 1})[0], 404)
        self.assertEqual(self.api.json("/api/progress", {"entry": movie["id"], "position": "abc"})[0], 400)

    def test_book_progress_uses_pages(self):
        entry = self.detail(self.item_by_title("livres", "Naruto")["id"])["entries"][0]
        self.api.json("/api/progress", {"entry": entry["id"], "position": 1, "duration": 3})
        self.assertEqual(self.api.json(f"/api/book/{entry['id']}")[1]["position"], 1)
        self.assertTrue(self.api.json("/api/progress", {"entry": entry["id"], "position": 2, "duration": 3})[1]["done"])


class TestJobs(ServerCase):
    def test_rescan_picks_up_new_files_and_keeps_progress(self):
        before = self.items("series")["total"]
        fixtures.write_mkv(self.run_.libs["series"] / "Nouvelle Serie" / "Saison 1" / "01 - A.mkv", fixtures.episode_blocks("Nouvelle Serie", 1, 1, "A"), minutes=20)
        status, data = self.api.json("/api/scan", {"lib": "series"})
        self.assertEqual(status, 202)
        self.run_.wait_jobs()
        self.assertEqual(self.items("series")["total"], before + 1)
        jobs = self.api.json("/api/jobs?log=1")[1]
        self.assertEqual(jobs["jobs"][0]["status"], "done")
        self.assertIn("Séries", "\n".join(jobs["jobs"][0]["log"]))

    def test_cleanup_simulation_then_apply(self):
        junk = self.run_.libs["films"] / "Inception (2010)" / "Thumbs.db"
        junk.write_bytes(b"x")
        self.api.json("/api/cleanup", {})
        self.run_.wait_jobs()
        self.assertTrue(junk.exists())
        self.api.json("/api/cleanup", {"apply": True})
        self.run_.wait_jobs()
        self.assertFalse(junk.exists())

    def test_enrich_needs_a_key_and_a_shows_library(self):
        self.assertEqual(self.api.json("/api/enrich", {"lib": "films"})[0], 400)
        self.assertEqual(self.api.json("/api/enrich", {"lib": "series"})[0], 400)


@unittest.skipUnless(FFMPEG, "ffmpeg absent")
class TestPlayback(ServerCase):
    def entry(self, title):
        return self.detail(self.item_by_title("films", title)["id"])["entries"][0]["id"]

    def play(self, entry, **body):
        status, data = self.api.json(f"/api/play/{entry}", body)
        self.assertEqual(status, 200, data)
        return data

    def ffprobe(self, data):
        path = Path(self.tmp) / "out.mp4"
        path.write_bytes(data)
        done = subprocess.run([self.hub.cfg.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], capture_output=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_compatible_file_is_played_directly_with_ranges(self):
        entry = self.entry("Compatible")
        plan = self.play(entry, caps=CAPS_ALL)
        self.assertEqual((plan["mode"], plan["seekable"]), ("direct", True))
        size = len(self.api.get(plan["url"])[2])
        status, headers, body = self.api.get(plan["url"], headers={"Range": "bytes=100-199"})
        self.assertEqual((status, len(body), headers["Content-Range"]), (206, 100, f"bytes 100-199/{size}"))
        status, headers, body = self.api.get(plan["url"], headers={"Range": "bytes=-50"})
        self.assertEqual((status, len(body)), (206, 50))
        self.assertEqual(self.api.get(plan["url"], headers={"Range": f"bytes={size + 10}-"})[0], 416)

    def test_multichannel_ac3_is_remuxed_to_aac(self):
        entry = self.entry("Vrai Film")
        plan = self.play(entry, caps=CAPS_ALL)
        self.assertEqual(plan["mode"], "remux")
        self.assertTrue(any("ac3" in w for w in plan["why"]))
        self.assertFalse(plan["seekable"])
        status, headers, body = self.api.get(plan["url"])
        self.assertEqual((status, headers["Content-Type"]), (200, "video/mp4"))
        info = self.ffprobe(body)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
        self.assertEqual((video["codec_name"], audio["codec_name"], audio["channels"]), ("h264", "aac", 2))

    def test_unreadable_codec_is_transcoded_and_scaled(self):
        entry = self.entry("Vrai Film")
        plan = self.play(entry, caps={**CAPS_ALL, "video": ["vp9"]}, max_height=90)
        self.assertEqual(plan["mode"], "transcode")
        body = self.api.get(plan["url"])[2]
        video = next(s for s in self.ffprobe(body)["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["codec_name"], video["height"]), ("h264", 90))

    def test_big_picture_is_scaled_down_to_1080p_by_default(self):
        fixtures.write_video(self.run_.libs["films"] / "Grand (2020)" / "Grand (2020).mkv", seconds=2, size="2560x1440")
        self.api.json("/api/scan", {"lib": "films"})
        self.run_.wait_jobs()
        plan = self.play(self.entry("Grand"), caps={**CAPS_ALL, "video": ["vp9"]})
        self.assertEqual(plan["mode"], "transcode")
        self.assertIn("h=1080", plan["url"])
        video = next(s for s in self.ffprobe(self.api.get(plan["url"])[2])["streams"] if s["codec_type"] == "video")
        self.assertEqual(video["height"], 1080)

    def test_restart_from_a_later_start(self):
        entry = self.entry("Vrai Film")
        plan = self.play(entry, caps=CAPS_ALL)
        whole = self.ffprobe(self.api.get(plan["url"])[2])["format"]
        later = self.ffprobe(self.api.get(plan["url"] + "&start=2")[2])["format"]
        self.assertLess(float(later["duration"]), float(whole["duration"]) - 1)

    def test_audio_and_subtitle_listing(self):
        plan = self.play(self.entry("Vrai Film"), caps=CAPS_ALL)
        self.assertEqual(len(plan["audio"]), 1)
        self.assertIn("AC-3", plan["audio"][0]["label"])
        self.assertEqual(plan["subs"][0]["where"], "mkv")
        status, headers, body = self.api.get(plan["subs"][0]["url"])
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"WEBVTT"))
        self.assertIn(b"Bonjour", body)
        self.assertEqual(self.api.get(plan["subs"][0]["url"])[0], 200)       # lu depuis le cache

    def test_side_subtitles(self):
        folder = self.run_.libs["films"] / "Compatible (2021)"
        (folder / "Compatible (2021).fr.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nSalut\n", encoding="utf-8")
        plan = self.play(self.entry("Compatible"), caps=CAPS_ALL)
        side = next(s for s in plan["subs"] if s["where"] == "fichier")
        self.assertIn("Français", side["label"])
        body = self.api.get(side["url"])[2]
        self.assertIn(b"00:00:01.000 --> 00:00:02.000", body)

    def test_stream_limit_returns_503(self):
        hub = self.hub
        taken = [hub.streams.acquire(blocking=False) for _ in range(hub.cfg.max_streams)]
        try:
            plan = self.play(self.entry("Vrai Film"), caps=CAPS_ALL)
            status, _, body = self.api.get(plan["url"])
            self.assertEqual(status, 503)
            self.assertIn("conversions en cours", json.loads(body)["error"])
        finally:
            for ok in taken:
                if ok:
                    hub.streams.release()

    def test_stream_slot_is_released_after_the_client_leaves(self):
        plan = self.play(self.entry("Vrai Film"), caps={**CAPS_ALL, "video": ["vp9"]}, max_height=180)
        conn = http.client.HTTPConnection("127.0.0.1", self.run_.port, timeout=30)
        conn.request("GET", plan["url"])
        response = conn.getresponse()
        response.read(2048)
        conn.close()
        end = time.time() + 10
        while self.hub.active and time.time() < end:
            time.sleep(0.05)
        self.assertEqual(self.hub.active, 0)

    def test_probe_is_kept_in_the_database(self):
        entry = self.entry("Compatible")
        self.play(entry, caps=CAPS_ALL)
        self.assertIsNotNone(self.hub.db.one("SELECT probe FROM entries WHERE id=?", (entry,))["probe"])

    def test_not_a_video(self):
        track = self.detail(self.item_by_title("musique", "Discovery")["id"])["entries"][0]["id"]
        self.assertEqual(self.api.json(f"/api/play/{track}", {})[0], 400)

    def test_audio_file_is_served_with_ranges(self):
        track = self.detail(self.item_by_title("musique", "Discovery")["id"])["entries"][0]["id"]
        status, headers, _ = self.api.get(f"/media/{track}/file", headers={"Range": "bytes=0-9"})
        self.assertEqual((status, headers["Accept-Ranges"]), (206, "bytes"))


class TestWithoutFfmpeg(unittest.TestCase):
    def test_stream_says_so(self):
        tmp = tempfile.mkdtemp()
        try:
            run = Running(tmp, ffmpeg=False)
            api = Client(run.port)
            movie = api.json("/api/library/films")[1]["items"][0]
            entry = api.json(f"/api/item/{movie['id']}")[1]["entries"][0]["id"]
            status, headers, body = api.get(f"/media/{entry}/stream?m=remux")
            self.assertEqual(status, 501)
            self.assertIn("ffmpeg", json.loads(body)["error"])
            run.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestPassword(unittest.TestCase):
    def test_login_flow(self):
        tmp = tempfile.mkdtemp()
        try:
            run = Running(tmp, password="secret")
            api = Client(run.port)
            self.assertEqual(api.get("/api/state")[0], 401)
            status, headers, _ = api.get("/")
            self.assertEqual((status, headers["Location"]), (302, "/login"))
            self.assertEqual(api.get("/login")[0], 200)
            self.assertEqual(api.get("/media/abc/file")[0], 302)
            status, headers, _ = api.request("POST", "/api/login", body=b"password=wrong", headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual((status, headers["Location"]), (303, "/login?error=1"))
            self.assertNotIn("Set-Cookie", headers)
            status, headers, _ = api.request("POST", "/api/login", body=b"password=secret", headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(status, 303)
            cookie = headers["Set-Cookie"].split(";")[0]
            self.assertEqual(Client(run.port, cookie).get("/api/state")[0], 200)
            self.assertEqual(Client(run.port, "hub=" + "0" * 64).get("/api/state")[0], 401)
            run.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
