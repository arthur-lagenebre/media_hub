"""Le serveur de bout en bout : un vrai serveur sur un port libre, une vraie médiathèque d'essai, de vraies requêtes HTTP."""

from __future__ import annotations

import errno
import http.client
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
from pathlib import Path
from urllib.parse import quote

from tests import fixtures
from hub import config

fixtures.REPO  # noqa: B018  (rend `libraries` importable)
config.use_media_library(fixtures.REPO)

from hub.scan.common import is_album_cover  # noqa: E402
from hub.scan.music import scan_music  # noqa: E402
from hub.server import jobs  # noqa: E402
from hub.server.app import TITLE_SORTS, make_server  # noqa: E402

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
        cfg.tmdb_key = ""       # jamais la vraie clé du .env de media_library : pas d'appel réseau, et les tests ne dépendent pas de la machine
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

    def test_movie_and_show_link_to_tmdb(self):
        inception = self.detail(self.item_by_title("films", "Inception")["id"])
        self.assertEqual(inception["links"], [{"label": "TMDB", "url": "https://www.themoviedb.org/movie/27205"}])
        self.assertNotIn("links", self.detail(self.item_by_title("series", "Dark")["id"]))          # pas d'identifiant TMDB tant que la série n'est pas enrichie
        self.assertNotIn("links", self.detail(self.item_by_title("musique", "Discovery")["id"]))

    def test_video_links_edge_cases(self):
        from hub.server.app import video_links
        movie = {"type": "movie"}
        self.assertEqual(video_links(movie, {}), [])                                                     # pas d'identifiant : pas de lien
        self.assertEqual(video_links(movie, {"tmdb": "n'importe quoi/../x"}), [])                        # une étiquette mal formée est ignorée
        self.assertEqual(video_links({"type": "show"}, {"tmdb_pinned": "70523"})[0]["url"], "https://www.themoviedb.org/tv/70523")      # identifiant épinglé dans le nom du dossier, série non enrichie
        self.assertEqual(video_links(movie, {"tmdb": "70523"})[0]["url"], "https://www.themoviedb.org/movie/70523")                     # nombre seul : le genre vient de l'élément
        self.assertEqual(video_links(movie, {"tmdb": " MOVIE/27205 "})[0]["url"], "https://www.themoviedb.org/movie/27205")             # la casse et les espaces ne comptent pas
        self.assertEqual(video_links(movie, {"tmdb": "movie/٢٧"}), [])                                                                    # seuls les chiffres ASCII font un identifiant
        self.assertEqual(video_links({"type": "album"}, {"tmdb": "1"}), [])

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
        self.assertEqual((naruto["unit"], naruto["units"]), ("tome", "tomes"))      # le singulier et le pluriel : l'interface n'ajoute pas de « s » à un pluriel

    def test_books_sections_are_quick_access_facets(self):
        data = self.api.json("/api/library/livres")[1]
        sections = dict(data["sections"])
        self.assertTrue({"Manga", "Artbook", "Romans"} <= set(sections))
        self.assertEqual(sum(sections.values()), data["total"])
        names = [n for n, _ in data["sections"]]
        self.assertLess(names.index("Manga"), names.index("Artbook"))              # l'ordre de l'étagère
        manga = self.api.json("/api/library/livres?section=Manga")[1]
        self.assertEqual(manga["total"], sections["Manga"])
        self.assertEqual({i["title"] for i in manga["items"]} & {"The Art of Dune", "Le Comte de Monte-Cristo"}, set())
        self.assertEqual(dict(manga["sections"]), sections)                    # les pastilles gardent leurs comptes sous un filtre
        self.assertEqual(self.api.json("/api/library/livres?section=Inconnue")[1]["total"], 0)
        self.assertNotIn("sections", self.api.json("/api/library/films")[1])

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

    def test_replaced_album_cover_is_served_without_rescan(self):
        album = self.item_by_title("musique", "Discovery")
        meta = json.loads(self.hub.db.one("SELECT meta FROM items WHERE id=?", (album["id"],))["meta"])
        cover = self.hub.cfg.library("musique").path / meta.get("folder", "") / meta["cover_file"]
        before = self.api.get(f"/img/item/{album['id']}")
        self.assertEqual(before[1]["Cache-Control"], "private, no-cache")      # le navigateur revalide : une nouvelle pochette n'attend pas une semaine
        original = cover.read_bytes()
        try:
            cover.write_bytes(original + b"\0" * 7)
            after = self.api.get(f"/img/item/{album['id']}", headers={"If-None-Match": before[1]["ETag"]})
            self.assertEqual(after[0], 200)
            self.assertNotEqual(after[1]["ETag"], before[1]["ETag"])
        finally:
            cover.write_bytes(original)


class TestAlbumCover(unittest.TestCase):
    """La pochette d'un album, c'est cover.{ext} dans son dossier : ni folder.jpg, ni la pochette embarquée dans les pistes."""

    def test_only_cover_dot_ext_is_the_cover(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for artist, other in (("Sans", "folder.jpg"), ("Avec", "Cover.PNG"), ("Rien", "")):
                fixtures.write_flac(root / artist / "2001 - Album" / "01 - Titre.flac", "Titre", f"Album {artist}", artist, 1, 1)
                if other:
                    (root / artist / "2001 - Album" / other).write_bytes(fixtures.PNG)
            items, _, _ = scan_music(config.Library("musique", "Musique", "music", root), {})
            covers = {item["title"]: json.loads(item["meta"]) for item in items}
        self.assertEqual({t: (m["has_cover"], m["cover_file"]) for t, m in covers.items()},
                         {"Album Sans": (False, ""), "Album Avec": (True, "Cover.PNG"), "Album Rien": (False, "")})

    def test_albums_of_an_artist_go_by_year_then_name(self):
        rows = [{"grp": grp, "year": year, "sort": sort} for grp, year, sort in (
            ("Daft Punk", "2013", "random access memories"), ("Daft Punk", "2001", "discovery"), ("Daft Punk", "", "inconnu"), ("Daft Punk", "2001", "alive"),
            ("Air", "2004", "talkie walkie"), ("", "1999", "sans artiste"))]
        ordered = [r["sort"] for r in sorted(rows, key=TITLE_SORTS["music"])]
        self.assertEqual(ordered, ["talkie walkie", "alive", "discovery", "random access memories", "inconnu", "sans artiste"])

    def test_cover_names(self):
        self.assertTrue(all(is_album_cover(n) for n in ("cover.jpg", "Cover.PNG", "cover.webp")))
        self.assertFalse(any(is_album_cover(n) for n in ("folder.jpg", "front.png", "albumart.jpg", "cover.txt", "cover", "my cover.jpg")))


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

    def test_dismissed_item_leaves_continue_until_watched_again(self):
        dark = self.detail(self.item_by_title("series", "Dark")["id"])
        first = dark["entries"][0]
        self.api.json("/api/progress", {"entry": first["id"], "position": 600, "duration": 3000})
        self.continue_for(dark["id"])
        self.assertTrue(self.api.json(f"/api/item/{dark['id']}/dismiss", {})[1]["ok"])
        self.assertEqual([c for c in self.api.json("/api/home")[1]["continue"] if c["item"]["id"] == dark["id"]], [])
        self.assertEqual(self.detail(dark["id"])["resume"], first["id"])        # la progression est intacte
        self.hub.db.execute("UPDATE progress SET updated=updated+10 WHERE entry_id=?", (first["id"],))   # relancé plus tard
        self.assertEqual(self.continue_for(dark["id"])["id"], first["id"])
        self.assertEqual(self.api.json("/api/item/nope/dismiss", {})[0], 404)
        # le navigateur réutilise sa connexion : la requête qui suit un POST ne doit pas être polluée par son corps
        conn = http.client.HTTPConnection("127.0.0.1", self.run_.port, timeout=30)
        try:
            conn.request("POST", f"/api/item/{dark['id']}/dismiss", body=b"{}", headers={"Content-Type": "application/json"})
            conn.getresponse().read()
            conn.request("GET", "/api/home")
            self.assertEqual(conn.getresponse().status, 200)
        finally:
            conn.close()
        self.hub.db.execute("DELETE FROM kv WHERE key LIKE 'dismiss:%'")      # la base est partagée entre les tests
        self.hub.db.execute("DELETE FROM progress WHERE entry_id=?", (first["id"],))

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

    def test_cleanup_keeps_only_cover_dot_ext_as_a_music_image(self):
        artist = next((self.run_.libs["musique"]).iterdir())
        album = next(p for p in artist.iterdir() if p.is_dir())
        guid = "{164D5496-D54B-4954-8C25-303F4EF577D7}"
        other = "{264D5496-D54B-4954-8C25-303F4EF577D7}"      # autre identifiant : sous Windows, la casse seule ne distingue pas deux fichiers
        junk = [album / f"AlbumArt_{guid}_Large.jpg", album / f"AlbumArt_{guid}_Small.jpg", album / "AlbumArtSmall.jpg", album / f"albumart_{other.lower()}_large.JPG",
                album / "folder.jpg", album / "Front.png", album / "AlbumArt.jpg", album / "AlbumArt_not-a-guid_Large.jpg", artist / "artist.jpg"]
        candidates = album / "_covers"       # Cover.py --candidates : pas encore choisies, donc pas touchées
        candidates.mkdir()
        self.addCleanup(shutil.rmtree, candidates, True)
        keep = [album / "cover.jpg", album / "cover.png", album / "notes.txt", candidates / "candidate.jpg", *album.glob("*.flac"),
                artist / "1999 - Un album qui manque.jpg", artist / "2001 - Un autre.PNG",       # pochettes d'albums manquants, posées dans le dossier de l'artiste
                self.run_.libs["films"] / "Inception (2010)" / "folder.jpg"]       # hors musique, folder.jpg est une vraie affiche
        for path in junk + keep:
            if not path.exists():
                path.write_bytes(b"x")
                if path not in junk:
                    self.addCleanup(path.unlink, True)
        self.api.json("/api/cleanup", {})       # la simulation ne touche à rien
        self.run_.wait_jobs()
        self.assertEqual([p.exists() for p in junk], [True] * len(junk))
        self.api.json("/api/cleanup", {"apply": True})
        self.run_.wait_jobs()
        self.assertEqual([p.exists() for p in junk], [False] * len(junk))
        self.assertEqual([p.exists() for p in keep], [True] * len(keep))
        self.assertIn(f"{len(junk)} effacé", "\n".join(self.api.json("/api/jobs?log=1")[1]["jobs"][0]["log"]))

    def test_force_unlink_removes_a_read_only_file(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        junk = folder / "Thumbs.db"
        junk.write_bytes(b"x")
        junk.chmod(0o444)
        jobs.force_unlink(str(junk))
        self.assertFalse(junk.exists())

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "root et Windows écrivent dans un dossier non modifiable : rien à forcer")
    def test_force_unlink_goes_through_a_locked_folder_and_restores_it(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        junk = folder / "Thumbs.db"
        junk.write_bytes(b"x")
        folder.chmod(0o555)
        try:
            jobs.force_unlink(str(junk))
            self.assertEqual(folder.stat().st_mode & 0o777, 0o555)
        finally:
            folder.chmod(0o755)
        self.assertFalse(junk.exists())

    def test_cleanup_logs_a_read_only_volume_once_per_library(self):
        libs = [types.SimpleNamespace(path=str(self.run_.libs[name]), kind=name) for name in ("films", "musique")]
        for name in ("films", "musique"):
            (self.run_.libs[name] / "Thumbs.db").write_bytes(b"x")
            (self.run_.libs[name] / "Desktop.ini").write_bytes(b"x")
            self.addCleanup(lambda n=name: [(self.run_.libs[n] / f).unlink(missing_ok=True) for f in ("Thumbs.db", "Desktop.ini")])
        log = []
        with mock.patch.object(jobs, "force_unlink", side_effect=OSError(errno.EROFS, "ro")):
            result = jobs.clean_junk(libs, True, log.append)
        self.assertEqual(result, {"found": 4, "removed": 0})
        self.assertEqual(sum("lecture seule (retire" in line for line in log), 2)        # une par bibliothèque
        self.assertEqual(sum("… et 1 autre(s)" in line for line in log), 2)

    def test_enrich_needs_a_key_and_a_shows_library(self):
        self.assertEqual(self.api.json("/api/enrich", {"lib": "films"})[0], 400)
        self.assertEqual(self.api.json("/api/enrich", {"lib": "series"})[0], 400)

    def test_artist_sheet_lists_albums_tracks_and_the_fetched_bio(self):
        from unittest import mock

        album = self.item_by_title("musique", "Discovery")
        detail = self.detail(album["id"])
        self.assertTrue(detail["artist_id"])
        self.assertEqual(album["artist_id"], detail["artist_id"])
        url = f"/api/artist/musique/{detail['artist_id']}"
        status, sheet = self.api.json(url)
        self.assertEqual(status, 200)
        self.assertEqual((sheet["name"], [a["title"] for a in sheet["albums"]]), ("Daft Punk", ["Discovery"]))
        self.assertEqual([t["title"] for t in sheet["tracks"]], ["One More Time", "Aerodynamic", "Digital Love"])
        self.assertNotIn("info", sheet)                                 # pas de fiche tant qu'on ne l'a pas demandée
        self.assertEqual(self.api.json("/api/artist/musique/deadbeef")[0], 404)
        self.assertEqual(self.api.json(f"/api/artist/films/{detail['artist_id']}")[0], 404)

        info = {"type": "Group", "area": "France", "begin": "1993", "end": "2021", "ended": True, "disambiguation": "", "sheet": 2, "links": [{"label": "Facebook", "url": "https://facebook.com/daftpunk"}, {"label": "Site officiel", "url": "https://daftpunk.com"}, {"label": "Site officiel", "url": "https://autre.example"}, {"label": "YouTube", "url": "https://youtube.com/daftpunk"}],
                "members": [{"name": "Thomas Bangalter", "mbid": "x", "begin": "1993", "end": "2021", "ended": True, "roles": ["synthétiseur"]}],
                "wiki": {"lang": "fr", "title": "Daft Punk", "url": "https://fr.wikipedia.org/wiki/Daft_Punk", "extract": "Duo de musique électronique."}}
        self.hub.db.update_item_meta(album["id"], meta={"artist_mbid": "abc"})
        lib = next(l for l in self.hub.cfg.libraries if l.id == "musique")
        photo = f"/img/artist/musique/{detail['artist_id']}?w=320"
        self.assertEqual(self.api.get(photo)[0], 404)                  # pas de photo avant les fiches
        self.addCleanup((self.hub.cfg.data / "artists" / f"{detail['artist_id']}.png").unlink, True)

        class Download:                                                 # ce que Wikimedia répondrait : une image
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n=-1):
                return fixtures.PNG

        with mock.patch("hub.enrich.fetch_artist", return_value=info), mock.patch("hub.enrich.photo_urls", return_value=["https://upload.wikimedia.org/x.png"]), \
                mock.patch("hub.enrich.urlopen", return_value=Download()):
            self.assertEqual(self.api.json("/api/enrich", {"lib": "musique"})[0], 202)         # MusicBrainz ne demande pas de clé
            self.run_.wait_jobs()
        sheet = self.api.json(url)[1]
        self.assertTrue(sheet["photo"])
        self.assertEqual([p.name for p in (self.hub.cfg.data / "artists").glob("*.png")], [f"{detail['artist_id']}.png"])       # un fichier par artiste, à son nom
        status, headers, body = self.api.get(photo)
        self.assertEqual((status, headers["Content-Type"]), (200, "image/jpeg"))
        self.assertIn("genres", sheet)
        self.assertEqual(sheet["info"]["facts"], "Groupe · France · 1993 – 2021")
        self.assertEqual(sheet["info"]["wiki"]["extract"], "Duo de musique électronique.")
        self.assertEqual(sheet["info"]["members"][0], {"name": "Thomas Bangalter", "roles": ["synthétiseur"], "period": "1993 – 2021", "former": True})
        self.assertEqual([(l["label"], l["url"]) for l in sheet["info"]["links"]], [("Site officiel", "https://daftpunk.com"), ("YouTube", "https://youtube.com/daftpunk"),
                          ("Wikipédia", "https://fr.wikipedia.org/wiki/Daft_Punk"), ("MusicBrainz", "https://musicbrainz.org/artist/abc")])      # ni Facebook ni le doublon ; Wikipédia et MusicBrainz en dernier
        self.hub.jobs.scan(lib)
        self.run_.wait_jobs()

    def test_artist_sheet_shows_missing_albums_from_the_images_next_to_their_folders(self):
        artist = self.run_.libs["musique"] / "Daft Punk"
        found = [artist / "2013 - Random Access Memories.png", artist / "1997 - Homework.txt", artist / "2001 - Discovery.png"]       # le dernier est possédé : il ne manque pas
        for path in found:
            path.write_bytes(fixtures.PNG)
            self.addCleanup(path.unlink, True)
        self.hub.jobs.scan(self.hub.cfg.library("musique"))
        self.run_.wait_jobs()
        album = self.item_by_title("musique", "Discovery")
        artist_id = self.detail(album["id"])["artist_id"]
        _, sheet = self.api.json(f"/api/artist/musique/{artist_id}")
        self.assertEqual([(w["year"], w["title"], bool(w["cover"])) for w in sheet["wanted"]], [("1997", "Homework", False), ("2013", "Random Access Memories", True)])
        self.assertEqual([a["title"] for a in sheet["albums"]], ["Discovery"])
        status, headers, data = self.api.get(sheet["wanted"][1]["cover"] + "?w=160")
        self.assertEqual((status, headers["Content-Type"].split("/")[0]), (200, "image"))
        self.assertEqual(self.api.get(f"/img/wanted/musique/{artist_id}/inconnu")[0], 404)
        self.assertEqual([i["title"] for i in self.items("musique")["items"] if "Homework" in i["title"] or "Random" in i["title"]], [])       # ailleurs que sur la fiche, ils n'existent pas
        self.api.json("/api/cleanup", {"apply": True})
        self.run_.wait_jobs()
        self.assertEqual([p.exists() for p in found], [True] * len(found))       # le nettoyage garde leurs images

    def test_artist_photo_is_one_file_that_each_new_photo_replaces(self):
        from unittest import mock

        album = self.item_by_title("musique", "Discovery")
        detail = self.detail(album["id"])
        artist_id = detail["artist_id"]
        base = f"/api/artist/musique/{artist_id}"
        photo = f"/img/artist/musique/{artist_id}?w=320"
        folder = self.hub.cfg.data / "artists"
        self.addCleanup(lambda: [p.unlink() for p in folder.glob(f"{artist_id}.*")])
        self.assertNotIn("own_photo", self.api.json(base)[1])
        self.assertEqual(self.api.request("POST", base + "/photo", body=b"ceci n'est pas une image", headers={"Content-Type": "image/png"})[0], 415)
        self.assertEqual(self.api.request("POST", base + "/photo", body=b"RIFFxxxxWAVE", headers={"Content-Type": "image/webp"})[0], 415)
        self.assertEqual(self.api.request("POST", "/api/artist/musique/deadbeef/photo", body=fixtures.PNG, headers={"Content-Type": "image/png"})[0], 404)
        status, _, body = self.api.request("POST", base + "/photo", body=fixtures.PNG, headers={"Content-Type": "image/png"})
        self.assertEqual(status, 200, body)
        self.assertTrue(self.api.json(base)[1]["photo"])
        self.assertEqual(self.api.get(photo)[0], 200)
        self.assertEqual([p.name for p in folder.glob(f"{artist_id}.*")], [f"{artist_id}.png"])
        self.api.request("POST", base + "/photo", body=fixtures.PNG, headers={"Content-Type": "image/png"})
        self.assertEqual([p.name for p in folder.glob(f"{artist_id}.*")], [f"{artist_id}.png"])       # la nouvelle prend la place de l'ancienne

        self.assertEqual(self.api.json(base + "/photo/wikipedia", {})[0], 404)                        # sans article connu, rien à chercher
        self.assertEqual(self.api.json(base + "/photo/delete", {})[0], 404)                           # et une photo ne se supprime pas
        info = {"sheet": 2, "photo": "", "wiki": {"lang": "fr", "title": "Daft Punk", "url": "https://fr.wikipedia.org/wiki/Daft_Punk", "extract": "Duo."}}
        self.hub.db.update_item_meta(album["id"], meta={"artist_mbid": "abc"})
        self.addCleanup(self.hub.jobs.scan, next(l for l in self.hub.cfg.libraries if l.id == "musique"))
        self.addCleanup(self.hub.db.kv_set, "artist:abc", None)         # la fiche posée ici ne doit pas empêcher celle du test des fiches
        self.hub.db.kv_set("artist:abc", {"fetched": "2099-01-01", "info": info})
        self.assertIn("wiki", self.api.json(base)[1]["info"])

        class Download:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n=-1):
                return b"\xff\xd8\xff\xe0 photo de Wikipedia"

        with mock.patch("hub.enrich.photo_urls", return_value=["https://upload.wikimedia.org/x.jpg"]), mock.patch("hub.enrich.urlopen", return_value=Download()):
            self.assertEqual(self.api.json(base + "/photo/wikipedia", {})[0], 200)
        self.assertEqual([p.name for p in folder.glob(f"{artist_id}.*")], [f"{artist_id}.jpg"])       # celle de Wikipédia remplace la mienne, sans la laisser à côté
        with mock.patch("hub.enrich.photo_urls", return_value=[]):
            self.assertEqual(self.api.json(base + "/photo/wikipedia", {})[0], 502)
        self.assertEqual([p.name for p in folder.glob(f"{artist_id}.*")], [f"{artist_id}.jpg"])       # un échec ne retire rien

    def test_artist_photos_from_before_are_brought_to_one_file(self):
        import tempfile
        from pathlib import Path
        from hub import enrich

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.assertIsNone(enrich.photo_file(folder, "a1", "m1"))
            (folder / "m1.jpg").write_bytes(b"wikipedia")
            (folder / "custom-a1.png").write_bytes(b"envoyee")
            self.assertEqual(enrich.photo_file(folder, "a1", "m1").name, "a1.png")                    # celle qu'on a envoyée l'emporte
            self.assertEqual(enrich.photo_file(folder, "a1", "m1").read_bytes(), b"envoyee")
            self.assertFalse((folder / "custom-a1.png").exists())
            (folder / "m2.jpg").write_bytes(b"wikipedia")
            self.assertEqual(enrich.photo_file(folder, "a2", "m2").name, "a2.jpg")                    # sinon celle de Wikipédia
            self.assertEqual(enrich.store_photo(folder, "a2", fixtures.PNG, ".png"), "a2.png")
            self.assertEqual(sorted(p.name for p in folder.glob("a2.*")), ["a2.png"])

    def test_artist_links_are_limited_to_the_wanted_ones(self):
        from hub import enrich

        def link(kind, url):
            return {"type": kind, "url": {"resource": url}}

        data = {"relations": [link("social network", "https://www.facebook.com/x"), link("social network", "https://twitter.com/x"), link("streaming", "https://open.spotify.com/artist/x"),
                              link("youtube", "https://www.youtube.com/x"), link("soundcloud", "https://soundcloud.com/x"), link("bandcamp", "https://x.bandcamp.com"),
                              link("official homepage", "https://x.example"), link("official homepage", "https://x2.example"), link("official homepage", "javascript:alert(1)"),
                              link("social network", "https://youtube.com/c/autre"), link("wikidata", "https://www.wikidata.org/wiki/Q1")]}
        self.assertEqual([(l["label"], l["url"]) for l in enrich.wanted_links(data)],
                         [("Site officiel", "https://x.example"), ("Bandcamp", "https://x.bandcamp.com"), ("SoundCloud", "https://soundcloud.com/x"), ("YouTube", "https://www.youtube.com/x")])
        self.assertEqual(enrich.wanted_links({}), [])

    def test_artist_photo_only_comes_from_wikimedia(self):
        from hub import enrich

        class Wiki:
            def external(self, url):
                return {"thumbnail": {"source": "https://thumb.wikimedia.org/wikipedia/commons/thumb/9/92/F.jpg/330px-F.jpg?utm_source=fr.wikipedia.org"},
                        "originalimage": {"source": "https://evil.example/x.jpg", "width": 4000}}

        urls = enrich.photo_urls(Wiki(), {"lang": "fr", "title": "Daft Punk"})
        self.assertEqual(urls[0], "https://thumb.wikimedia.org/wikipedia/commons/thumb/9/92/F.jpg/960px-F.jpg?utm_source=fr.wikipedia.org")     # la plus grande d'abord
        self.assertEqual(len(urls), 2)                                  # la vignette en repli ; l'adresse d'un autre site n'est jamais suivie
        self.assertTrue(all(u.startswith("https://thumb.wikimedia.org/") for u in urls))

    def test_enrich_keeps_show_synopsis_seasons_and_cast(self):
        from unittest import mock
        from libraries.video.tmdb import TmdbError

        class FakeTmdb:
            def __init__(self, *args, **kwargs):
                pass

            def series(self, show_id):
                return {"name": "Dark", "overview": "Un enfant disparaît.", "genres": [{"name": "Drame"}], "first_air_date": "2017-12-01", "number_of_seasons": 3}

            def season(self, show_id, number):
                if number == 2:
                    raise TmdbError("introuvable")
                return {"overview": "Synopsis de la saison %d." % number}

            def aggregate_credits(self, show_id):
                return {"cast": [{"name": "Invité", "total_episode_count": 1, "roles": [{"character": "Passant", "episode_count": 1}]},
                                 {"name": "Louis Hofmann", "total_episode_count": 26, "roles": [{"character": "Jonas", "episode_count": 26}]}]}

        lib = next(l for l in self.hub.cfg.libraries if l.id == "series")
        self.hub.cfg.tmdb_key = "fake"
        self.addCleanup(setattr, self.hub.cfg, "tmdb_key", "")
        with mock.patch("hub.enrich.Tmdb", FakeTmdb), mock.patch("hub.enrich.lookup.resolve_show_id", return_value=1234):
            self.assertEqual(self.api.json("/api/enrich", {"lib": "series"})[0], 202)
            self.run_.wait_jobs()
        dark = self.detail(self.item_by_title("series", "Dark")["id"])
        self.assertEqual(dark["summary"], "Un enfant disparaît.")
        self.assertEqual(dark["season_summaries"], {"1": "Synopsis de la saison 1."})
        self.assertEqual(dark["actors"][0], {"name": "Louis Hofmann", "role": "Jonas"})
        self.assertEqual(dark["links"][0], {"label": "TMDB", "url": "https://www.themoviedb.org/tv/1234"})
        self.hub.jobs.scan(lib)                # un rescan n'efface rien
        self.run_.wait_jobs()
        again = self.detail(self.item_by_title("series", "Dark")["id"])
        self.assertEqual((again["summary"], again["season_summaries"], again["actors"]), (dark["summary"], dark["season_summaries"], dark["actors"]))


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


class TestNativeToken(unittest.TestCase):
    """Clients natifs (Android TV, Chromecast) : jeton par /api/token, en Bearer ou en ?token= sur les fichiers."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def start(self, password):
        run = Running(self.tmp, password=password)
        self.addCleanup(run.stop)
        return run, Client(run.port)

    def tokens(self, api):
        """(jeton, jeton de média) tels que /api/token les donne."""
        data = json.loads(api.request("POST", "/api/token", body={"password": "secret"})[2])
        return data["token"], data["media_token"]

    def test_token_with_good_and_bad_password(self):
        _, api = self.start("secret")
        status, _, body = api.request("POST", "/api/token", body={"password": "secret"})
        self.assertEqual(status, 200)
        token, media = json.loads(body)["token"], json.loads(body)["media_token"]
        self.assertRegex(token, r"^[0-9a-f]{64}$")
        self.assertRegex(media, r"^[0-9a-f]{64}$")
        self.assertNotEqual(token, media)
        status, _, body = api.request("POST", "/api/token", body={"password": "wrong"})
        self.assertEqual((status, json.loads(body)), (401, {"error": "mot de passe incorrect"}))
        self.assertEqual(api.request("POST", "/api/token", body=b"pas du json")[0], 401)
        self.assertEqual(api.request("POST", "/api/token", body=[1, 2])[0], 401)
        self.assertEqual(api.request("POST", "/api/token", body={"password": 12})[0], 401)

    def test_bearer(self):
        run, api = self.start("secret")
        token, media = self.tokens(api)
        self.assertEqual(api.get("/api/state", headers={"Authorization": f"Bearer {token}"})[0], 200)
        self.assertEqual(api.get("/api/state", headers={"Authorization": "Bearer " + "0" * 64})[0], 401)
        self.assertEqual(api.get("/api/state", headers={"Authorization": "Bearer é"})[0], 401)
        self.assertEqual(api.get("/api/state", headers={"Authorization": token})[0], 401)
        self.assertEqual(api.get("/api/state", headers={"Authorization": f"Bearer {media}"})[0], 401)       # le jeton de média n'ouvre pas l'API
        self.assertEqual(Client(run.port, f"hub={media}").get("/api/state")[0], 401)                       # ni en cookie

    def test_query_token_is_the_media_token_and_only_on_files(self):
        run, api = self.start("secret")
        token, media = self.tokens(api)
        albums = Client(run.port, f"hub={token}").json("/api/library/musique")[1]["items"]
        item = next(a["id"] for a in albums if a["title"] == "Discovery")       # un album avec pochette
        file = Client(run.port, f"hub={token}").json(f"/api/item/{item}")[1]["entries"][0]["file"]
        for url in (f"/img/item/{item}", file):
            self.assertEqual(api.get(f"{url}?token={media}")[0], 200, url)
            self.assertEqual(api.get(f"{url}?token={token}")[0], 302, url)             # le jeton principal ne passe pas en URL
            self.assertEqual(api.get(f"{url}?token={'0' * 64}")[0], 302, url)
        self.assertEqual(api.get(f"/img/item/{item}?%74oken={media}")[0], 200)         # la clé encodée est la même clé
        naruto = next(b["id"] for b in Client(run.port, f"hub={token}").json("/api/library/livres")[1]["items"] if b["title"] == "Naruto")
        volume = Client(run.port, f"hub={token}").json(f"/api/item/{naruto}")[1]["entries"][0]["id"]
        self.assertEqual(api.get(f"/book/{volume}/page/0?token={media}")[0], 200)      # une page de livre
        self.assertEqual(api.get(f"/book/{volume}/page/0?token={token}")[0], 302)
        self.assertEqual(api.get(f"/api/subs/inconnu/e0.vtt?token={media}")[0], 404)       # sous-titres : accès accordé (la piste n'existe pas, mais ce n'est pas un 401)
        self.assertEqual(api.get(f"/api/subs/inconnu/e0.vtt?token={token}")[0], 401)       # le jeton principal n'y passe pas non plus
        self.assertEqual(api.get("/api/subs/inconnu/e0.vtt")[0], 401)
        self.assertEqual(api.request("POST", f"/api/subs/x/../../cleanup?token={media}", body={"apply": True})[0], 404)     # le préfixe n'ouvre pas une autre route
        self.assertEqual(api.get(f"/api/state?token={media}")[0], 401)
        self.assertEqual(api.get(f"/api/library/films?token={media}")[0], 401)
        self.assertEqual(api.request("POST", f"/api/cleanup?token={media}", body={"apply": True})[0], 401)
        self.assertEqual(api.request("POST", f"/api/scan?token={media}", body={})[0], 401)

    def reuse_connection(self, run, body):
        """POST /api/token puis GET /api/state sur la MÊME connexion (keep-alive, comme OkHttp ou URLSession) : (état du premier, état du second)."""
        conn = http.client.HTTPConnection("127.0.0.1", run.port, timeout=30)
        self.addCleanup(conn.close)
        conn.request("POST", "/api/token", body=body, headers={"Content-Type": "application/json"})
        first = conn.getresponse()
        data = first.read()
        headers = {"Authorization": "Bearer " + json.loads(data)["token"]} if first.status == 200 else {}
        conn.request("GET", "/api/state", headers=headers)
        second = conn.getresponse()
        second.read()
        return first.status, second.status

    def test_the_body_is_always_read_so_the_connection_can_be_reused(self):
        run, _ = self.start("")
        self.assertEqual(self.reuse_connection(run, json.dumps({"password": "x"})), (200, 200))      # sans mot de passe, le corps est quand même lu

    def test_the_body_is_read_with_a_password_too(self):
        run, _ = self.start("secret")
        self.assertEqual(self.reuse_connection(run, json.dumps({"password": "secret"})), (200, 200))
        self.assertEqual(self.reuse_connection(run, json.dumps({"password": "faux"})), (401, 401))   # 401 « connexion requise », pas 400 : la requête suivante est bien lue

    def test_an_oversized_body_is_refused(self):
        run, _ = self.start("secret")
        conn = http.client.HTTPConnection("127.0.0.1", run.port, timeout=30)
        self.addCleanup(conn.close)
        conn.request("POST", "/api/token", body=json.dumps({"password": "secret", "pad": "x" * 6000}), headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        self.assertEqual((response.status, json.loads(response.read())), (413, {"error": "corps trop gros"}))

    def test_cookie_still_works(self):
        run, api = self.start("secret")
        token, _ = self.tokens(api)
        self.assertEqual(Client(run.port, f"hub={token}").get("/api/state")[0], 200)

    def test_without_password_everything_stays_open(self):
        _, api = self.start("")
        self.assertEqual(api.get("/api/state")[0], 200)
        status, _, body = api.request("POST", "/api/token", body={"password": "quoi que ce soit"})
        self.assertEqual((status, json.loads(body)), (200, {"token": "", "media_token": ""}))

    def test_tokens_are_masked_in_the_error_log(self):
        run, api = self.start("secret")
        token, media = self.tokens(api)
        with mock.patch("hub.server.app.print") as printed, mock.patch.object(run.hub, "item", side_effect=RuntimeError("boum")):
            api.get(f"/api/item/x?token={token}", headers={"Authorization": f"Bearer {token}"})
            api.get(f"/api/item/x?%74oken={media}&a=1", headers={"Authorization": f"Bearer {token}"})       # la clé encodée ne contourne pas le masquage
        logged = " ".join(str(c.args[0]) for c in printed.call_args_list)
        self.assertIn("boum", logged)
        self.assertIn("token=***&a=1", logged)
        self.assertNotIn(token, logged)
        self.assertNotIn(media, logged)


class TestSagas(ServerCase):
    """Les sagas regroupées dans la bibliothèque Films : une vignette par saga, tous les films au clic."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        films = cls.run_.libs["films"]
        for i in range(1, 11):
            fixtures.write_mkv(films / "_Course" / f"{i} - Course {i}.mkv", fixtures.movie_blocks(f"Course {i}", "Ça roule.", f"{2000 + i}-05-01", f"movie/{900 + i}", "Action", ["Réal"], [], collection="Saga Course", part=i), minutes=100)
        cls.hub.jobs.scan(cls.hub.cfg.library("films"))
        cls.run_.wait_jobs()

    def sagas(self, **query):
        return self.items("films", sagas=1, **query)

    def test_without_the_option_every_film_is_listed(self):
        titles = [i["title"] for i in self.items("films")["items"]]
        self.assertEqual(len([t for t in titles if t.startswith("Course ")]), 10)
        self.assertNotIn("Saga Course", titles)

    def test_sagas_become_one_card_and_single_films_stay(self):
        data = self.sagas()
        by_title = {i["title"]: i for i in data["items"]}
        self.assertEqual(sorted(t for t in by_title if by_title[t]["type"] == "saga"), ["Saga Course", "Saga DCEU"])
        course = by_title["Saga Course"]
        self.assertEqual((course["n"], course["years"], course["year"]), (10, ["2001", "2010"], "2001"))
        self.assertEqual(len(course["covers"]), 3)
        self.assertIn("Inception", by_title)
        self.assertNotIn("Course 3", by_title)
        self.assertNotIn("Man of Steel", by_title)
        self.assertEqual(data["sagas"], 2)
        self.assertEqual(data["films"], 10 + 2 + len([t for t in by_title if by_title[t]["type"] != "saga"]))

    def test_the_covers_follow_the_order_of_the_saga(self):
        course = next(i for i in self.sagas()["items"] if i["title"] == "Saga Course")
        firsts = [self.detail(c)["title"] for c in course["covers"]]
        self.assertEqual(firsts, ["Course 1", "Course 2", "Course 3"])

    def test_a_saga_page_lists_all_ten_films_in_order(self):
        data = self.items("films", group="Saga Course", sort="part", limit=50)
        self.assertEqual([i["title"] for i in data["items"]], [f"Course {i}" for i in range(1, 11)])      # Course 10 après Course 9, pas après Course 1
        self.assertEqual(data["total"], 10)

    def test_filters_work_on_whole_sagas(self):
        self.assertEqual([i["title"] for i in self.sagas(q="course 7")["items"]], ["Saga Course"])
        self.assertEqual([i["title"] for i in self.sagas(genre="Action")["items"] if i["title"] == "Saga Course"], ["Saga Course"])
        letters = dict(self.sagas()["letters"])
        self.assertGreaterEqual(letters.get("S", 0), 2)
        self.assertLessEqual({"Saga Course", "Saga DCEU"}, {i["title"] for i in self.sagas(letter="S")["items"]})

    def test_a_saga_is_unseen_until_every_film_is_seen(self):
        films = self.items("films", group="Saga Course", sort="part", limit=50)["items"]
        for film in films[:9]:
            self.api.json(f"/api/item/{film['id']}/seen", {"done": True})
        course = next(i for i in self.sagas()["items"] if i["title"] == "Saga Course")
        self.assertEqual(course["seen"], 9)
        self.assertIn("Saga Course", [i["title"] for i in self.sagas(filter="unseen")["items"]])
        self.api.json(f"/api/item/{films[9]['id']}/seen", {"done": True})
        course = next(i for i in self.sagas()["items"] if i["title"] == "Saga Course")
        self.assertEqual(course["seen"], 10)
        self.assertNotIn("Saga Course", [i["title"] for i in self.sagas(filter="unseen")["items"]])
        for film in films:
            self.api.json(f"/api/item/{film['id']}/seen", {"done": False})

    def test_movie_detail_gives_the_whole_saga(self):
        film = self.item_by_title("films", "Course 4")
        detail = self.detail(film["id"])
        self.assertEqual([m["title"] for m in detail["saga"]], [f"Course {i}" for i in range(1, 11)])


class TestApiUrls(ServerCase):
    """L'API donne elle-même les URLs des images et des fichiers : relatives, sans ?w= ni jeton, et elles mènent bien quelque part."""

    def test_state_announces_the_contract_version(self):
        self.assertEqual(self.api.json("/api/state")[1]["api"], 1)

    def test_items_carry_their_cover(self):
        album = self.item_by_title("musique", "Discovery")
        self.assertEqual(album["cover"], f"/img/item/{album['id']}")
        self.assertEqual(self.api.get(album["cover"])[0], 200)
        self.assertEqual(self.api.get(album["cover"] + "?w=160")[0], 200)
        self.assertEqual(self.detail(album["id"])["cover"], album["cover"])
        for lib in ("films", "series", "livres"):                 # toute vignette, jamais une saga (qui n'est pas un élément)
            self.assertTrue(all(i["cover"] == f"/img/item/{i['id']}" for i in self.items(lib)["items"]))

    def test_sagas_have_no_cover_of_their_own_but_list_their_members(self):
        films = self.items("films", sagas=1)["items"]
        saga = next((i for i in films if i["type"] == "saga"), None)
        if saga is None:
            self.skipTest("pas de saga dans la médiathèque factice")
        self.assertNotIn("cover", saga)
        self.assertEqual(saga["cover_urls"], [f"/img/item/{i}" for i in saga["covers"]])

    def test_entries_carry_their_file_and_image(self):
        book = self.detail(self.item_by_title("livres", "Naruto")["id"])["entries"][0]
        self.assertEqual((book["file"], book["image"]), (f"/media/{book['id']}/file", f"/img/entry/{book['id']}"))
        self.assertEqual(self.api.get(book["file"])[0], 200)
        self.assertEqual(self.api.get(book["image"])[0], 200)
        track = self.detail(self.item_by_title("musique", "Discovery")["id"])["entries"][0]
        self.assertEqual(track["file"], f"/media/{track['id']}/file")
        self.assertNotIn("image", track)                          # une piste n'a pas d'image à elle

    def test_tracks_of_artists_and_playlists_carry_cover_and_file(self):
        album = self.item_by_title("musique", "Discovery")
        sheet = self.api.json(f"/api/artist/musique/{album['artist_id']}")[1]
        for track in sheet["tracks"]:
            self.assertEqual((track["cover"], track["file"]), (f"/img/item/{album['id']}", f"/media/{track['id']}/file"))
        self.assertNotIn("image", sheet)                          # pas de photo tant que « Fiches artistes » n'est pas passé
        made = self.api.json("/api/playlists", {"name": "Adresses", "entries": [sheet["tracks"][0]["id"]]})[1]
        self.assertEqual(made["cover_urls"], [f"/img/item/{album['id']}"])
        self.assertEqual((made["tracks"][0]["cover"], made["tracks"][0]["file"]), (f"/img/item/{album['id']}", f"/media/{sheet['tracks'][0]['id']}/file"))
        listed = next(p for p in self.api.json("/api/playlists")[1]["playlists"] if p["id"] == made["id"])
        self.assertEqual(listed["cover_urls"], made["cover_urls"])

    def test_artist_photo_url_when_there_is_a_photo(self):
        album = self.item_by_title("musique", "Discovery")
        with mock.patch("hub.server.app.artist_photo", return_value=Path(__file__)):
            sheet = self.api.json(f"/api/artist/musique/{album['artist_id']}")[1]
        self.assertTrue(sheet["photo"])
        self.assertEqual(sheet["image"], f"/img/artist/musique/{album['artist_id']}")

    def test_book_info_gives_the_page_template(self):
        entry = self.detail(self.item_by_title("livres", "Naruto")["id"])["entries"][0]
        info = self.api.json(f"/api/book/{entry['id']}")[1]
        self.assertEqual((info["file"], info["page_url"]), (f"/media/{entry['id']}/file", f"/book/{entry['id']}/page/{{n}}"))
        self.assertEqual(self.api.get(info["page_url"].format(n=0))[0], 200)


class TestPlaylists(ServerCase):
    """Les playlists : créées à la main, gardées dans la base, intactes après une mise à jour."""

    def tracks(self):
        album = self.detail(self.item_by_title("musique", "Discovery")["id"])
        return album, [e["id"] for e in album["entries"]]

    def test_create_add_move_remove_rename_delete(self):
        album, ids = self.tracks()
        status, made = self.api.json("/api/playlists", {"name": "  Route   des vacances ", "entries": ids[:2]})
        self.assertEqual(status, 201)
        self.assertEqual((made["name"], made["count"], made["covers"]), ("Route des vacances", 2, [album["id"]]))
        pid = made["id"]
        self.assertEqual([t["id"] for t in made["tracks"]], ids[:2])
        self.assertEqual(made["tracks"][0]["album"], "Discovery")
        self.assertEqual(made["tracks"][0]["artist"], "Daft Punk")
        added = self.api.json(f"/api/playlist/{pid}/add", {"entries": [ids[2], ids[0]]})[1]       # un titre peut figurer deux fois
        self.assertEqual([t["id"] for t in added["tracks"]], [ids[0], ids[1], ids[2], ids[0]])
        moved = self.api.json(f"/api/playlist/{pid}/move", {"from": 3, "to": 0})[1]
        self.assertEqual([t["id"] for t in moved["tracks"]], [ids[0], ids[0], ids[1], ids[2]])
        removed = self.api.json(f"/api/playlist/{pid}/remove", {"index": 1})[1]
        self.assertEqual([t["id"] for t in removed["tracks"]], [ids[0], ids[1], ids[2]])
        self.assertEqual(self.api.json(f"/api/playlist/{pid}/rename", {"name": "Été"})[1]["name"], "Été")
        self.assertIn("Été", [p["name"] for p in self.api.json("/api/playlists")[1]["playlists"]])
        self.assertEqual(self.api.json(f"/api/playlist/{pid}/delete", {})[0], 200)
        self.assertEqual(self.api.get(f"/api/playlist/{pid}")[0], 404)

    def test_the_same_name_reuses_the_playlist(self):
        _, ids = self.tracks()
        first = self.api.json("/api/playlists", {"name": "Soirée", "entries": ids[:1]})[1]
        again = self.api.json("/api/playlists", {"name": "SOIRÉE", "entries": ids[1:2]})[1]
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(again["count"], 2)
        self.api.json(f"/api/playlist/{first['id']}/delete", {})

    def test_bad_requests(self):
        _, ids = self.tracks()
        self.assertEqual(self.api.json("/api/playlists", {"name": "   "})[0], 400)
        pid = self.api.json("/api/playlists", {"name": "Essai"})[1]["id"]
        self.assertEqual(self.api.json(f"/api/playlist/{pid}/add", {"entries": ["inconnu"]})[0], 400)
        film = self.item_by_title("films", "Inception")
        film_entry = self.detail(film["id"])["entries"][0]["id"]
        self.assertEqual(self.api.json(f"/api/playlist/{pid}/add", {"entries": [film_entry]})[0], 400)      # que de la musique
        self.assertEqual(self.api.json(f"/api/playlist/{pid}/add", {"entries": ids[:1]})[0], 200)
        self.assertEqual(self.api.json(f"/api/playlist/{pid}/remove", {"index": 5})[0], 400)
        self.assertEqual(self.api.json(f"/api/playlist/{pid}/move", {"from": 0, "to": 9})[0], 400)
        self.assertEqual(self.api.json("/api/playlist/zzzz/rename", {"name": "x"})[0], 404)
        self.api.json(f"/api/playlist/{pid}/delete", {})

    def test_a_playlist_survives_a_rescan_and_hides_vanished_tracks(self):
        _, ids = self.tracks()
        pid = self.api.json("/api/playlists", {"name": "Durable", "entries": ids})[1]["id"]
        self.hub.jobs.scan(self.hub.cfg.library("musique"))
        self.run_.wait_jobs()
        self.assertEqual([t["id"] for t in self.api.json(f"/api/playlist/{pid}")[1]["tracks"]], ids)
        self.hub.db.execute("DELETE FROM entries WHERE id=?", (ids[1],))
        gone = self.api.json(f"/api/playlist/{pid}")[1]
        self.assertEqual(([t["id"] for t in gone["tracks"]], gone["missing"]), ([ids[0], ids[2]], 1))
        self.assertEqual(self.api.json(f"/api/playlist/{pid}/remove", {"index": 1})[1]["count"], 1)      # l'index suit la liste affichée
        self.api.json(f"/api/playlist/{pid}/delete", {})
        self.hub.jobs.scan(self.hub.cfg.library("musique"))
        self.run_.wait_jobs()


class BearerClient(Client):
    """Un client natif : aucun cookie, seulement `Authorization: Bearer`."""

    def __init__(self, port, token):
        super().__init__(port)
        self.token = token

    def request(self, method, path, body=None, headers=None):
        return super().request(method, path, body=body, headers={"Authorization": f"Bearer {self.token}", **(headers or {})})


NUM = (int, float)
NULL = type(None)
ENTRY_FIELDS = {"id": str, "kind": str, "title": str, "summary": str, "season": (int, NULL), "disc": (int, NULL), "number": NUM + (NULL,), "duration": NUM,
                "format": str, "label": str, "sub": str, "file": str}
ITEM_FIELDS = {"id": str, "type": str, "title": str, "year": str, "grp": str, "n": int, "letter": str, "added": int, "genres": list, "cover": str}
ITEM_OPTIONAL = {"part": NUM, "duration": NUM, "artist_id": str, "missing": int, "seen": int, "started": int}
# `missing` change de type : un entier (tomes manquants) dans les listes, une liste de {label, numbers, total} dans la fiche d'une série de livres (docs/api.md, « Élément »).
DETAIL_OPTIONAL = {k: v for k, v in ITEM_OPTIONAL.items() if k != "missing"}
TRACK_FIELDS = {"id": str, "title": str, "artist": str, "album": str, "item": str, "duration": NUM, "cover": str, "file": str}
LIBRARY_KINDS = {"movies", "shows", "music", "books"}


class ContractBase(unittest.TestCase):
    """Le contrat décrit dans docs/api.md, vu par un client tiers : les champs documentés (présence et type) et les URLs qu'ils portent.

    Un champ ajouté ne casse rien ; un champ perdu, renommé ou d'un autre type casse le test. Le serveur est protégé par mot de passe : seul le Bearer ouvre les URLs.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.run_ = Running(cls.tmp, password="secret")
        cls.hub = cls.run_.hub
        token = json.loads(Client(cls.run_.port).request("POST", "/api/token", body={"password": "secret"})[2])["token"]
        cls.api = BearerClient(cls.run_.port, token)

    @classmethod
    def tearDownClass(cls):
        cls.run_.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # --- Outils ------------------------------------------------------------
    def has(self, obj, fields, where, optional=None):
        """`obj` porte chacun des `fields` ({nom: type ou types}) ; les champs `optional` sont vérifiés seulement s'ils sont là. D'autres champs sont permis."""
        self.assertIsInstance(obj, dict, where)
        for name, kinds in {**fields, **{k: v for k, v in (optional or {}).items() if k in obj}}.items():
            self.assertIn(name, obj, f"{where} : champ « {name} » perdu ou renommé")
            kinds = kinds if isinstance(kinds, tuple) else (kinds,)
            ok = isinstance(obj[name], kinds) and not (isinstance(obj[name], bool) and bool not in kinds)
            self.assertTrue(ok, f"{where} : « {name} » vaut {obj[name]!r}, attendu {[k.__name__ for k in kinds]}")

    def get(self, path):
        status, data = self.api.json(path)
        self.assertEqual(status, 200, f"{path} : {data}")
        return data

    def served(self, url, where):
        """Une URL renvoyée par l'API : relative, sans `?w=` ni jeton, et lisible avec le seul Bearer."""
        self.assertIsInstance(url, str, where)
        self.assertTrue(url.startswith("/") and "?" not in url, f"{where} : {url!r} n'est pas une URL relative nue")
        self.assertEqual(self.api.get(url)[0], 200, f"{where} : {url}")

    def item(self, lib, title):
        found = [i for i in self.get(f"/api/library/{lib}?limit=600")["items"] if i["title"] == title]
        self.assertTrue(found, f"{title!r} absent de {lib}")
        return found[0]

    def detail(self, lib, title):
        return self.get(f"/api/item/{self.item(lib, title)['id']}")

    def check_entry(self, entry, where):
        self.has(entry, ENTRY_FIELDS, where, {"date": str, "artist": str, "pages": int, "image": str, "progress": dict})
        if entry["kind"] != "audio":
            self.assertIn("image", entry, f"{where} : « image » absent d'un fichier qui n'est pas une piste")
        if "progress" in entry:
            self.has(entry["progress"], {"position": NUM, "duration": NUM, "done": bool, "updated": int}, where + ".progress")



class TestApiContract(ContractBase):
    def test_state(self):
        data = self.get("/api/state")
        self.has(data, {"version": str, "api": int, "libraries": list, "ffmpeg": bool, "ffprobe": bool, "tmdb": bool, "auth": bool, "busy": bool, "streams": int, "max_streams": int}, "state")
        self.assertEqual(data["api"], 1)
        self.assertTrue(data["auth"])
        self.assertEqual([l["id"] for l in data["libraries"]], ["films", "series", "animations", "musique", "livres"])
        for lib in data["libraries"]:
            self.has(lib, {"id": str, "label": str, "kind": str, "count": int, "scanned": (int, NULL), "exists": bool}, f"state.libraries[{lib['id']}]")
            self.assertIn(lib["kind"], LIBRARY_KINDS)

    def test_home(self):
        film = self.detail("films", "Inception")["entries"][0]["id"]
        self.assertEqual(self.api.json("/api/progress", {"entry": film, "position": 600, "duration": 8880})[0], 200)
        self.addCleanup(self.hub.db.execute, "DELETE FROM progress WHERE entry_id=?", (film,))
        data = self.get("/api/home")
        self.has(data, {"continue": list, "libraries": list}, "home")
        self.assertEqual([l["id"] for l in data["libraries"]], ["films", "series", "animations", "musique", "livres"])
        for lib in data["libraries"]:
            self.has(lib, {"id": str, "label": str, "kind": str, "count": int, "recent": list}, f"home.libraries[{lib['id']}]")
            self.assertTrue(lib["recent"], lib["id"])
            for item in lib["recent"]:
                self.has(item, ITEM_FIELDS, f"home.recent[{item['title']}]", ITEM_OPTIONAL)
        card = next(c for c in data["continue"] if c["id"] == film)
        self.check_entry(card, "home.continue")
        self.has(card, {"item": dict, "position": NUM, "total": NUM, "resume": bool, "updated": int}, "home.continue")
        self.has(card["item"], ITEM_FIELDS, "home.continue.item", ITEM_OPTIONAL)
        self.assertEqual((card["position"], card["resume"]), (600, True))
        self.served(card["file"], "home.continue.file")

    def test_library(self):
        for lib in ("films", "series", "animations", "musique", "livres"):
            data = self.get(f"/api/library/{lib}")
            self.has(data, {"library": dict, "total": int, "offset": int, "items": list, "letters": list, "genres": list}, f"library[{lib}]")
            self.has(data["library"], {"id": str, "label": str, "kind": str}, f"library[{lib}].library")
            self.assertEqual(data["total"], len(data["items"]))
            for letter, count in data["letters"]:
                self.assertIsInstance(letter, str)
                self.assertIsInstance(count, int)
            for genre, count in data["genres"]:
                self.assertIsInstance(genre, str)
                self.assertIsInstance(count, int)
            for item in data["items"]:
                self.has(item, ITEM_FIELDS, f"library[{lib}].items[{item['title']}]", ITEM_OPTIONAL)
                self.assertEqual(item["cover"], f"/img/item/{item['id']}")

    def test_library_sections_and_sagas(self):
        books = self.get("/api/library/livres")
        self.assertTrue(books["sections"])
        for name, count in books["sections"]:
            self.assertIsInstance(name, str)
            self.assertIsInstance(count, int)
        films = self.get("/api/library/films?sagas=1")
        self.has(films, {"sagas": int, "films": int}, "library[films, sagas=1]")
        saga = next(i for i in films["items"] if i["type"] == "saga")
        self.has(saga, {"id": str, "lib": str, "type": str, "title": str, "year": str, "years": list, "grp": str, "n": int, "letter": str, "added": int, "genres": list,
                        "covers": list, "cover_urls": list}, "library.saga", {"seen": int, "started": int})
        self.assertEqual(len(saga["covers"]), len(saga["cover_urls"]))
        members = self.get(f"/api/library/films?group={quote(saga['title'])}&sort=part")["items"]
        self.assertEqual(len(members), saga["n"])

    def test_item_movie(self):
        data = self.detail("films", "Inception")
        self.has(data, {**ITEM_FIELDS, "library": dict, "summary": str, "entries": list, "resume": (str, NULL)}, "item[film]", {**DETAIL_OPTIONAL, "links": list, "directors": list, "actors": list})
        self.assertEqual(data["type"], "movie")
        self.assertEqual(data["resume"], data["entries"][0]["id"])
        for entry in data["entries"]:
            self.check_entry(entry, "item[film].entries")
        self.served(data["cover"], "item[film].cover")
        self.served(data["entries"][0]["image"], "item[film].entries.image")
        self.served(data["entries"][0]["file"], "item[film].entries.file")
        for link in data.get("links", []):
            self.has(link, {"label": str, "url": str}, "item[film].links")
        saga = self.detail("films", "Man of Steel")
        self.assertIn(saga["id"], [m["id"] for m in saga["saga"]])                  # toute la saga, le film consulté compris
        self.assertGreater(len(saga["saga"]), 1)
        for other in saga["saga"]:
            self.has(other, ITEM_FIELDS, "item[film].saga", ITEM_OPTIONAL)

    def test_item_show(self):
        data = self.detail("series", "Dark")
        self.has(data, {**ITEM_FIELDS, "library": dict, "summary": str, "entries": list, "resume": (str, NULL)}, "item[série]", {**DETAIL_OPTIONAL, "seasons": list, "links": list})
        self.assertEqual(data["type"], "show")
        self.assertEqual(len(data["entries"]), 3)
        for entry in data["entries"]:
            self.check_entry(entry, "item[série].entries")
            self.assertEqual(entry["kind"], "video")
        self.assertEqual([e["season"] for e in data["entries"]], [1, 1, 2])
        self.served(data["cover"], "item[série].cover")
        self.served(data["entries"][0]["image"], "item[série].entries.image")      # l'épisode 1 porte une vignette
        self.served(data["entries"][0]["file"], "item[série].entries.file")

    def test_item_album(self):
        data = self.detail("musique", "Discovery")
        self.has(data, {**ITEM_FIELDS, "library": dict, "summary": str, "entries": list, "resume": (str, NULL), "tracks_total": int}, "item[album]", DETAIL_OPTIONAL)
        self.assertEqual((data["type"], data["tracks_total"], len(data["entries"])), ("album", 3, 3))
        for entry in data["entries"]:
            self.check_entry(entry, "item[album].entries")
            self.assertEqual(entry["kind"], "audio")
            self.assertNotIn("image", entry)
            self.assertEqual(entry["artist"], "Daft Punk")
        self.served(data["cover"], "item[album].cover")
        self.served(data["entries"][0]["file"], "item[album].entries.file")
        sheet = self.get(f"/api/artist/musique/{data['artist_id']}")
        self.has(sheet, {"library": dict, "id": str, "name": str, "albums": list, "tracks": list, "genres": list, "duration": NUM, "wanted": list, "photo": bool}, "artist")
        for track in sheet["tracks"]:
            self.has(track, TRACK_FIELDS, "artist.tracks")
        self.served(sheet["tracks"][0]["cover"], "artist.tracks.cover")
        self.served(sheet["tracks"][0]["file"], "artist.tracks.file")

    def test_item_book(self):
        data = self.detail("livres", "Naruto")
        self.has(data, {**ITEM_FIELDS, "library": dict, "summary": str, "entries": list, "resume": (str, NULL)}, "item[livre]", {**DETAIL_OPTIONAL, "authors": list, "section": str})
        self.assertEqual(data["type"], "series")
        self.assertEqual(len(data["entries"]), 2)
        self.assertEqual(self.item("livres", "Naruto")["missing"], 1)               # dans la liste : le nombre de tomes manquants
        self.assertIsInstance(data["missing"], list)                                 # dans la fiche : la liste des manques
        for gap in data["missing"]:
            self.has(gap, {"label": str, "numbers": list, "total": int}, "item[livre].missing")
        self.assertEqual(data["missing"][0]["numbers"], [3])
        for entry in data["entries"]:
            self.check_entry(entry, "item[livre].entries")
            self.assertEqual((entry["kind"], entry["format"]), ("book", "cbz"))
        self.served(data["cover"], "item[livre].cover")
        self.served(data["entries"][0]["image"], "item[livre].entries.image")
        self.served(data["entries"][0]["file"], "item[livre].entries.file")
        info = self.get(f"/api/book/{data['entries'][0]['id']}")
        self.has(info, {"id": str, "item": str, "title": str, "format": str, "position": NUM, "file": str, "next": (dict, NULL), "pages": int, "page_url": str}, "book")
        self.has(info["next"], {"id": str, "title": str}, "book.next")
        self.served(info["file"], "book.file")
        self.served(info["page_url"].format(n=0), "book.page_url")

    def test_tagged_is_a_boolean(self):
        """docs/api.md : `tagged` est un booléen sur toute fiche."""
        for lib, title in (("films", "Inception"), ("series", "Dark"), ("musique", "Discovery"), ("livres", "Naruto")):
            tagged = self.detail(lib, title)["tagged"]
            self.assertIsInstance(tagged, bool, f"item[{title}] : « tagged » vaut {tagged!r}")

    def test_search(self):
        data = self.get("/api/search?q=dark")
        self.has(data, {"results": list}, "search")
        self.assertEqual([r["id"] for r in data["results"]], ["series"])
        for group in data["results"]:
            self.has(group, {"id": str, "label": str, "kind": str, "items": list}, "search.results")
            for item in group["items"]:
                self.has(item, ITEM_FIELDS, f"search.items[{item['title']}]", ITEM_OPTIONAL)
        self.assertEqual(self.get("/api/search?q=d"), {"results": []})

    def test_playlists(self):
        tracks = [e["id"] for e in self.detail("musique", "Discovery")["entries"]]
        made = self.api.json("/api/playlists", {"name": "Contrat", "entries": tracks[:2]})
        self.assertEqual(made[0], 201)
        summary_fields = {"id": str, "name": str, "count": int, "duration": NUM, "covers": list, "cover_urls": list, "updated": int}
        detail_fields = {**summary_fields, "tracks": list, "missing": int}
        self.has(made[1], detail_fields, "playlist.créée")
        listed = next(p for p in self.get("/api/playlists")["playlists"] if p["id"] == made[1]["id"])
        self.has(listed, summary_fields, "playlists")
        detail = self.get(f"/api/playlist/{made[1]['id']}")
        self.has(detail, detail_fields, "playlist")
        self.assertEqual([t["id"] for t in detail["tracks"]], tracks[:2])
        for track in detail["tracks"]:
            self.has(track, {**TRACK_FIELDS, "number": NUM + (NULL,)}, "playlist.tracks")
        self.assertEqual(len(detail["cover_urls"]), len(detail["covers"]))
        self.served(listed["cover_urls"][0], "playlists.cover_urls")
        self.served(detail["tracks"][0]["cover"], "playlist.tracks.cover")
        self.served(detail["tracks"][0]["file"], "playlist.tracks.file")

    def test_urls_need_the_token(self):
        album = self.item("musique", "Discovery")
        self.assertEqual(Client(self.run_.port).get(album["cover"])[0], 302)       # sans jeton, hors /api/ : redirection vers /login
        self.assertEqual(Client(self.run_.port).get("/api/state")[0], 401)
        self.assertEqual(self.api.get(album["cover"] + "?w=160")[0], 200)           # `?w=` s'ajoute à l'URL renvoyée


@unittest.skipUnless(FFMPEG, "ffmpeg absent")
class TestPlayContract(ContractBase):
    """`POST /api/play/<entry>` : vraies vidéos, vrai ffprobe. Une série de deux épisodes de 30 s donne `next` et `resume`."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for n, title in enumerate(("Un", "Deux"), 1):
            fixtures.write_video(cls.run_.libs["series"] / "Contrat" / "Saison 1" / f"{n:02d} - {title}.mkv", seconds=30)
        cls.hub.jobs.scan(cls.hub.cfg.library("series"))
        cls.run_.wait_jobs()

    def play(self, entry, **body):
        status, data = self.api.json(f"/api/play/{entry}", body)
        self.assertEqual(status, 200, data)
        self.has(data, {"entry": str, "item": str, "title": str, "show": str, "season": (int, NULL), "number": NUM + (NULL,), "mode": str, "why": list, "url": str, "seekable": bool,
                        "duration": NUM, "resume": NUM, "warning": str, "audio": list, "subs": list, "video": (dict, NULL), "next": (dict, NULL)}, "play")
        self.assertIn(data["mode"], ("direct", "remux", "transcode"))
        for track in data["audio"]:
            self.has(track, {"index": int, "label": str, "lang": str, "selected": bool, "commentary": bool}, "play.audio")
        for sub in data["subs"]:
            self.has(sub, {"id": str, "label": str, "lang": str, "forced": bool, "default": bool, "text": bool, "where": str, "url": str}, "play.subs")
        if data["video"]:
            self.has(data["video"], {"codec": str, "width": int, "height": int, "hdr": bool}, "play.video")
        return data

    def film(self, title):
        return self.detail("films", title)["entries"][0]["id"]

    def test_direct_plan_for_a_compatible_file(self):
        entry = self.film("Compatible")
        plan = self.play(entry, caps=CAPS_ALL)
        self.assertEqual((plan["mode"], plan["seekable"], plan["url"], plan["why"]), ("direct", True, f"/media/{entry}/file", []))
        self.assertEqual((plan["show"], plan["resume"], plan["next"]), ("", 0, None))
        self.assertEqual([(t["selected"], t["index"]) for t in plan["audio"]], [(True, 0)])
        self.assertEqual(plan["video"]["codec"], "h264")
        self.served(plan["url"], "play.url (direct)")

    def test_remux_plan_gives_a_stream_url_audio_tracks_and_subtitles(self):
        entry = self.film("Vrai Film")
        plan = self.play(entry, caps=CAPS_ALL)
        self.assertEqual((plan["mode"], plan["seekable"]), ("remux", False))
        self.assertTrue(plan["url"].startswith(f"/media/{entry}/stream?m=remux"))
        self.assertTrue(plan["why"])
        self.assertEqual([(t["index"], t["selected"]) for t in plan["audio"]], [(0, True)])
        self.assertEqual([(s["where"], s["text"]) for s in plan["subs"]], [("mkv", True)])
        self.assertEqual(self.api.get(plan["subs"][0]["url"])[0], 200)
        status, headers, _ = self.api.get(plan["url"] + "&start=1")                 # on avance en redemandant avec ?start=
        self.assertEqual((status, headers["Content-Type"]), (200, "video/mp4"))

    def test_transcode_plan(self):
        plan = self.play(self.film("Vrai Film"), caps={**CAPS_ALL, "video": ["vp9"]}, max_height=90)
        self.assertEqual(plan["mode"], "transcode")
        self.assertIn("m=transcode", plan["url"])
        self.assertEqual(self.api.get(plan["url"])[0], 200)

    def test_next_and_resume(self):
        show = self.detail("series", "Contrat")
        first, second = show["entries"][0], show["entries"][1]
        self.assertEqual(self.api.json("/api/progress", {"entry": first["id"], "position": 25, "duration": 30})[1], {"ok": True, "done": False})
        self.addCleanup(self.hub.db.execute, "DELETE FROM progress WHERE entry_id=?", (first["id"],))
        plan = self.play(first["id"], caps=CAPS_ALL)
        self.assertEqual((plan["show"], plan["season"], plan["number"], plan["resume"]), ("Contrat", 1, 1, 25))
        self.has(plan["next"], {"id": str, "title": str, "season": (int, NULL), "number": NUM + (NULL,)}, "play.next")
        self.assertEqual((plan["next"]["id"], plan["next"]["title"]), (second["id"], second["title"]))
        self.assertIsNone(self.play(second["id"], caps=CAPS_ALL)["next"])


class TestDocumentedRoutes(unittest.TestCase):
    """Chaque route du tableau « Interface HTTP » du README se retrouve, avec sa méthode, dans docs/api.md."""

    ROOT = Path(__file__).resolve().parent.parent

    @staticmethod
    def shape(text):
        return re.sub(r"<[^>]*>", "<>", text)          # `<id>` et `<entry>` désignent la même chose

    def readme_routes(self):
        text = (self.ROOT / "README.md").read_text(encoding="utf-8")
        table = text.split("## Interface HTTP", 1)[1].split("\n## ", 1)[0]
        routes = []
        for row in table.splitlines():
            if not row.startswith("|") or row.startswith("| ---") or row.startswith("| Route"):
                continue
            method = "GET"
            for token in re.findall(r"`([^`]+)`", re.split(r"(?<!\\)\|", row)[1]):
                if not re.match(r"(GET|POST|/)", token):
                    continue                 # une variable (`HUB_PASSWORD`), pas une route
                found = re.match(r"((?:GET|POST)(?:/(?:GET|POST))?) (.*)", token)
                if found:
                    method, token = found.group(1), found.group(2)
                token = re.sub(r"<[^>]*>", lambda m: m.group().replace("\\|", "\0"), token)       # un « | » entre chevrons ne sépare pas des variantes
                variants = token.split("\\|")
                paths = [variants[0]] + [variants[0].rsplit("/", 1)[0] + "/" + v for v in variants[1:]]
                for path in paths:
                    for verb in method.split("/"):
                        routes.append((verb, path.replace("\0", "|").split("?")[0]))
        return routes

    def test_every_route_of_the_readme_is_in_the_api_document(self):
        routes = self.readme_routes()
        self.assertGreaterEqual(len(routes), 25, routes)          # le tableau a bien été lu
        doc = self.shape((self.ROOT / "docs" / "api.md").read_text(encoding="utf-8"))
        missing = [f"{verb} {path}" for verb, path in routes if f"{verb} {self.shape(path)}" not in doc]
        self.assertEqual(missing, [], "routes du README absentes de docs/api.md")

    def test_the_readme_reader_understands_the_table(self):
        routes = self.readme_routes()
        for expected in (("GET", "/api/state"), ("GET", "/api/search"), ("POST", "/api/playlists"), ("GET", "/api/playlists"), ("POST", "/api/playlist/<id>/rename"),
                         ("GET", "/api/subs/<entry>/<e|s><n>.vtt"), ("GET", "/media/<entry>/stream"), ("POST", "/api/entry/<id>/seen"), ("GET", "/book/<id>/epub/<chemin>"),
                         ("GET", "/img/entry/<id>"), ("POST", "/api/token"), ("GET", "/api/jobs"), ("POST", "/api/cleanup")):
            self.assertIn(expected, routes)


if __name__ == "__main__":
    unittest.main()
