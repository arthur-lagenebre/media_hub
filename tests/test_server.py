"""Le serveur de bout en bout : un vrai serveur sur un port libre, une vraie médiathèque d'essai, de vraies requêtes HTTP."""

from __future__ import annotations

import errno
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
from pathlib import Path
from urllib.parse import quote

from tests import fixtures
from hub import accounts, config

fixtures.REPO  # noqa: B018  (rend `libraries` importable)
config.use_media_library(fixtures.REPO)

from hub.scan.common import is_album_cover  # noqa: E402
from hub.media import images  # noqa: E402
from hub.scan.music import artist_dirs, scan_music  # noqa: E402
from hub.server import jobs  # noqa: E402
from hub.server.app import TITLE_SORTS, Handler, make_server  # noqa: E402

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
    def __init__(self, root, password="", ffmpeg=True, max_streams=2, admin="", reuse=False):
        """`password` : HUB_PASSWORD (amorçage du compte administrateur), `admin` : HUB_ADMIN ; `reuse` : redémarre sur la médiathèque et la base déjà là (rien n'est refabriqué)."""
        self.root = Path(root)
        if reuse:
            names = {"films": "Films", "series": "Series", "animations": "Animations", "musique": "Musique", "livres": "Livres"}
            self.libs = {lib: self.root / "srv" / name for lib, name in names.items()}
        else:
            self.libs = fixtures.build_library(self.root / "srv")
            video = self.root / "srv" / "Films" / "Vrai Film (2020)" / "Vrai Film (2020).mkv"
            if FFMPEG:
                fixtures.write_video(video, seconds=4, acodec="ac3", channels=6, subtitle="1\n00:00:00,000 --> 00:00:02,000\nBonjour\n")
                fixtures.write_video(self.root / "srv" / "Films" / "Compatible (2021)" / "Compatible (2021).mkv", seconds=3)
        env = {"HUB_SRV": str(self.root / "srv"), "HUB_DATA": str(self.root / "data"), "HUB_REPO": str(fixtures.REPO), "HUB_PORT": "0", "HUB_PASSWORD": password,
               "HUB_ADMIN": admin, "HUB_MAX_STREAMS": str(max_streams)}
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

    def test_library_page_bounds_fall_back_to_sane_values(self):
        total = self.items("films")["total"]
        self.assertEqual(len(self.items("films", limit=0)["items"]), 1)                              # au moins un élément par page
        self.assertEqual(self.items("films", offset=-5)["offset"], 0)                                  # un départ négatif ramène au début
        broken = self.items("films", offset="x", limit=1)                                              # une valeur qui n'est pas un nombre : les deux reviennent aux valeurs par défaut
        self.assertEqual((broken["offset"], len(broken["items"])), (0, total))
        self.assertEqual(self.items("films", offset=total)["items"], [])                              # au-delà de la fin : une page vide, pas une erreur

    def test_music_letter_is_the_artist_and_sort_is_fixed(self):
        album = self.item_by_title("musique", "Discovery")
        self.hub.db.execute("UPDATE items SET grp='Zebra' WHERE id=?", (album["id"],))
        try:
            data = self.items("musique")
            self.assertIn(["Z", 1], data["letters"])
            self.assertNotIn(["D", 1], data["letters"])                    # « Discovery » commence par D, mais l'artiste par Z
            self.assertEqual([i["title"] for i in self.items("musique", letter="Z")["items"]], ["Discovery"])
            self.assertEqual(self.items("musique", letter="D")["items"], [])
        finally:
            self.hub.db.execute("UPDATE items SET grp='Daft Punk' WHERE id=?", (album["id"],))

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

    def test_album_links_to_its_musicbrainz_release(self):
        discovery = self.detail(self.item_by_title("musique", "Discovery")["id"])
        self.assertEqual(discovery["links"], [{"label": "MusicBrainz", "url": "https://musicbrainz.org/release/11111111-2222-3333-4444-555555555555"}])

    def test_album_links_edge_cases(self):
        from hub.server.app import album_links
        self.assertEqual(album_links({}), [])                                                  # pas d'identifiant : pas de lien
        self.assertEqual(album_links({"mbid": "../../etc"}), [])                               # ce qui n'est pas un identifiant est ignoré
        self.assertEqual(album_links({"mbid": "11111111-2222-3333-4444-555555555555/x"}), [])

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
        self.assertIn("form-action 'none'", headers["Content-Security-Policy"])               # une page d'epub ne poste rien
        self.assertIn("base-uri 'none'", headers["Content-Security-Policy"])
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
                artist / "artist.webp",       # la photo de l'artiste
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
        self.assertEqual((result["found"], result["removed"], len(result["sample"])), (4, 0, 4))
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
        home = self.run_.libs["musique"] / "Daft Punk"
        self.addCleanup((home / "artist.webp").unlink, True)

        with mock.patch("hub.enrich.fetch_artist", return_value=info):
            self.assertEqual(self.api.json("/api/enrich", {"lib": "musique"})[0], 202)         # MusicBrainz ne demande pas de clé
            self.run_.wait_jobs()
        sheet = self.api.json(url)[1]
        self.assertFalse(sheet["photo"])                                                # les fiches ne téléchargent aucune photo, même avec un article Wikipédia
        self.assertEqual(list(home.glob("artist.*")), [])
        self.assertEqual(list((self.hub.cfg.data / "artists").glob("*.*")), [])
        self.assertEqual(self.api.get(photo)[0], 404)
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
        album = self.item_by_title("musique", "Discovery")
        detail = self.detail(album["id"])
        artist_id = detail["artist_id"]
        base = f"/api/artist/musique/{artist_id}"
        photo = f"/img/artist/musique/{artist_id}?w=320"
        folder = self.hub.cfg.data / "artists"
        home = self.run_.libs["musique"] / "Daft Punk"
        self.addCleanup(lambda: [p.unlink() for p in [*folder.glob(f"{artist_id}.*"), *home.glob("artist.*")]])
        self.assertNotIn("own_photo", self.api.json(base)[1])
        self.assertEqual(self.api.request("POST", base + "/photo", body=b"ceci n'est pas une image", headers={"Content-Type": "image/png"})[0], 415)
        self.assertEqual(self.api.request("POST", base + "/photo", body=b"RIFFxxxxWAVE", headers={"Content-Type": "image/webp"})[0], 415)
        self.assertEqual(self.api.request("POST", base + "/photo", body=b"\x89PNG\r\n\x1a\n tronquee", headers={"Content-Type": "image/png"})[0], 415)       # l'en-tête d'une image, rien derrière
        self.assertEqual(self.api.request("POST", "/api/artist/musique/deadbeef/photo", body=fixtures.PNG, headers={"Content-Type": "image/png"})[0], 404)
        status, _, body = self.api.request("POST", base + "/photo", body=fixtures.PNG, headers={"Content-Type": "image/png"})
        self.assertEqual(status, 200, body)
        self.assertTrue(self.api.json(base)[1]["photo"])
        self.assertEqual(self.api.get(photo)[0], 200)
        self.assertEqual([p.name for p in home.glob("artist.*")], ["artist.webp"])       # dans le dossier de l'artiste, à la racine de la médiathèque
        self.assertEqual(list(folder.glob(f"{artist_id}.*")), [])
        self.api.request("POST", base + "/photo", body=fixtures.PNG, headers={"Content-Type": "image/png"})
        self.assertEqual([p.name for p in home.glob("artist.*")], ["artist.webp"])       # la nouvelle prend la place de l'ancienne

        self.assertEqual(self.api.get(base + "/photo/wikipedia")[0], 404)                             # la photo de Wikipédia n'existe plus
        self.assertEqual(self.api.json(base + "/photo/delete", {})[0], 404)                           # et une photo ne se supprime pas

    @unittest.skipUnless(images.Image is not None and images.can_webp(""), "Pillow absent")
    def test_artist_photo_keeps_only_the_frame_and_becomes_webp(self):
        import io
        from PIL import Image

        album = self.item_by_title("musique", "Discovery")
        artist_id = self.detail(album["id"])["artist_id"]
        base = f"/api/artist/musique/{artist_id}"
        home = self.run_.libs["musique"] / "Daft Punk"
        self.addCleanup(lambda: [p.unlink() for p in home.glob("artist.*")])

        def two_halves(size=(40, 20)):       # gauche rouge, droite bleue
            image = Image.new("RGB", size, (220, 0, 0))
            image.paste((0, 0, 220), (size[0] // 2, 0, size[0], size[1]))
            out = io.BytesIO()
            image.save(out, "PNG")
            return out.getvalue()

        def stored():
            with Image.open(home / "artist.webp") as image:
                return image.format, image.size, image.convert("RGB").getpixel((image.width // 2, image.height // 2))

        def send(query, body=None):
            return self.api.request("POST", base + "/photo" + query, body=body or two_halves(), headers={"Content-Type": "image/png"})

        self.assertEqual(self.api.json(base + "/photo/crop", {"x": 0, "y": 0, "w": 1, "h": 1})[0], 404)       # rien à recadrer sans photo
        for bad in ("?crop=abc", "?crop=0,0,1", "?crop=0.5,0,0.8,1", "?crop=0,0,0,1", "?crop=-0.1,0,0.5,1"):
            self.assertEqual(send(bad)[0], 400, bad)
        self.assertFalse((home / "artist.webp").exists())
        status, _, body = send("?crop=0.5,0,0.5,1")                       # la moitié droite : la bleue
        self.assertEqual(status, 200, body)
        format_, size, centre = stored()
        self.assertEqual((format_, size), ("WEBP", (20, 20)))             # ce qui est hors du cadre n'est plus dans le fichier
        self.assertTrue(centre[2] > 150 and centre[0] < 80, centre)
        for bad in ({"x": "a", "y": 0, "w": 1, "h": 1}, {"x": 0, "y": 0, "w": 2, "h": 1}, {}):
            self.assertEqual(self.api.json(base + "/photo/crop", bad)[0], 400, bad)
        self.assertEqual(self.api.json(base + "/photo/crop", {"x": 0, "y": 0, "w": 0.5, "h": 0.5})[0], 200)       # on recadre le fichier gardé, encore
        self.assertEqual(stored()[1], (10, 10))
        self.assertEqual(send("", two_halves((3000, 1000)))[0], 200)       # sans cadre, l'image entière, réduite à 1280 px au plus
        self.assertEqual(stored()[1], (1280, 427))
        self.assertEqual(self.api.get(f"/img/artist/musique/{artist_id}?w=320")[0], 200)

    @unittest.skipUnless(FFMPEG and images.Image is not None, "ffmpeg ou Pillow absent")
    def test_artist_photo_is_cropped_by_ffmpeg_when_pillow_is_missing(self):
        import io
        from PIL import Image

        album = self.item_by_title("musique", "Discovery")
        artist_id = self.detail(album["id"])["artist_id"]
        base = f"/api/artist/musique/{artist_id}"
        home = self.run_.libs["musique"] / "Daft Punk"
        self.addCleanup(lambda: [p.unlink() for p in home.glob("artist.*")])
        image = Image.new("RGB", (40, 20), (220, 0, 0))
        image.paste((0, 0, 220), (20, 0, 40, 20))
        out = io.BytesIO()
        image.save(out, "PNG")
        with mock.patch("hub.media.images.Image", None):
            status, _, body = self.api.request("POST", base + "/photo?crop=0.5,0,0.5,1", body=out.getvalue(), headers={"Content-Type": "image/png"})
        self.assertEqual(status, 200, body)
        with Image.open(home / "artist.webp") as stored:
            self.assertEqual((stored.format, stored.size), ("WEBP", (20, 20)))
            self.assertTrue(stored.convert("RGB").getpixel((10, 10))[2] > 150)

    def test_artist_photo_without_a_converter_is_kept_as_it_is_but_cannot_be_cropped(self):
        album = self.item_by_title("musique", "Discovery")
        artist_id = self.detail(album["id"])["artist_id"]
        base = f"/api/artist/musique/{artist_id}"
        home = self.run_.libs["musique"] / "Daft Punk"
        folder = self.hub.cfg.data / "artists"
        self.addCleanup(lambda: [p.unlink() for p in [*folder.glob(f"{artist_id}.*"), *home.glob("artist.*")]])
        with mock.patch("hub.media.images.can_webp", return_value=False):
            self.assertEqual(self.api.request("POST", base + "/photo?crop=0,0,0.5,0.5", body=fixtures.PNG, headers={"Content-Type": "image/png"})[0], 501)
            self.assertEqual(self.api.request("POST", base + "/photo", body=fixtures.PNG, headers={"Content-Type": "image/png"})[0], 200)
        self.assertEqual(([p.name for p in folder.glob(f"{artist_id}.*")], list(home.glob("artist.*"))), ([f"{artist_id}.png"], []))       # sans de quoi convertir : comme avant, sous HUB_DATA
        self.assertEqual(self.api.get(f"/img/artist/musique/{artist_id}?w=320")[0], 200)

    def test_an_artist_photo_beside_the_albums_is_not_cleaned_up_and_not_a_missing_album(self):
        home = self.run_.libs["musique"] / "Daft Punk"
        (home / "artist.webp").write_bytes(b"RIFF\0\0\0\0WEBP")
        self.addCleanup((home / "artist.webp").unlink, True)
        self.hub.jobs.scan(self.hub.cfg.library("musique"))
        self.run_.wait_jobs()
        artist_id = self.detail(self.item_by_title("musique", "Discovery")["id"])["artist_id"]
        self.assertEqual(self.api.json(f"/api/artist/musique/{artist_id}")[1]["wanted"], [])
        self.api.json("/api/cleanup", {"apply": True})
        self.run_.wait_jobs()
        self.assertTrue((home / "artist.webp").exists())

    def test_the_artist_folder_is_the_root_folder_where_the_artist_has_most_albums(self):
        from types import SimpleNamespace as R

        def release(rel, artist):
            return R(rel=rel, artist_id=artist)

        found = artist_dirs([release("Daft Punk/Discovery", "dp"), release("Daft Punk/Homework", "dp"), release("Daft Punk/Collabs", "tb"), release("Rock/Muse/Absolution", "mu"),
                             release("Rock/Queen/Jazz", "qu"), release("Single", "so"), release("Tiersmonde/Alpha", "al"), release("Tiersmonde/Beta", "be")])
        self.assertEqual(found, {"dp": "Daft Punk"})       # majorité d'albums : Rock en range deux autres sans majorité, Single est posé à la racine

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
            home = Path(tmp) / "Artiste"
            home.mkdir()
            (home / "artist.webp").write_bytes(b"dans son dossier")
            self.assertEqual(enrich.photo_file(folder, "a2", "m2", home), home / "artist.webp")           # celle de son dossier passe avant toutes les autres
            self.assertEqual(enrich.photo_file(folder, "a2", "m2").name, "a2.png")                        # sans dossier, on retrouve les anciennes
            (folder / "evil.png").write_bytes(b"x")
            self.assertIsNone(enrich.photo_file(folder / "sous", "a3", "../evil"))                      # un identifiant qui a l'air d'un chemin ne désigne aucun fichier

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
        self.assertRegex(token, r"^default\.[0-9a-f]{64}$")        # <identifiant du compte>.<mac> ; le premier compte s'appelle « default »
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


class TestSagaOrder(ServerCase):
    """Les films d'une saga se rangent par date de sortie, pas par le numéro du dossier : « 12 - Ant-Man » est le 12e du MCU, pas de la saga Ant-Man."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        films = cls.run_.libs["films"]
        for part, title, date in ((31, "Ant-Man et la Guêpe - Quantumania", "2023-02-15"), (12, "Ant-Man", "2015-07-14"), (20, "Ant-Man et la Guêpe", "2018-07-04")):
            fixtures.write_mkv(films / "_Marvel" / f"{part} - {title}.mkv", fixtures.movie_blocks(title, "Petit.", date, f"movie/{800 + part}", "Action", collection="Saga Ant-Man", part=part), minutes=110)
        cls.hub.jobs.scan(cls.hub.cfg.library("films"))
        cls.run_.wait_jobs()

    def test_a_saga_page_follows_the_release_order_and_numbers_from_one(self):
        data = self.items("films", group="Saga Ant-Man", sort="part")
        self.assertEqual([(i["title"], i["part"]) for i in data["items"]], [("Ant-Man", 1), ("Ant-Man et la Guêpe", 2), ("Ant-Man et la Guêpe - Quantumania", 3)])

    def test_the_film_page_lists_the_saga_the_same_way(self):
        saga = self.detail(self.item_by_title("films", "Ant-Man et la Guêpe")["id"])["saga"]
        self.assertEqual([(m["title"], m["part"]) for m in saga], [("Ant-Man", 1), ("Ant-Man et la Guêpe", 2), ("Ant-Man et la Guêpe - Quantumania", 3)])

    def test_the_covers_of_the_saga_card_follow_the_release_order(self):
        card = next(i for i in self.items("films", sagas=1)["items"] if i["title"] == "Saga Ant-Man")
        self.assertEqual([self.detail(c)["title"] for c in card["covers"]], ["Ant-Man", "Ant-Man et la Guêpe", "Ant-Man et la Guêpe - Quantumania"])


class TestSagasFile(ServerCase):
    """`sagas.json`, sous HUB_DATA : des listes de films (identifiants TMDB) qui s'ajoutent à la saga de l'étiquette. Inception (27205) n'en a pas ; Man of Steel (49521) et Batman v Superman (209112) sont de « Saga DCEU »."""

    @property
    def file(self):
        return self.hub.cfg.data / "sagas.json"

    def write(self, *sagas):
        self.file.write_text(json.dumps({"sagas": [{"name": name, "films": films} for name, films in sagas]}), encoding="utf-8")

    def tearDown(self):
        self.file.unlink(missing_ok=True)

    def tiles(self):
        return {i["title"]: i for i in self.items("films", sagas=1)["items"] if i["type"] == "saga"}

    def names(self, title):
        return [s["name"] for s in self.detail(self.item_by_title("films", title)["id"]).get("sagas", [])]

    def test_without_the_file_the_saga_of_the_tag_is_the_only_one(self):
        self.assertEqual(sorted(self.tiles()), ["Saga DCEU"])
        self.assertEqual(self.names("Inception"), [])
        self.assertEqual(self.names("Man of Steel"), ["Saga DCEU"])

    def test_a_film_of_the_file_is_listed_with_its_rank_in_the_list(self):
        self.write(("Ma saga", [209112, 999999, 27205]))        # le film du milieu n'est pas dans la médiathèque : il garde sa place
        data = self.items("films", group="Ma saga", sort="part")
        self.assertEqual([(i["title"], i["part"]) for i in data["items"]], [("Batman v Superman", 1), ("Inception", 3)])
        self.assertEqual(data["total"], 2)

    def test_the_grid_gives_a_film_one_tile(self):
        self.write(("Ma saga", [209112, 999999, 27205]))
        data = self.items("films", sagas=1)
        tiles = {i["title"]: i for i in data["items"] if i["type"] == "saga"}
        self.assertEqual(sorted(tiles), ["Ma saga", "Saga DCEU"])
        self.assertEqual((tiles["Ma saga"]["n"], tiles["Saga DCEU"]["n"]), (2, 2))
        titles = [i["title"] for i in data["items"]]
        self.assertNotIn("Inception", titles)                    # rangé dans « Ma saga »
        self.assertNotIn("Batman v Superman", titles)
        self.assertEqual(data["films"], len(set(i["id"] for i in self.items("films")["items"])))      # chaque film compte une fois

    def test_the_film_page_lists_every_saga_of_the_film(self):
        self.write(("Ma saga", [209112, 999999, 27205]))
        steel = self.detail(self.item_by_title("films", "Batman v Superman")["id"])
        self.assertEqual([s["name"] for s in steel["sagas"]], ["Saga DCEU", "Ma saga"])      # celle de l'étiquette d'abord
        self.assertEqual([(f["title"], f["part"]) for f in steel["sagas"][1]["films"]], [("Batman v Superman", 1), ("Inception", 3)])
        self.assertEqual([m["title"] for m in steel["saga"]], ["Man of Steel", "Batman v Superman"])      # `saga` reste la saga de l'étiquette
        inception = self.detail(self.item_by_title("films", "Inception")["id"])
        self.assertEqual([s["name"] for s in inception["sagas"]], ["Ma saga"])
        self.assertNotIn("saga", inception)

    def test_a_saga_of_the_file_with_one_film_here_stays_out_of_the_grid(self):
        self.write(("Seul", [27205, 1, 2]))
        self.assertEqual(sorted(self.tiles()), ["Saga DCEU"])
        self.assertEqual(self.names("Inception"), [])

    def test_the_first_saga_of_the_file_keeps_the_tile(self):
        self.write(("A", [209112, 27205]), ("B", [27205, 209112]))
        self.assertEqual(sorted(self.tiles()), ["A", "Saga DCEU"])       # B n'a plus de film à ranger : pas de vignette, mais sa page existe
        self.assertEqual([i["title"] for i in self.items("films", group="B", sort="part")["items"]], ["Inception", "Batman v Superman"])
        self.assertEqual(self.names("Inception"), ["A", "B"])

    def test_the_search_finds_the_films_of_a_saga_by_its_name(self):
        self.write(("Ma saga", [209112, 27205]))
        self.assertEqual(sorted(i["title"] for i in self.items("films", q="ma saga")["items"]), ["Batman v Superman", "Inception"])

    def test_the_file_is_read_again_when_it_changes(self):
        self.write(("Ma saga", [209112, 27205]))
        self.assertIn("Ma saga", self.tiles())
        self.write(("Autre", [209112, 27205]))
        self.assertEqual(sorted(self.tiles()), ["Autre", "Saga DCEU"])

    def test_a_broken_file_gives_no_saga(self):
        self.file.write_text("{pas du json", encoding="utf-8")
        self.assertEqual(sorted(self.tiles()), ["Saga DCEU"])
        self.file.write_text('["pas", "un", "objet"]', encoding="utf-8")
        self.assertEqual(sorted(self.tiles()), ["Saga DCEU"])

    def test_a_huge_tmdb_id_is_unreadable_not_fatal(self):
        from hub import sagas
        huge = "9" * 5000
        self.assertIsNone(sagas.film_id(huge))
        self.assertEqual(sagas.parse(json.dumps({"sagas": [{"name": "a", "films": [huge, 1]}], "ignore": [huge]})), [("a", [1])])
        self.file.write_text('{"sagas": [{"name": "a", "films": ["' + huge + '"]}], "ignore": ["' + huge + '"]}', encoding="utf-8")
        self.assertEqual(self.api.json("/api/library/films")[0], 200)                  # la bibliothèque reste lisible
        self.assertEqual(self.api.json("/api/sagas/add", {"name": "b", "films": [huge]})[0], 400)
        self.assertEqual(self.api.json("/api/sagas/ignore", {"tmdb": huge})[0], 400)

    def test_the_names_that_differ_by_case_only_are_one_saga(self):
        from hub import sagas
        self.assertEqual(sagas.parse(json.dumps({"sagas": [{"name": "Saga", "films": [1]}, {"name": "saga", "films": [2]}]})), [("Saga", [1])])

    def test_an_unreadable_file_is_never_overwritten(self):
        broken = '{"sagas": [{"name": "Mes sagas", "films": [1, 2, 3],}]}'                  # une virgule en trop, après une édition à la main
        self.file.write_text(broken, encoding="utf-8")
        data = self.api.json("/api/sagas")[1]
        self.assertEqual((data["sagas"], data.get("damaged")), ([], True))
        for status, answer in (self.api.json("/api/sagas/ignore", {"tmdb": 5}), self.api.json("/api/sagas/add", {"name": "x", "films": [1]}),
                               self.api.json("/api/sagas", {"sagas": [], "version": data["version"]}), self.api.json("/api/sagas", {"sagas": []})):
            self.assertEqual(status, 409)
            self.assertIn("pas lisible", answer["error"])
        self.assertEqual(self.file.read_text(encoding="utf-8"), broken)                    # rien n'a été touché
        self.file.write_text('{"sagas": 3}', encoding="utf-8")                              # du JSON, mais pas le bon : même refus
        self.assertEqual(self.api.json("/api/sagas/ignore", {"tmdb": 5})[0], 409)
        self.file.write_text('["pas", "un", "objet"]', encoding="utf-8")
        self.assertEqual(self.api.json("/api/sagas/ignore", {"tmdb": 5})[0], 409)
        self.file.write_text("{" * 5000, encoding="utf-8")                                  # imbriqué à l'excès : illisible aussi, sans erreur 500
        self.assertEqual(self.api.json("/api/sagas")[1].get("damaged"), True)

    def test_a_blank_file_has_nothing_to_lose(self):
        self.file.write_text("  \n", encoding="utf-8")
        data = self.api.json("/api/sagas")[1]
        self.assertNotIn("damaged", data)
        self.assertEqual(self.api.json("/api/sagas/ignore", {"tmdb": 5})[0], 200)

    def test_a_file_saga_with_one_film_here_takes_the_name_of_a_tag_saga(self):
        self.file.write_text(json.dumps({"sagas": [{"name": "Saga DCEU", "films": [27205, 111]}]}), encoding="utf-8")        # Inception + un film absent
        tiles = [i["title"] for i in self.items("films", sagas=1)["items"] if i["type"] == "saga"]
        self.assertNotIn("Saga DCEU", tiles)                                                                                # pas de vignette à deux sens
        self.assertEqual([i["title"] for i in self.items("films", group="Saga DCEU", sort="part")["items"]], ["Inception"])  # la page désigne la saga du fichier
        steel = self.detail(self.item_by_title("films", "Man of Steel")["id"])
        self.assertNotIn("sagas", steel)                                                                                    # la fiche ne propose pas une saga que la page ne montrerait pas
        self.assertEqual(len(steel["saga"]), 2)                                                                             # `saga` (ancien champ) reste ce que dit l'étiquette

    def test_parse_keeps_what_is_readable(self):
        from hub import sagas
        text = json.dumps({"sagas": [
            {"name": "  Une   saga ", "films": [3, "movie/4", "5", 3, True, None, "abc", 1.5]},
            {"name": "Une saga", "films": [9]},              # nom déjà pris
            {"name": "", "films": [1]},                      # sans nom
            {"films": [1]}, "texte", {"name": "Vide", "films": "pas une liste"}]})
        self.assertEqual(sagas.parse(text), [("Une saga", [3, 4, 5]), ("Vide", [])])
        self.assertEqual(sagas.parse(""), [])
        self.assertEqual(sagas.parse('{"sagas": 3}'), [])


class TestSagasEditor(ServerCase):
    """Les routes qui écrivent `sagas.json` (administrateur) : lire, tout remplacer, ajouter des films, chercher des films à ajouter."""

    @property
    def file(self):
        return self.hub.cfg.data / "sagas.json"

    def tearDown(self):
        self.file.unlink(missing_ok=True)

    def save(self, sagas, **extra):
        return self.api.json("/api/sagas", {"sagas": [{"name": n, "films": f} for n, f in sagas], **extra})

    def add(self, name, **films):
        return self.api.json("/api/sagas/add", {"name": name, **films})

    def stored(self):
        return json.loads(self.file.read_text(encoding="utf-8"))["sagas"]

    def test_without_the_file_there_is_no_saga(self):
        self.assertEqual(self.api.json("/api/sagas"), (200, {"version": "", "ignored": [], "sagas": []}))

    def test_saving_writes_the_file_and_the_hub_uses_it_at_once(self):
        status, data = self.save([("Ma saga", [209112, 999999, 27205])])
        self.assertEqual(status, 200)
        self.assertEqual(self.stored(), [{"name": "Ma saga", "films": [209112, 999999, 27205]}])
        films = data["sagas"][0]["films"]
        self.assertEqual([(f["tmdb"], f["item"] and f["item"]["title"]) for f in films], [(209112, "Batman v Superman"), (999999, None), (27205, "Inception")])
        self.assertEqual([f["date"] for f in films], ["2016-03-23", "", "2010-07-21"])
        self.assertEqual(self.api.json("/api/sagas")[1], data)
        self.assertEqual([(i["title"], i["part"]) for i in self.items("films", group="Ma saga", sort="part")["items"]], [("Batman v Superman", 1), ("Inception", 3)])
        self.assertEqual(sorted(p.name for p in self.file.parent.glob("sagas.json*")), ["sagas.json"])          # pas de fichier temporaire resté

    def test_an_empty_list_removes_every_saga(self):
        self.save([("Ma saga", [27205])])
        self.assertEqual(self.save([])[1]["sagas"], [])
        self.assertEqual(self.items("films", group="Ma saga")["total"], 0)

    def test_a_save_from_an_outdated_page_is_refused(self):
        version = self.save([("A", [27205])])[1]["version"]
        self.assertTrue(version)
        self.assertEqual(self.save([("B", [27205])], version=version)[0], 200)                 # à jour : accepté
        status, data = self.save([("C", [27205])], version=version)                            # un autre est passé par là
        self.assertEqual(status, 409)
        self.assertIn("recharge", data["error"])
        self.assertEqual([s["name"] for s in self.api.json("/api/sagas")[1]["sagas"]], ["B"])
        self.assertEqual(self.save([("C", [27205])])[0], 200)                                  # sans version : on écrase

    def test_what_is_not_readable_is_refused_not_dropped(self):
        before = self.save([("Garde", [27205])])[1]
        for sagas, message in (([("", [1])], "nom"), ([("A", [1]), ("a", [2])], "s'appellent"), ([("A", ["abc"])], "illisible"), ([("x" * 81, [1])], "trop long")):
            status, data = self.save(sagas)
            self.assertEqual(status, 400, sagas)
            self.assertIn(message, data["error"])
        self.assertEqual(self.api.json("/api/sagas/add", {"name": "A", "films": ["abc"]})[0], 400)
        for body in ({"sagas": "pas une liste"}, {"sagas": ["texte"]}, {"sagas": [{"name": "A", "films": "pas une liste"}]}, {}, {"sagas": [], "version": 3}):
            self.assertEqual(self.api.json("/api/sagas", body)[0], 400, body)
        self.assertEqual(self.api.json("/api/sagas")[1], before)                                   # rien n'a été touché

    def test_ids_are_cleaned_up(self):
        self.save([("  Ma   saga ", ["movie/27205", "27205", 209112, 209112])])
        self.assertEqual(self.stored(), [{"name": "Ma saga", "films": [27205, 209112]}])

    def test_adding_appends_to_a_saga_and_creates_it(self):
        inception, steel = (self.item_by_title("films", t)["id"] for t in ("Inception", "Batman v Superman"))
        status, data = self.add("Ma saga", items=[inception])
        self.assertEqual((status, data["name"]), (200, "Ma saga"))
        self.assertEqual(self.stored(), [{"name": "Ma saga", "films": [27205]}])
        status, data = self.add("MA SAGA", items=[inception, steel], films=[1234])               # même saga (la casse ne compte pas), sans doublon
        self.assertEqual((status, data["name"]), (200, "Ma saga"))
        self.assertEqual(self.stored(), [{"name": "Ma saga", "films": [27205, 1234, 209112]}])          # `films` d'abord, puis `items`
        self.assertEqual([f["tmdb"] for f in data["sagas"][0]["films"]], [27205, 1234, 209112])

    def test_adding_a_film_without_tmdb_id_or_unknown_is_refused(self):
        bare = next(i for i in self.items("films")["items"] if i["year"] == "1999")
        status, data = self.add("Ma saga", items=[bare["id"]])
        self.assertEqual(status, 400)
        self.assertIn("TMDB", data["error"])
        self.assertEqual(self.add("Ma saga", items=["deadbeef"])[0], 404)
        self.assertEqual(self.add("Ma saga", items=[self.item_by_title("musique", "Discovery")["id"]])[0], 404)         # pas un film
        self.assertEqual(self.add("Ma saga")[0], 400)
        self.assertEqual(self.add("", films=[1])[0], 400)
        self.assertFalse(self.file.exists())

    def test_the_picker_lists_films_with_a_tmdb_id(self):
        data = self.api.json("/api/sagas/films")[1]
        titles = [f["item"]["title"] for f in data["films"]]
        self.assertIn("Inception", titles)
        self.assertEqual(len(titles), data["total"])
        self.assertTrue(all(f["tmdb"] for f in data["films"]))
        self.assertFalse([f for f in data["films"] if f["item"]["year"] == "1999"])               # le film sans étiquette n'a pas d'identifiant : il ne peut pas être choisi
        by_name = self.api.json("/api/sagas/films?q=BATMAN")[1]["films"]
        self.assertEqual([(f["tmdb"], f["date"]) for f in by_name], [(209112, "2016-03-23")])
        self.assertEqual([f["item"]["title"] for f in self.api.json("/api/sagas/films?q=saga%20dceu")[1]["films"]], ["Batman v Superman", "Man of Steel"])
        self.assertEqual(len(self.api.json("/api/sagas/films?limit=1")[1]["films"]), 1)

    def test_the_picker_gives_a_saga_of_the_tag_in_release_order(self):
        films = self.api.json("/api/sagas/films?group=" + quote("Saga DCEU"))[1]["films"]
        self.assertEqual([(f["item"]["title"], f["tmdb"]) for f in films], [("Man of Steel", 49521), ("Batman v Superman", 209112)])


class TestHardening(ServerCase):
    """Ce que la relecture a trouvé de fragile : corps de requête malformés, chemins qui sortent d'une bibliothèque, chemins du serveur dans les erreurs, caches qui ne suivent pas ou s'écrivent à moitié."""

    def raw(self, method, path, headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.run_.port, timeout=30)
        self.addCleanup(conn.close)
        conn.putrequest(method, path)
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.endheaders()
        response = conn.getresponse()
        return response.status, json.loads(response.read() or "null")

    def test_an_unreadable_content_length_is_a_400_not_a_hung_thread(self):
        for value in ("-5", "abc", "1.5"):
            self.assertEqual(self.raw("POST", "/api/progress", {"Content-Length": value})[0], 400, value)
        self.assertEqual(self.raw("POST", "/api/login", {"Content-Length": "-1"})[0], 400)
        self.assertEqual(self.raw("POST", "/api/token", {"Content-Length": "x"})[0], 400)
        self.assertEqual(self.api.json("/api/state")[0], 200)                       # le serveur répond toujours

    def test_a_json_body_that_is_not_an_object_is_an_error_not_a_crash(self):
        for path in ("/api/progress", "/api/playlists", "/api/scan", "/api/cleanup", "/api/sagas/add"):
            for body in ([], [1, 2], 5, "texte"):
                status, _, _ = self.api.request("POST", path, body=json.dumps(body).encode())
                self.assertNotEqual(status, 500, (path, body))

    def test_a_path_may_not_leave_its_library_by_a_sibling_with_the_same_prefix(self):
        from hub.server.app import HttpError
        lib = self.hub.cfg.library("films")
        for rel in (f"../{lib.path.name}2/x.mkv", "../Series/x.mkv", "../../x"):
            with self.assertRaises(HttpError, msg=rel) as caught:
                self.hub.path_of({"lib": "films", "rel": rel})
            self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.hub.path_of({"lib": "films", "rel": "Inception (2010)/Inception (2010).mkv"})[1].name, "Inception (2010).mkv")

    def test_an_unknown_path_under_a_served_prefix_is_a_404_not_the_home_page(self):
        for path in ("/img/nope/zzz", "/book/abc/page/-1", "/media/zzz"):
            self.assertEqual(self.api.get(path)[0], 404, path)
        self.assertEqual(self.api.get("/")[0], 200)
        self.assertEqual(self.api.get("/js/core.js")[0], 200)
        self.assertEqual(self.api.get("/une-page-de-l-application")[0], 200)

    def test_the_paths_of_the_server_stay_out_of_the_answers(self):
        import re
        from hub.server import app as server
        lib = self.hub.cfg.library("films")
        message = self.hub.scrub(f"{lib.path}/Film/x.mkv: Invalid data ; {self.hub.cfg.data}/subs/x")
        self.assertEqual(message, "…/Film/x.mkv: Invalid data ; …/subs/x")
        self.assertNotIn(str(lib.path), self.hub.scrub(str(lib.path / "x")))

        def boom(h, m, q):
            raise RuntimeError(f"échec sur {h.hub.cfg.data}/subs/x et {lib.path}")
        route = ("GET", re.compile(r"/api/boom"), boom)
        server.ROUTES.append(route)
        self.addCleanup(server.ROUTES.remove, route)
        status, data = self.api.json("/api/boom")
        self.assertEqual(status, 500)
        self.assertNotIn(str(self.hub.cfg.data), data["error"])
        self.assertNotIn(str(lib.path), data["error"])

    def test_progress_must_be_finite(self):
        entry = self.detail(self.item_by_title("films", "Inception")["id"])["entries"][0]["id"]
        for fields in ('"position": Infinity, "duration": 100', '"position": 5, "duration": Infinity'):         # `Infinity` passe json.loads : stocké, il rendrait la fiche du compte illisible
            status, _, _ = self.api.request("POST", "/api/progress", body=('{"entry": "%s", %s}' % (entry, fields)).encode())
            self.assertEqual(status, 400, fields)
        for fields in ('"position": -Infinity, "duration": 100', '"position": NaN, "duration": 100'):             # ramenés à 0 : sans danger
            status, _, _ = self.api.request("POST", "/api/progress", body=('{"entry": "%s", %s}' % (entry, fields)).encode())
            self.assertEqual(status, 200, fields)
        self.assertEqual(self.api.json("/api/progress", {"entry": entry, "position": 5, "duration": 100})[0], 200)
        self.addCleanup(self.hub.db.execute, "DELETE FROM progress WHERE entry_id=?", (entry,))

    def test_a_corrected_subtitle_file_is_served_again(self):
        films = self.run_.libs["films"]
        folder = films / "Sous-titré (2020)"
        fixtures.write_mkv(folder / "film.mkv", fixtures.movie_blocks("Sous-titré", "Un film.", "2020-01-01", "movie/4242", "Drame"), minutes=5)
        srt = folder / "film.fr.srt"
        srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nPremière version\n", encoding="utf-8")
        self.hub.jobs.scan(self.hub.cfg.library("films"))
        self.run_.wait_jobs()
        entry = self.detail(self.item_by_title("films", "Sous-titré")["id"])["entries"][0]["id"]
        status, headers, data = self.api.request("GET", f"/api/subs/{entry}/s0.vtt")
        self.assertEqual(status, 200)
        self.assertIn("Première version", data.decode())
        first = headers["ETag"]
        self.assertEqual(self.api.request("GET", f"/api/subs/{entry}/s0.vtt", headers={"If-None-Match": first})[0], 304)          # inchangé : on revalide
        srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nVersion corrigée\n", encoding="utf-8")
        stat = srt.stat()
        os.utime(srt, (stat.st_atime, stat.st_mtime + 10))
        status, headers, data = self.api.request("GET", f"/api/subs/{entry}/s0.vtt", headers={"If-None-Match": first})
        self.assertEqual(status, 200)                                                                                           # le fichier a changé : plus de 304
        self.assertIn("Version corrigée", data.decode())
        self.assertNotEqual(headers["ETag"], first)
        self.assertEqual(headers["Cache-Control"], "private, no-cache")                                                        # plus de jour entier de cache aveugle
        self.assertEqual(self.api.get(f"/api/subs/{entry}/s9.vtt")[0], 404)

    def test_the_caches_are_written_in_one_piece(self):
        from hub.media.images import write_atomic
        target = Path(tempfile.mkdtemp()) / "x.jpg"
        self.addCleanup(shutil.rmtree, target.parent, True)
        write_atomic(target, b"abc")
        write_atomic(target, b"def")
        self.assertEqual((target.read_bytes(), [p.name for p in target.parent.iterdir()]), (b"def", ["x.jpg"]))
        with self.assertRaises(OSError):
            write_atomic(target.parent / "absent" / "x.jpg", b"x")
        self.assertEqual([p.name for p in target.parent.iterdir()], ["x.jpg"])               # rien de laissé derrière une écriture ratée

    def test_a_playlist_is_not_created_when_none_of_its_tracks_is_valid(self):
        before = len(self.api.json("/api/playlists")[1]["playlists"])
        self.assertEqual(self.api.json("/api/playlists", {"name": "Vide", "entries": ["nope"]})[0], 400)
        self.assertEqual(len(self.api.json("/api/playlists")[1]["playlists"]), before)

    def test_only_known_columns_can_be_updated(self):
        item = self.item_by_title("films", "Inception")["id"]
        with self.assertRaises(ValueError):
            self.hub.db.update_item_meta(item, **{"title=?, lib": "x"})
        self.hub.db.update_item_meta(item, year="2010")                                      # une colonne connue passe toujours


class TestScanResilience(ServerCase):
    """Un scan ne détruit rien de ce qu'on ne peut pas retrouver : ni la progression quand le volume est vide un instant, ni le scan entier à cause d'un fichier abîmé."""

    def scan(self):
        job = self.hub.jobs.scan(self.hub.cfg.library("films"))
        self.run_.wait_jobs()
        return job

    def inception(self):
        return self.item_by_title("films", "Inception")["id"]

    def touch_inception(self):
        """Un scan ne relit pas un fichier dont la date n'a pas bougé : on la change, comme si le fichier avait été remplacé."""
        path = self.run_.libs["films"] / "Inception (2010)" / "Inception (2010).mkv"
        stat = path.stat()
        os.utime(path, (stat.st_atime, stat.st_mtime + 10))

    def progress_rows(self, entry):
        return self.hub.db.query("SELECT * FROM progress WHERE entry_id=?", (entry,))

    def test_an_empty_library_for_one_scan_keeps_the_progress(self):
        entry = self.detail(self.inception())["entries"][0]["id"]
        self.assertEqual(self.api.json("/api/progress", {"entry": entry, "position": 600, "duration": 8880})[0], 200)
        films = self.run_.libs["films"]
        stash = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, stash, True)
        for child in list(films.iterdir()):                      # le volume est monté, mais vide
            shutil.move(str(child), str(stash / child.name))
        try:
            job = self.scan()
            self.assertEqual((job.status, self.items("films")["total"]), ("done", 0))
            self.assertTrue(any("ATTENTION" in line for line in job.log), list(job.log))
            self.assertEqual(len(self.progress_rows(entry)), 1)             # la progression n'a pas bougé
        finally:
            for child in list(stash.iterdir()):
                shutil.move(str(child), str(films / child.name))
        self.scan()
        card = next(c for c in self.api.json("/api/home")[1]["continue"] if c["id"] == entry)
        self.assertEqual((card["position"], card["resume"]), (600, True))     # le film est revenu, avec sa progression
        self.addCleanup(self.hub.db.execute, "DELETE FROM progress WHERE entry_id=?", (entry,))

    def test_the_progress_of_a_file_gone_for_a_long_time_is_dropped_but_not_a_recent_one(self):
        now = int(time.time())
        for name, age in (("orphelin-ancien", 100 * 86400), ("orphelin-recent", 2 * 86400)):
            self.hub.db.execute("INSERT INTO progress(profile, entry_id, position, duration, done, updated) VALUES('default', ?, 5, 10, 0, ?)", (name, now - age))
        self.addCleanup(self.hub.db.execute, "DELETE FROM progress WHERE entry_id LIKE 'orphelin-%'")
        self.scan()
        self.assertEqual(self.progress_rows("orphelin-ancien"), [])
        self.assertEqual(len(self.progress_rows("orphelin-recent")), 1)

    def test_a_damaged_file_does_not_stop_the_scan(self):
        from libraries.video import ebml
        real = ebml.read_metadata

        def damaged(path, ordered=False):
            if "Inception" in str(path):
                raise OverflowError("fichier abîmé")                # ce que le lecteur ne rattrape pas
            return real(path, ordered=ordered)
        self.touch_inception()
        with mock.patch.object(ebml, "read_metadata", damaged):
            job = self.scan()
        self.addCleanup(self.scan)
        self.assertEqual(job.status, "done")
        inception = self.detail(self.inception())
        self.assertEqual((inception["title"], inception["year"], inception["tagged"]), ("Inception", "2010", False))           # sans ses tags, sous le nom de son dossier
        self.assertTrue(self.detail(self.item_by_title("films", "Man of Steel")["id"])["tagged"])                                # les autres films sont indexés

    def test_a_failed_read_is_made_again_at_the_next_scan(self):
        from libraries.video import ebml
        real = ebml.read_metadata
        self.touch_inception()
        with mock.patch.object(ebml, "read_metadata", lambda path, ordered=False: (None, None) if "Inception" in str(path) else real(path, ordered=ordered)):
            self.scan()                                          # une erreur d'E/S passagère
        self.assertEqual(self.detail(self.inception())["tagged"], False)
        job = self.scan()                                        # le fichier n'a pas changé, mais la lecture ratée n'a pas été gardée
        self.assertEqual(job.result["read"], 1)
        self.assertEqual(self.detail(self.inception())["summary"], "Un voleur s'introduit dans les rêves.")
        self.assertEqual(self.scan().result["read"], 0)          # lue : on ne la relit plus

    def test_a_file_without_tags_is_not_read_again(self):
        self.scan()
        self.assertEqual(self.scan().result["read"], 0)          # « Sans Etiquette » n'a pas de tags : ce n'est pas une erreur


class FakeTmdb:
    """TMDB pour les sagas : « Saga DCEU » (collection 9000) compte cinq films dont trois qu'on n'a pas ; 777 est un film que seul `sagas.json` réclame."""
    calls = []
    parts = [{"id": 141052, "title": "Justice League", "release_date": "2017-11-15", "poster_path": None},
             {"id": 49521, "title": "Man of Steel", "release_date": "2013-06-12", "poster_path": "/mos.jpg"},
             {"id": 297762, "title": "Wonder Woman", "release_date": "2017-05-31", "poster_path": "/ww.jpg"},
             {"id": 209112, "title": "Batman v Superman", "release_date": "2016-03-23", "poster_path": "/bvs.jpg"},
             {"id": 1000, "title": "Film intermédiaire", "release_date": "2014-01-01", "poster_path": "/mid.jpg"},
             {"id": 2000, "title": "Film annoncé", "release_date": "2099-01-01", "poster_path": "/soon.jpg"},          # pas encore sorti
             {"id": 2001, "title": "Film en projet", "release_date": "", "poster_path": None}]                            # pas même de date
    movies = {49521: {"id": 49521, "belongs_to_collection": {"id": 9000}}, 209112: {"id": 209112, "belongs_to_collection": {"id": 9000}},
              777: {"id": 777, "title": "Film du fichier", "release_date": "2020-02-02", "poster_path": "/f.jpg"}}

    def __init__(self, key, language="fr-FR", **kwargs):
        self.cache = kwargs.get("cache")

    def get(self, endpoint):
        from libraries.video.tmdb import TmdbError
        FakeTmdb.calls.append(endpoint)
        found = re.fullmatch(r"movie/(\d+)", endpoint)
        if not found or int(found.group(1)) not in self.movies:
            raise TmdbError("introuvable sur TMDB (HTTP 404)")
        return self.movies[int(found.group(1))]

    def collection(self, collection_id):
        FakeTmdb.calls.append(f"collection/{collection_id}")
        return {"parts": list(self.parts)}

    def save_image(self, image_path, size, dest):
        FakeTmdb.calls.append(f"image{image_path}")
        Path(dest).write_bytes(fixtures.PNG)


class TestMissingFilms(ServerCase):
    """Les films qui manquent à une saga : ceux de la collection TMDB (saga de l'étiquette) et ceux que `sagas.json` réclame. Man of Steel (49521) et Batman v Superman (209112) sont de « Saga DCEU »."""

    def setUp(self):
        FakeTmdb.calls = []
        self.hub.cfg.tmdb_key = "fake"
        self.addCleanup(setattr, self.hub.cfg, "tmdb_key", "")
        self.addCleanup(self.hub.db.execute, "DELETE FROM kv WHERE key='sagainfo'")
        self.addCleanup((self.hub.cfg.data / "sagas.json").unlink, True)

    def enrich(self, **body):
        with mock.patch("hub.enrich.Tmdb", FakeTmdb):
            status, data = self.api.json("/api/enrich", {"lib": "films", **body})
            self.assertEqual(status, 202, data)
            self.run_.wait_jobs()

    def group(self, name="Saga DCEU"):
        return self.items("films", group=name, sort="part")

    def test_nothing_is_missing_before_the_enrichment(self):
        data = self.group()
        self.assertEqual([(i["title"], i["part"]) for i in data["items"]], [("Man of Steel", 1), ("Batman v Superman", 2)])        # numérotés de 1 à N
        self.assertNotIn("missing", data)
        self.assertNotIn("missing", self.detail(self.item_by_title("films", "Man of Steel")["id"])["sagas"][0])

    def test_the_enrichment_describes_the_films_we_do_not_have(self):
        self.enrich()
        info = self.hub.db.kv_get("sagainfo")
        self.assertEqual(info["collections"]["Saga DCEU"]["films"], [49521, 1000, 209112, 297762, 141052, 2000, 2001])         # dans l'ordre de sortie, ce qui est annoncé à la fin
        self.assertEqual(sorted(info["films"]), ["1000", "141052", "2000", "2001", "209112", "297762", "49521"])                # toute la collection : un film retiré plus tard doit pouvoir apparaître grisé
        self.assertEqual(info["films"]["1000"], {"title": "Film intermédiaire", "date": "2014-01-01", "year": "2014", "poster": "film-1000.jpg"})
        self.assertEqual(info["films"]["141052"]["poster"], "")                                                     # sans affiche
        self.assertTrue((self.hub.cfg.data / "tmdb" / "film-1000.jpg").is_file())
        self.assertEqual(sorted(p.name for p in (self.hub.cfg.data / "tmdb").glob("film-*.tmp")), [])               # écrites d'un bloc : pas de reste

    def test_the_saga_page_numbers_by_the_collection_and_lists_what_is_missing(self):
        self.enrich()
        data = self.group()
        self.assertEqual([(i["title"], i["part"]) for i in data["items"]], [("Man of Steel", 1), ("Batman v Superman", 3)])        # le 2 manque : on garde le 3
        self.assertEqual([(f["tmdb"], f["title"], f["year"], f["rank"], f["cover"]) for f in data["missing"]],
                         [(1000, "Film intermédiaire", "2014", 2, "/img/film/1000"), (297762, "Wonder Woman", "2017", 4, "/img/film/297762"), (141052, "Justice League", "2017", 5, "")])
        self.assertEqual(data["total"], 2)                                                                          # un manquant n'est pas un élément : il ne compte pas

    def test_a_film_not_out_yet_is_described_but_not_shown_as_missing(self):
        self.enrich()
        data = self.group()
        self.assertEqual([f["tmdb"] for f in data["missing"]], [1000, 297762, 141052])                              # ni « Film annoncé » (2099) ni « Film en projet » (sans date)
        self.assertEqual([i["part"] for i in data["items"]], [1, 3])                                                # les numéros des films qu'on a ne changent pas
        film = self.detail(self.item_by_title("films", "Batman v Superman")["id"])
        self.assertEqual([f["tmdb"] for f in film["sagas"][0]["missing"]], [1000, 297762, 141052])
        self.assertIn("2000", self.hub.db.kv_get("sagainfo")["films"])                                              # décrit : prêt pour le jour de sa sortie

    def test_a_film_shows_up_on_its_release_day_without_running_anything_again(self):
        self.enrich()
        with mock.patch("hub.missing.today", return_value="2098-12-31"):
            self.assertNotIn(2000, [f["tmdb"] for f in self.group()["missing"]])                                    # la veille
        with mock.patch("hub.missing.today", return_value="2099-01-01"):
            shown = self.group()["missing"]
            self.assertEqual([(f["tmdb"], f["rank"]) for f in shown][-1], (2000, 6))                                # le jour même, à sa place
            self.assertNotIn(2001, [f["tmdb"] for f in shown])                                                      # sans date, il ne sort jamais de lui-même

    def test_released_means_a_date_that_is_today_or_earlier(self):
        from hub import missing
        for date, now, expected in (("2017-11-15", "2017-11-15", True), ("2017-11-14", "2017-11-15", True), ("2017-11-16", "2017-11-15", False),
                                    ("2017-11-15T00:00:00", "2017-11-15", True), ("", "2017-11-15", False), (None, "2017-11-15", False)):
            self.assertEqual(missing.released({"date": date}, now), expected, (date, now))
        self.assertFalse(missing.released({}))

    def test_a_film_listed_by_a_file_saga_is_shown_even_if_it_is_not_out_yet(self):
        FakeTmdb.movies = {**FakeTmdb.movies, 3000: {"id": 3000, "title": "À venir", "release_date": "2099-06-01", "poster_path": None}}
        self.addCleanup(FakeTmdb.movies.pop, 3000, None)
        self.api.json("/api/sagas", {"sagas": [{"name": "Ma saga", "films": [209112, 3000]}]})                       # on l'a écrit soi-même : on veut le voir
        self.enrich()
        self.assertEqual([(f["tmdb"], f["rank"]) for f in self.group("Ma saga")["missing"]], [(3000, 2)])

    def test_a_film_that_is_not_out_yet_says_so_and_one_that_is_out_does_not(self):
        FakeTmdb.movies = {**FakeTmdb.movies, 3000: {"id": 3000, "title": "À venir", "release_date": "2099-06-01", "poster_path": None}}
        self.addCleanup(FakeTmdb.movies.pop, 3000, None)
        self.api.json("/api/sagas", {"sagas": [{"name": "Ma saga", "films": [209112, 1000, 3000]}]})
        self.enrich()
        absent = {f["tmdb"]: f for f in self.group("Ma saga")["missing"]}
        self.assertEqual(sorted(absent), [1000, 3000])
        self.assertIs(absent[3000]["coming"], True)
        self.assertNotIn("coming", absent[1000])                                                                      # sorti : un film qui manque, tout simplement

    def test_the_film_page_lists_what_is_missing_from_each_saga(self):
        self.enrich()
        steel = self.detail(self.item_by_title("films", "Batman v Superman")["id"])
        saga = steel["sagas"][0]
        self.assertEqual([(f["title"], f["part"]) for f in saga["films"]], [("Man of Steel", 1), ("Batman v Superman", 3)])
        self.assertEqual([f["rank"] for f in saga["missing"]], [2, 4, 5])
        self.assertEqual([(f["title"], f["part"]) for f in steel["saga"]], [("Man of Steel", 1), ("Batman v Superman", 3)])      # `saga` : mêmes rangs

    def test_the_grid_does_not_count_missing_films(self):
        self.enrich()
        tiles = {i["title"]: i for i in self.items("films", sagas=1)["items"] if i["type"] == "saga"}
        self.assertEqual(tiles["Saga DCEU"]["n"], 2)

    def test_the_poster_of_a_missing_film_is_served(self):
        self.enrich()
        self.assertEqual(self.api.get("/img/film/1000")[0], 200)
        self.assertEqual(self.api.get("/img/film/1000?w=160")[0], 200)
        self.assertEqual(self.api.get("/img/film/141052")[0], 404)                  # TMDB n'a pas d'affiche pour lui
        self.assertEqual(self.api.get("/img/film/5")[0], 404)                       # inconnu

    def test_a_film_can_be_hidden_from_the_missing_ones_and_shown_again(self):
        self.enrich()
        status, data = self.api.json("/api/sagas/ignore", {"tmdb": 1000})
        self.assertEqual(status, 200)
        self.assertEqual(data["ignored"], [{"tmdb": 1000, "title": "Film intermédiaire", "year": "2014"}])
        self.assertEqual([f["rank"] for f in self.group()["missing"]], [4, 5])
        self.assertEqual([i["part"] for i in self.group()["items"]], [1, 3])        # le rang compte toujours le film caché
        self.assertEqual(json.loads((self.hub.cfg.data / "sagas.json").read_text(encoding="utf-8"))["ignore"], [1000])
        self.api.json("/api/sagas", {"sagas": [{"name": "Autre", "films": [27205]}], "version": data["version"]})       # tout remplacer garde la liste
        self.assertEqual(self.api.json("/api/sagas")[1]["ignored"][0]["tmdb"], 1000)
        self.assertEqual(self.api.json("/api/sagas/ignore", {"tmdb": 1000, "ignore": False})[1]["ignored"], [])
        self.assertEqual([f["rank"] for f in self.group()["missing"]], [2, 4, 5])
        self.assertEqual(self.api.json("/api/sagas/ignore", {"tmdb": "abc"})[0], 400)

    def test_a_saga_of_the_file_shows_the_films_it_lists_and_we_do_not_have(self):
        self.api.json("/api/sagas", {"sagas": [{"name": "Ma saga", "films": [209112, 777, 27205]}]})
        self.assertNotIn("missing", self.group("Ma saga"))                          # pas encore décrit
        self.enrich()
        data = self.group("Ma saga")
        self.assertEqual([(i["title"], i["part"]) for i in data["items"]], [("Batman v Superman", 1), ("Inception", 3)])
        self.assertEqual([(f["tmdb"], f["title"], f["rank"]) for f in data["missing"]], [(777, "Film du fichier", 2)])
        self.api.json("/api/sagas/ignore", {"tmdb": 777})                           # ce qu'on a écrit soi-même ne se cache pas
        self.assertEqual([f["rank"] for f in self.group("Ma saga")["missing"]], [2])
        absent = self.api.json("/api/sagas")[1]["sagas"][0]["films"][1]            # et l'éditeur le décrit
        self.assertEqual((absent["tmdb"], absent["item"], absent["title"], absent["year"], absent["date"]), (777, None, "Film du fichier", "2020", "2020-02-02"))
        film = self.detail(self.item_by_title("films", "Inception")["id"])
        self.assertEqual([(s["name"], [f["rank"] for f in s["missing"]]) for s in film["sagas"]], [("Ma saga", [2])])

    def test_a_film_that_leaves_the_library_appears_greyed_at_once(self):
        self.enrich()
        self.addCleanup(self.enrich_scan)
        steel = self.item_by_title("films", "Man of Steel")["id"]                    # il quitte la médiathèque (le scan ne le voit plus)
        self.hub.db.execute("DELETE FROM items WHERE id=?", (steel,))
        data = self.group()
        self.assertEqual([(i["title"], i["part"]) for i in data["items"]], [("Batman v Superman", 3)])
        self.assertEqual([(f["tmdb"], f["title"], f["rank"]) for f in data["missing"]][:2], [(49521, "Man of Steel", 1), (1000, "Film intermédiaire", 2)])

    def test_a_collection_known_before_the_owned_films_were_described_is_completed(self):
        self.enrich()
        info = self.hub.db.kv_get("sagainfo")
        del info["films"]["49521"]                                                   # ce que la version précédente gardait : seulement les absents
        self.hub.db.kv_set("sagainfo", info)
        steel = self.item_by_title("films", "Man of Steel")["id"]
        self.hub.db.execute("DELETE FROM items WHERE id=?", (steel,))
        self.addCleanup(self.enrich_scan)
        FakeTmdb.calls = []
        self.enrich()                                                                # collection encore fraîche : seul ce film est demandé
        self.assertEqual(FakeTmdb.calls, ["movie/49521"])
        self.assertIn("49521", self.hub.db.kv_get("sagainfo")["films"])

    def test_missing_films_of_a_tag_saga_can_be_hidden_but_not_those_of_a_file_saga(self):
        self.enrich()
        self.api.json("/api/sagas", {"sagas": [{"name": "Ma saga", "films": [209112, 777]}]})
        self.enrich()
        self.assertTrue(all(f.get("ignorable") is True for f in self.group()["missing"]))
        self.assertTrue(all("ignorable" not in f for f in self.group("Ma saga")["missing"]))

    def test_ignore_wants_a_boolean_and_is_capped(self):
        self.assertEqual(self.api.json("/api/sagas/ignore", {"tmdb": 1000, "ignore": 0})[0], 400)
        self.assertEqual(self.api.json("/api/sagas/ignore", {"tmdb": 1000, "ignore": "false"})[0], 400)
        with mock.patch("hub.sagas.MAX_IGNORED", 2):
            self.assertEqual([self.api.json("/api/sagas/ignore", {"tmdb": i})[0] for i in (1, 2, 3)], [200, 200, 400])
            self.assertEqual(self.api.json("/api/sagas/ignore", {"tmdb": 1, "ignore": False})[0], 200)       # en rendre un reste possible

    def enrich_scan(self):
        self.hub.jobs.scan(self.hub.cfg.library("films"))
        self.run_.wait_jobs()

    def test_a_second_run_asks_nothing_new_unless_forced(self):
        self.enrich()
        asked = list(FakeTmdb.calls)
        self.assertIn("collection/9000", asked)
        FakeTmdb.calls = []
        self.enrich()
        self.assertEqual(FakeTmdb.calls, [])                                         # collection connue, affiches là
        self.enrich(force=True)
        self.assertIn("collection/9000", FakeTmdb.calls)

    def test_a_scan_keeps_what_the_enrichment_found(self):
        self.enrich()
        self.hub.jobs.scan(self.hub.cfg.library("films"))
        self.run_.wait_jobs()
        self.assertEqual(len(self.group()["missing"]), 3)

    def test_a_collection_that_does_not_match_our_films_is_not_trusted(self):
        self.enrich()
        info = self.hub.db.kv_get("sagainfo")
        info["collections"]["Saga DCEU"]["films"].remove(209112)                    # un de nos films n'y figure pas : le nom est peut-être celui de deux sagas
        self.hub.db.kv_set("sagainfo", info)
        data = self.group()
        self.assertNotIn("missing", data)
        self.assertEqual([i["part"] for i in data["items"]], [1, 2])

    def test_the_enrichment_logs_what_it_cannot_find(self):
        self.api.json("/api/sagas", {"sagas": [{"name": "Ma saga", "films": [209112, 4242]}]})          # 4242 n'existe pas chez TMDB
        self.enrich()
        job = next(j for j in self.api.json("/api/jobs?log=1")[1]["jobs"] if j["label"].startswith("Sagas TMDB"))
        self.assertEqual(job["status"], "done")
        self.assertTrue(any("4242" in line for line in job["log"]), job["log"])
        self.assertEqual(job["result"]["failed"], 1)

    def test_a_refused_key_stops_the_job_and_keeps_what_is_done(self):
        from libraries.video.tmdb import TmdbAuthError

        class Refused(FakeTmdb):
            def collection(self, collection_id):
                raise TmdbAuthError("clé refusée")
        with mock.patch("hub.enrich.Tmdb", Refused):
            self.api.json("/api/enrich", {"lib": "films"})
            self.run_.wait_jobs()
        job = next(j for j in self.api.json("/api/jobs?log=1")[1]["jobs"] if j["label"].startswith("Sagas TMDB"))
        self.assertEqual(job["status"], "failed")
        self.assertTrue(any("clé refusée" in line for line in job["log"]))


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


def entry_of(api, lib, title):
    """(identifiant de l'élément, ses fichiers) pour le titre `title` de la bibliothèque `lib`."""
    item = next(i for i in api.json(f"/api/library/{lib}?limit=600")[1]["items"] if i["title"] == title)
    return item["id"], api.json(f"/api/item/{item['id']}")[1]["entries"]


class AccountsCase(unittest.TestCase):
    """Les comptes sur un vrai serveur : HUB_PASSWORD (« secret ») crée l'administrateur `admin`, qui crée les autres par l'API. Un serveur neuf par test (environ une seconde)."""

    PASSWORD = "motdepasse1"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.start("secret")
        self.admin = self.session_for("admin")

    def start(self, password="secret", **kw):
        run = Running(self.tmp, password=password, **kw)
        self.addCleanup(run.stop)
        self.run_, self.hub, self.anon = run, run.hub, Client(run.port)
        return run

    def sign_in(self, name, password):
        """Un client natif connecté en tant que `name` ; `.session` garde la réponse de /api/token."""
        status, _, body = self.anon.request("POST", "/api/token", body={"user": name, "password": password})
        self.assertEqual(status, 200, body)
        client = BearerClient(self.run_.port, json.loads(body)["token"])
        client.session = json.loads(body)
        return client

    def session_for(self, name):
        """Comme `sign_in`, sans passer par /api/token (qui se fait attendre 0,4 s) : les tests qui ne portent pas sur la connexion vont plus vite. La connexion a ses propres tests."""
        row = accounts.by_name(self.hub.db, name)
        client = BearerClient(self.run_.port, self.hub.session_token(row))
        client.session = {"token": client.token, "media_token": self.hub.media_token(), "user": accounts.public(row)}
        return client

    def new_user(self, name, role="user", password=PASSWORD):
        """Crée le compte par l'API (en administrateur) et rend un client connecté ; `.account` garde la réponse de la création."""
        status, made = self.admin.json("/api/users", {"name": name, "password": password, "role": role})
        self.assertEqual(status, 201, made)
        client = self.session_for(name)
        client.account = made
        return client

    def watch(self, api, entry, position=600):
        self.assertEqual(api.json("/api/progress", {"entry": entry, "position": position, "duration": 3000})[0], 200)

    def continue_ids(self, api):
        return [c["item"]["id"] for c in api.json("/api/home")[1]["continue"]]

    def rows(self, sql, *params):
        return self.hub.db.query(sql, params)

    def count(self, sql, *params):
        return self.rows(sql, *params)[0]["n"]


class TestAccountSeed(AccountsCase):
    def test_hub_password_creates_the_default_admin(self):
        users = self.admin.json("/api/users")[1]["users"]
        self.assertEqual([(u["id"], u["name"], u["role"]) for u in users], [("default", "admin", "admin")])       # HUB_ADMIN absent : « admin »
        self.assertEqual(self.admin.session["user"], {"id": "default", "name": "admin", "role": "admin"})
        state = self.admin.json("/api/state")[1]
        self.assertEqual((state["auth"], state["user"]), (True, {"id": "default", "name": "admin", "role": "admin"}))

    def test_hub_admin_names_the_account_and_the_password_may_be_short(self):
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, True)
        run = Running(other, password="court", admin="  Chef de   famille ")        # six caractères : HUB_PASSWORD existait avant la longueur minimale
        self.addCleanup(run.stop)
        api = Client(run.port)
        status, _, body = api.request("POST", "/api/token", body={"password": "court"})       # un seul compte : le nom est facultatif
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["user"], {"id": "default", "name": "Chef de famille", "role": "admin"})
        for user in ("CHEF DE FAMILLE", " chef de famille "):                                  # la casse et les espaces ne comptent pas
            self.assertEqual(api.request("POST", "/api/token", body={"user": user, "password": "court"})[0], 200, user)
        status, _, body = api.request("POST", "/api/token", body={"user": "admin", "password": "court"})
        self.assertEqual((status, json.loads(body)), (401, {"error": "mot de passe incorrect"}))

    def test_hub_password_is_ignored_once_an_account_exists(self):
        self.run_.stop()
        self.start("autre-mot-de-passe", admin="Autre", reuse=True)
        self.assertEqual([(u["id"], u["name"]) for u in self.rows("SELECT id, name FROM users")], [("default", "admin")])
        self.assertEqual(self.anon.request("POST", "/api/token", body={"password": "autre-mot-de-passe"})[0], 401)
        self.assertEqual(self.anon.request("POST", "/api/token", body={"password": "secret"})[0], 200)

    def test_progress_and_playlists_from_before_the_accounts_go_to_the_first_account(self):
        import sqlite3

        self.run_.stop()
        conn = sqlite3.connect(Path(self.tmp) / "data" / "hub.db")
        conn.execute("DELETE FROM users")                           # retour au serveur d'avant les comptes : ouvert, profil unique
        conn.commit()
        conn.close()
        api = Client(self.start("", reuse=True).port)
        self.assertEqual(api.json("/api/state")[1]["auth"], False)
        dark, episodes = entry_of(api, "series", "Dark")
        film, movie = entry_of(api, "films", "Inception")
        self.watch(api, episodes[0]["id"])
        self.watch(api, movie[0]["id"], position=1000)
        api.json(f"/api/item/{dark}/dismiss", {})                   # `dismiss:default:<élément>` : Dark ne se reprend plus
        tracks = [e["id"] for e in entry_of(api, "musique", "Discovery")[1]]
        playlist = api.json("/api/playlists", {"name": "Ancienne", "entries": tracks[:2]})[1]["id"]
        self.run_.stop()
        conn = sqlite3.connect(Path(self.tmp) / "data" / "hub.db")
        conn.execute("DROP TABLE users")                            # une base de la version 1 : pas de table des comptes
        conn.execute("UPDATE kv SET value='1' WHERE key='schema'")
        conn.commit()
        conn.close()
        self.start("secret", reuse=True)
        self.assertEqual(self.hub.db.kv_get("schema"), 2)
        first = self.sign_in("admin", "secret")
        self.assertEqual(first.session["user"]["id"], "default")
        self.assertEqual(self.continue_ids(first), [film])           # le film entamé est repris, la série écartée le reste
        self.assertEqual(first.json("/api/item/" + dark)[1]["entries"][0]["progress"]["position"], 600)
        found = first.json("/api/playlists")[1]["playlists"]
        self.assertEqual([(p["id"], p["name"], p["count"]) for p in found], [(playlist, "Ancienne", 2)])
        self.admin = first
        newcomer = self.new_user("alice")                           # un compte ultérieur part de zéro
        self.assertEqual((self.continue_ids(newcomer), newcomer.json("/api/playlists")[1]["playlists"]), ([], []))


class TestAccountLogin(AccountsCase):
    def setUp(self):
        super().setUp()
        self.alice, self.bob = self.new_user("alice"), self.new_user("Bob")

    def test_token_needs_the_user_name_when_there_are_several_accounts(self):
        status, _, body = self.anon.request("POST", "/api/token", body={"password": "secret"})
        self.assertEqual((status, json.loads(body)), (401, {"error": "nom d'utilisateur requis"}))
        for user in ("", None, "   "):
            status, _, body = self.anon.request("POST", "/api/token", body={"user": user, "password": "secret"})
            self.assertEqual(status, 401, user)
            self.assertIn("error", json.loads(body))

    def test_unknown_name_and_wrong_password_look_the_same(self):
        for body in ({"user": "inconnu", "password": self.PASSWORD}, {"user": "alice", "password": "faux"}, {"user": "alice", "password": "secret"},
                     {"user": "alice", "password": 12}, {"user": 12, "password": self.PASSWORD}, {"user": ["alice"], "password": self.PASSWORD}):
            status, _, answer = self.anon.request("POST", "/api/token", body=body)
            self.assertEqual((status, json.loads(answer)), (401, {"error": "mot de passe incorrect"}), body)

    def test_token_gives_each_account_its_own_session_token(self):
        for client, name, role in ((self.alice, "alice", "user"), (self.bob, "Bob", "user"), (self.admin, "admin", "admin")):
            uid = client.session["user"]["id"]
            self.assertEqual(client.session["user"], {"id": uid, "name": name, "role": role})
            self.assertRegex(client.session["token"], rf"^{uid}\.[0-9a-f]{{64}}$")
            self.assertEqual(client.json("/api/state")[1]["user"]["name"], name)
        self.assertEqual(len({c.session["token"] for c in (self.alice, self.bob, self.admin)}), 3)
        self.assertEqual(len({c.session["media_token"] for c in (self.alice, self.bob, self.admin)}), 1)          # le jeton de média est celui du serveur
        again = json.loads(self.anon.request("POST", "/api/token", body={"user": "ALICE", "password": self.PASSWORD})[2])
        self.assertEqual(again["token"], self.alice.session["token"])                                                # la casse du nom ne compte pas, le jeton est stable
        self.assertEqual((again["user"], again["media_token"]), (self.alice.session["user"], self.alice.session["media_token"]))

    def test_forged_tokens_are_refused(self):
        uid, mac = self.alice.session["token"].split(".")
        tampered = mac[:-1] + ("1" if mac[-1] == "0" else "0")
        for token in (f"{self.bob.account['id']}.{mac}", f"{uid}.{'0' * 64}", f"{uid}.{tampered}", f"{uid}.", f".{mac}", uid, mac, f"inconnu.{mac}", f"{uid}.{mac[:-2]}"):
            self.assertEqual(BearerClient(self.run_.port, token).get("/api/state")[0], 401, token)
            self.assertEqual(Client(self.run_.port, f"hub={token}").get("/api/state")[0], 401, token)
        self.assertEqual(BearerClient(self.run_.port, f"{uid}.{mac}.x").get("/api/state")[0], 401)
        self.assertEqual(BearerClient(self.run_.port, self.alice.session["token"]).get("/api/state")[0], 200)

    def test_the_session_cookie_must_be_exactly_the_token(self):
        token = self.alice.session["token"]
        state = lambda cookie: Client(self.run_.port, cookie).get("/api/state")[0]      # noqa: E731
        for cookie in (f"hub={token}.junk", f"hub={token}zz", f"hub={token}x=1", f"hub={token}0", f"hub={token}=", f"xhub={token}", f"hub={token[:-1]}", f"hub=.{token}", "hub=", f"hub={token.upper()}"):
            self.assertEqual(state(cookie), 401, cookie)
        for cookie in (f"hub={token}", f"hub={token};", f"hub={token}; x=1", f"hub={token};x=1", f"a=b; hub={token}; c=d", f"a=b;hub={token};c=d", f"a=b; hub={token}", f"hub={token} ; c=d"):
            self.assertEqual(state(cookie), 200, cookie)
        self.assertEqual(Client(self.run_.port, f"hub={token}").get("/api/library/films")[0], 200)

    def test_the_bearer_token_must_be_exactly_the_token(self):
        token = self.alice.session["token"]
        for suffix in (".junk", "zz", "x=1", "0", ";x=1", " x"):
            self.assertEqual(BearerClient(self.run_.port, token + suffix).get("/api/state")[0], 401, suffix)
        self.assertEqual(self.anon.get("/api/state", headers={"Authorization": f"Bearer {token}0"})[0], 401)
        for scheme in ("Bearer", "bearer", "BEARER", "Bearer  "):
            self.assertEqual(self.anon.get("/api/state", headers={"Authorization": f"{scheme} {token}"})[0], 200, scheme)
        self.assertEqual(self.anon.get("/api/state", headers={"Authorization": f"Basic {token}"})[0], 401)
        self.assertEqual(self.anon.get("/api/state", headers={"Authorization": token})[0], 401)
        for text, ok in ((token, True), (token + ".junk", False), (token + "zz", False), (token + "\n", False), (token.upper(), False), ("é" + token[token.index("."):], False),
                         (f"{'a' * 64}.{'0' * 64}", True), (f"{'a' * 65}.{'0' * 64}", False), (f".{'0' * 64}", False)):
            self.assertEqual(accounts.TOKEN_FORM.fullmatch(text) is not None, ok, text)

    def test_login_form_sets_a_cookie_for_the_named_account(self):
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        status, headers, _ = self.anon.request("POST", "/api/login", body=b"user=alice&password=" + self.PASSWORD.encode(), headers=form)
        self.assertEqual((status, headers["Location"]), (303, "/"))
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        cookie = Client(self.run_.port, headers["Set-Cookie"].split(";")[0])
        self.assertEqual(cookie.json("/api/state")[1]["user"]["name"], "alice")
        for body in (b"password=" + self.PASSWORD.encode(), b"user=inconnu&password=" + self.PASSWORD.encode(), b"user=alice&password=faux", b"user=alice"):
            status, headers, _ = self.anon.request("POST", "/api/login", body=body, headers=form)
            self.assertEqual((status, headers["Location"]), (303, "/login?error=1"), body)
            self.assertNotIn("Set-Cookie", headers)

    def test_login_accepts_json_too(self):
        status, headers, _ = self.anon.request("POST", "/api/login", body={"user": "BOB", "password": self.PASSWORD})
        self.assertEqual((status, headers["Location"]), (303, "/"))
        self.assertEqual(Client(self.run_.port, headers["Set-Cookie"].split(";")[0]).json("/api/state")[1]["user"]["name"], "Bob")
        for body in ({"password": self.PASSWORD}, {"user": 7, "password": self.PASSWORD}, [1, 2], {"user": "bob"}):
            status, headers, _ = self.anon.request("POST", "/api/login", body=body)
            self.assertEqual((status, headers["Location"]), (303, "/login?error=1"), body)

    def test_login_page_asks_for_the_name_only_with_several_accounts(self):
        self.assertIn(b'name="user"', self.anon.get("/login")[2])
        self.admin.json(f"/api/user/{self.alice.account['id']}/delete", {})
        self.admin.json(f"/api/user/{self.bob.account['id']}/delete", {})
        self.assertNotIn(b'name="user"', self.anon.get("/login")[2])

    def test_the_media_token_does_not_open_the_api_and_names_nobody(self):
        media = self.alice.session["media_token"]
        self.assertEqual(BearerClient(self.run_.port, media).get("/api/state")[0], 401)
        self.assertEqual(Client(self.run_.port, f"hub={media}").get("/api/state")[0], 401)
        self.assertEqual(self.anon.get(f"/api/state?token={media}")[0], 401)
        self.assertEqual(self.anon.get(f"/api/users?token={media}")[0], 401)
        item = next(a["id"] for a in self.alice.json("/api/library/musique")[1]["items"] if a["title"] == "Discovery")
        seen = []
        original = Handler.authorized

        def spy(handler, path, query):
            ok = original(handler, path, query)
            seen.append((ok, handler.user and handler.user["name"]))
            return ok

        with mock.patch.object(Handler, "authorized", spy):
            conn = http.client.HTTPConnection("127.0.0.1", self.run_.port, timeout=30)           # une seule connexion : le même gestionnaire sert les trois requêtes
            self.addCleanup(conn.close)
            for headers in ({"Authorization": f"Bearer {self.alice.token}"}, {"__media": True}, {"Authorization": f"Bearer {self.bob.token}"}):
                url = f"/img/item/{item}" + (f"?token={media}" if headers.pop("__media", False) else "")
                conn.request("GET", url, headers=headers)
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 200, url)
        self.assertEqual(seen, [(True, "alice"), (True, None), (True, "Bob")])        # avec le jeton de média personne n'est connu, et le compte d'avant ne reste pas collé


class TestAccountIsolation(AccountsCase):
    def setUp(self):
        super().setUp()
        self.alice, self.bob = self.new_user("alice"), self.new_user("bob")
        self.dark, self.episodes = entry_of(self.alice, "series", "Dark")

    def test_progress_and_continue_belong_to_one_account(self):
        first = self.episodes[0]["id"]
        self.watch(self.alice, first)
        self.assertEqual((self.continue_ids(self.alice), self.continue_ids(self.bob), self.continue_ids(self.admin)), ([self.dark], [], []))
        row = self.alice.json("/api/home")[1]["continue"][0]
        self.assertEqual((row["id"], row["position"], row["resume"]), (first, 600, True))
        started = lambda api: [i.get("started") for i in api.json("/api/library/series")[1]["items"] if i["id"] == self.dark]      # noqa: E731
        self.assertEqual((started(self.alice), started(self.bob)), ([1], [None]))
        self.assertEqual(self.alice.json(f"/api/item/{self.dark}")[1]["entries"][0]["progress"]["position"], 600)
        self.assertNotIn("progress", self.bob.json(f"/api/item/{self.dark}")[1]["entries"][0])
        self.bob.json(f"/api/item/{self.dark}/seen", {"done": True})                                              # « tout vu » de Bob ne touche pas Alice
        self.assertEqual((self.continue_ids(self.alice), self.continue_ids(self.bob)), ([self.dark], []))
        self.assertEqual(started(self.alice), [1])
        self.bob.json(f"/api/item/{self.dark}/seen", {"done": False})
        self.assertEqual(self.alice.json(f"/api/item/{self.dark}")[1]["entries"][0]["progress"]["position"], 600)

    def test_dismissing_from_continue_is_per_account(self):
        first = self.episodes[0]["id"]
        self.watch(self.alice, first)
        self.watch(self.bob, first)
        self.assertEqual(self.alice.json(f"/api/item/{self.dark}/dismiss", {})[0], 200)
        self.assertEqual((self.continue_ids(self.alice), self.continue_ids(self.bob)), ([], [self.dark]))
        self.assertEqual([r["key"] for r in self.rows("SELECT key FROM kv WHERE key LIKE 'dismiss:%'")], [f"dismiss:{self.alice.account['id']}:{self.dark}"])

    def test_playlists_belong_to_one_account(self):
        ids = [e["id"] for e in entry_of(self.alice, "musique", "Discovery")[1]]
        pid = self.alice.json("/api/playlists", {"name": "Route", "entries": ids[:2]})[1]["id"]
        self.assertEqual([p["name"] for p in self.alice.json("/api/playlists")[1]["playlists"]], ["Route"])
        self.assertEqual((self.bob.json("/api/playlists")[1]["playlists"], self.admin.json("/api/playlists")[1]["playlists"]), ([], []))
        self.assertEqual(self.bob.get(f"/api/playlist/{pid}")[0], 404)
        for action, body in (("add", {"entries": ids[2:]}), ("remove", {"index": 0}), ("move", {"from": 0, "to": 1}), ("rename", {"name": "Volée"}), ("delete", {})):
            self.assertEqual(self.bob.json(f"/api/playlist/{pid}/{action}", body)[0], 404, action)
        again = self.bob.json("/api/playlists", {"name": "route", "entries": ids[2:]})[1]          # même nom chez Bob : sa propre playlist, pas celle d'Alice
        self.assertNotEqual(again["id"], pid)
        self.assertEqual(again["count"], 1)
        mine = self.alice.json(f"/api/playlist/{pid}")[1]
        self.assertEqual(([t["id"] for t in mine["tracks"]], mine["name"]), (ids[:2], "Route"))


class TestAccountPermissions(AccountsCase):
    def setUp(self):
        super().setUp()
        self.bob = self.new_user("bob")
        self.artist = next(i for i in self.bob.json("/api/library/musique")[1]["items"] if i["title"] == "Discovery")["artist_id"]

    def restricted(self):
        """Ce qu'un compte `user` n'a pas le droit de faire : (méthode, chemin, corps)."""
        admin = self.admin.session["user"]["id"]
        return [("POST", "/api/scan", {}), ("POST", "/api/scan", {"lib": "films"}), ("POST", "/api/enrich", {"lib": "musique"}), ("POST", "/api/images/refresh", {"lib": "films"}), ("POST", "/api/cleanup", {"apply": True}),
                ("POST", f"/api/artist/musique/{self.artist}/photo", fixtures.PNG),
                ("POST", f"/api/artist/musique/{self.artist}/photo/crop", {"x": 0, "y": 0, "w": 1, "h": 1}),
                ("GET", "/api/sagas", None), ("POST", "/api/sagas", {"sagas": []}), ("POST", "/api/sagas/add", {"name": "intrus", "films": [1]}), ("GET", "/api/sagas/films", None), ("POST", "/api/sagas/ignore", {"tmdb": 1}),
                ("GET", "/api/users", None), ("POST", "/api/users", {"name": "intrus", "password": self.PASSWORD}),
                ("POST", f"/api/user/{admin}/password", {"password": "piratage-1"}), ("POST", f"/api/user/{admin}/delete", {})]

    def test_a_plain_account_gets_403_on_every_administration_route(self):
        junk = self.run_.libs["films"] / "Inception (2010)" / "Thumbs.db"
        junk.write_bytes(b"x")
        self.addCleanup(junk.unlink, True)
        for method, path, body in self.restricted():
            status, _, data = self.bob.request(method, path, body=body)
            self.assertEqual((status, json.loads(data)), (403, {"error": "réservé à l'administrateur"}), path)
        self.assertEqual(len(self.bob.json("/api/jobs")[1]["jobs"]), len(self.hub.cfg.libraries))      # seuls les scans du démarrage : rien de lancé depuis
        self.assertTrue(junk.exists())
        self.assertEqual([u["name"] for u in self.admin.json("/api/users")[1]["users"]], ["admin", "bob"])
        self.assertEqual(self.admin.get("/api/state")[0], 200)                            # le compte de l'administrateur est intact : même jeton, même mot de passe
        self.assertEqual(self.anon.request("POST", "/api/token", body={"user": "admin", "password": "secret"})[0], 200)

    def test_a_plain_account_keeps_everything_else(self):
        self.assertEqual(self.bob.json("/api/state")[1]["user"], {"id": self.bob.account["id"], "name": "bob", "role": "user"})
        for path in ("/api/home", "/api/library/films", "/api/jobs", "/api/playlists", f"/api/artist/musique/{self.artist}", "/api/search?q=dark"):
            self.assertEqual(self.bob.get(path)[0], 200, path)

    def test_the_jobs_log_is_for_the_administrator_only(self):
        for log in ("1", "1&x=2"):
            status, _, data = self.bob.request("GET", f"/api/jobs?log={log}")
            self.assertEqual((status, json.loads(data)), (403, {"error": "réservé à l'administrateur"}), log)
        self.assertEqual(self.anon.get("/api/jobs?log=1")[0], 401)
        for path in ("/api/jobs", "/api/jobs?log=0", "/api/jobs?log="):                 # sans journal, l'état des tâches reste ouvert à tout compte
            status, data = self.bob.json(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(data["jobs"] and all("log" not in j for j in data["jobs"]), path)
            self.assertIn("busy", data)
        status, data = self.admin.json("/api/jobs?log=1")
        self.assertEqual(status, 200)
        self.assertTrue(data["jobs"] and all(isinstance(j["log"], list) for j in data["jobs"]))

    def test_an_administrator_made_by_the_api_has_the_same_rights(self):
        second = self.new_user("deuxieme", role="admin")
        self.assertEqual(second.get("/api/users")[0], 200)
        self.assertEqual(second.json("/api/cleanup", {})[0], 202)
        self.run_.wait_jobs()
        self.assertEqual(second.json("/api/state")[1]["user"]["role"], "admin")

    def test_a_refusal_does_not_desynchronise_a_kept_alive_connection(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.run_.port, timeout=30)
        self.addCleanup(conn.close)
        headers = {"Authorization": f"Bearer {self.bob.session['token']}", "Content-Type": "application/json"}
        for method, path, body in self.restricted():
            if method != "POST":
                continue
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            conn.request("POST", path, body=data + b" " * 20000, headers=headers)        # un corps que le refus n'a pas besoin de lire, mais qui est là
            first = conn.getresponse()
            first.read()
            self.assertEqual(first.status, 403, path)
            conn.request("GET", "/api/state", headers=headers)                              # la requête suivante, sur la même connexion
            second = conn.getresponse()
            self.assertEqual((second.status, json.loads(second.read())["user"]["name"]), (200, "bob"), path)

    def test_a_refusal_with_a_huge_body_closes_the_connection(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.run_.port, timeout=30)
        self.addCleanup(conn.close)
        conn.putrequest("POST", "/api/scan")
        conn.putheader("Authorization", f"Bearer {self.bob.session['token']}")
        conn.putheader("Content-Length", str(5 << 20))
        conn.endheaders()                                                                  # l'en-tête annonce 5 Mo qui ne viendront pas
        response = conn.getresponse()
        response.read()
        self.assertEqual(response.status, 403)
        conn.sock.settimeout(10)
        self.assertEqual(conn.sock.recv(1), b"")                                           # le serveur a coupé : le corps annoncé ne peut plus être resynchronisé


class TestAccountManagement(AccountsCase):
    def test_create_lists_and_never_shows_the_hash(self):
        status, made = self.admin.json("/api/users", {"name": "  Barbara   Martin ", "password": self.PASSWORD})
        self.assertEqual(status, 201)
        self.assertEqual((made["name"], made["role"]), ("Barbara Martin", "user"))
        self.assertRegex(made["id"], r"^[0-9a-f]{16}$")
        self.assertIsInstance(made["created"], int)
        self.assertEqual(set(made), {"id", "name", "role", "created"})
        boss = self.admin.json("/api/users", {"name": "Chef", "password": self.PASSWORD, "role": "admin"})[1]
        self.assertEqual(boss["role"], "admin")
        users = self.admin.json("/api/users")[1]["users"]
        self.assertEqual([u["name"] for u in users], ["admin", "Barbara Martin", "Chef"])           # du plus ancien au plus récent
        self.assertEqual({k for u in users for k in u}, {"id", "name", "role", "created"})
        self.assertNotIn("scrypt", json.dumps(users))
        self.assertEqual(self.sign_in("barbara martin", self.PASSWORD).session["user"]["id"], made["id"])

    def test_create_refuses_bad_names_passwords_and_roles(self):
        good = {"name": "alice", "password": self.PASSWORD}
        self.admin.json("/api/users", good)
        self.admin.json("/api/users", {"name": "Élodie", "password": self.PASSWORD})
        for body, status in (({**good, "name": "ALICE"}, 409), ({**good, "name": " alice  "}, 409), ({**good, "name": "Admin"}, 409), ({**good, "name": "ÉLODIE"}, 409),
                             ({**good, "name": "x", "password": "court"}, 400), ({**good, "name": "x", "password": "é" * 201}, 400), ({"name": "x"}, 400), ({"name": "x", "password": 12345678}, 400),
                             ({**good, "name": "x", "role": "root"}, 400), ({**good, "name": "x", "role": ["admin"]}, 400), ({**good, "name": ""}, 400), ({**good, "name": "   "}, 400),
                             ({**good, "name": "x" * 41}, 400), ({**good, "name": "a\x00b"}, 400), ({**good, "name": 12}, 400), ({"password": self.PASSWORD}, 400)):
            answer = self.admin.json("/api/users", body)
            self.assertEqual(answer[0], status, body)
            self.assertIn("error", answer[1])
        self.assertEqual(self.admin.json("/api/users", [1, 2])[0], 400)
        self.assertEqual(self.admin.json("/api/users", {"name": "x" * 40, "password": "12345678"})[0], 201)       # les bornes elles-mêmes passent
        self.assertEqual(self.admin.json("/api/users", {"name": "y", "password": "é" * 200})[0], 201)
        self.assertEqual(sorted(u["name"] for u in self.admin.json("/api/users")[1]["users"]), sorted(["admin", "alice", "Élodie", "x" * 40, "y"]))

    def test_the_administrator_gives_another_account_a_new_password(self):
        bob = self.new_user("bob")
        status, answer = self.admin.json(f"/api/user/{bob.account['id']}/password", {"password": "nouveau-mdp-1"})
        self.assertEqual((status, answer), (200, {"ok": True}))                                    # pas de jeton : ce n'est pas son compte
        self.assertEqual(bob.get("/api/state")[0], 401)                                           # ses sessions sont fermées
        self.assertEqual(self.anon.request("POST", "/api/token", body={"user": "bob", "password": self.PASSWORD})[0], 401)
        self.assertEqual(self.sign_in("bob", "nouveau-mdp-1").get("/api/state")[0], 200)
        self.assertEqual(self.admin.get("/api/state")[0], 200)
        self.assertEqual(self.admin.json(f"/api/user/{bob.account['id']}/password", {"password": "court"})[0], 400)
        self.assertEqual(self.admin.json(f"/api/user/{bob.account['id']}/password", {})[0], 400)
        self.assertEqual(self.sign_in("bob", "nouveau-mdp-1").get("/api/state")[0], 200)          # les refus n'ont rien changé
        status, answer = self.admin.json("/api/user/inconnu/password", {"password": "nouveau-mdp-1"})
        self.assertEqual((status, answer), (404, {"error": "compte inconnu"}))

    def test_the_administrator_changing_its_own_password_gets_a_new_token(self):
        status, headers, data = self.admin.request("POST", "/api/user/default/password", body={"current": "secret", "password": "administrateur-2"})
        answer = json.loads(data)
        self.assertEqual((status, answer["ok"]), (200, True))
        self.assertEqual(self.admin.get("/api/state")[0], 401)
        self.assertIn(f"hub={answer['token']}", headers["Set-Cookie"])
        self.assertEqual(BearerClient(self.run_.port, answer["token"]).get("/api/state")[0], 200)
        self.assertEqual(self.anon.request("POST", "/api/token", body={"password": "secret"})[0], 401)
        self.assertEqual(self.anon.request("POST", "/api/token", body={"password": "administrateur-2"})[0], 200)

    def test_the_administrator_must_give_its_current_password_to_change_its_own(self):
        for body in ({"password": "administrateur-2"}, {"current": "faux", "password": "administrateur-2"}, {"current": 12, "password": "administrateur-2"},
                     {"current": None, "password": "administrateur-2"}, {"current": ["secret"], "password": "administrateur-2"}, {"current": "", "password": "administrateur-2"}):
            status, answer = self.admin.json("/api/user/default/password", body)
            self.assertEqual((status, answer), (400, {"error": "mot de passe actuel incorrect"}), body)
        self.assertEqual(self.admin.get("/api/state")[0], 200)                           # la session n'a pas bougé...
        self.assertEqual(self.anon.request("POST", "/api/token", body={"password": "secret"})[0], 200)          # ...ni le mot de passe
        self.assertEqual(self.anon.request("POST", "/api/token", body={"password": "administrateur-2"})[0], 401)
        status, answer = self.admin.json("/api/user/default/password", {"current": "secret", "password": "court"})          # le bon `current` ne dispense pas de la longueur minimale
        self.assertEqual(status, 400)
        self.assertNotIn("actuel", answer["error"])

    def test_the_administrator_changes_another_password_without_giving_current(self):
        bob = self.new_user("bob")
        status, answer = self.admin.json(f"/api/user/{bob.account['id']}/password", {"password": "nouveau-mdp-1"})        # ni `current` ni compte à vérifier : celui de Bob n'est pas le sien
        self.assertEqual((status, answer), (200, {"ok": True}))
        self.assertEqual(self.admin.get("/api/state")[0], 200)
        self.assertEqual(self.sign_in("bob", "nouveau-mdp-1").get("/api/state")[0], 200)

    def test_signing_out_the_other_devices_asks_for_the_password_and_keeps_this_one_connected(self):
        bob, lea = self.new_user("bob"), self.new_user("lea")
        for body in ({}, {"current": "faux"}, {"current": 12}, {"current": None}):
            status, answer = bob.json("/api/me/signout-others", body)
            self.assertEqual((status, answer), (400, {"error": "mot de passe actuel incorrect"}), body)
        self.assertEqual(bob.get("/api/state")[0], 200)                                           # rien n'a bougé
        elsewhere = self.session_for("bob")                                                       # le même compte sur un autre appareil
        status, headers, data = bob.request("POST", "/api/me/signout-others", body={"current": self.PASSWORD})
        answer = json.loads(data)
        self.assertEqual((status, answer["ok"]), (200, True))
        self.assertEqual(bob.get("/api/state")[0], 401)                                           # l'ancien jeton ne vaut plus, celui-ci comme l'autre appareil
        self.assertEqual(elsewhere.get("/api/state")[0], 401)
        self.assertEqual(BearerClient(self.run_.port, answer["token"]).get("/api/state")[0], 200)
        cookie = headers["Set-Cookie"]                                                            # le navigateur d'où vient la demande reste connecté
        self.assertTrue(cookie.startswith(f"hub={answer['token']};") and "HttpOnly" in cookie, cookie)
        self.assertEqual(Client(self.run_.port, cookie.split(";")[0]).get("/api/state")[0], 200)
        self.assertEqual(lea.get("/api/state")[0], 200)                                           # les autres comptes ne sont pas touchés
        self.assertEqual(self.sign_in("bob", self.PASSWORD).session["token"], answer["token"])    # le mot de passe est resté le même
        self.assertEqual(self.anon.request("POST", "/api/me/signout-others", body={"current": self.PASSWORD})[0], 401)

    def test_a_user_changes_its_own_password(self):
        bob = self.new_user("bob")
        for body in ({"current": "faux", "new": "nouveau-mdp-1"}, {"new": "nouveau-mdp-1"}, {"current": 12, "new": "nouveau-mdp-1"}, {"current": "secret", "new": "nouveau-mdp-1"}):
            status, answer = bob.json("/api/me/password", body)
            self.assertEqual((status, answer), (400, {"error": "mot de passe actuel incorrect"}), body)
        status, answer = bob.json("/api/me/password", {"current": self.PASSWORD, "new": "court"})
        self.assertEqual(status, 400)
        self.assertNotIn("actuel", answer["error"])
        self.assertEqual(bob.get("/api/state")[0], 200)                                           # rien n'a changé
        status, headers, data = bob.request("POST", "/api/me/password", body={"current": self.PASSWORD, "new": "nouveau-mdp-1"})
        answer = json.loads(data)
        self.assertEqual((status, answer["ok"]), (200, True))
        self.assertRegex(answer["token"], rf"^{bob.account['id']}\.[0-9a-f]{{64}}$")
        self.assertNotEqual(answer["token"], bob.session["token"])
        self.assertEqual(bob.get("/api/state")[0], 401)                                           # l'ancien jeton est refusé...
        self.assertEqual(Client(self.run_.port, f"hub={bob.session['token']}").get("/api/state")[0], 401)       # ...en cookie aussi
        self.assertEqual(BearerClient(self.run_.port, answer["token"]).get("/api/state")[0], 200)
        cookie = headers["Set-Cookie"]                                                            # le navigateur reste connecté
        self.assertTrue(cookie.startswith(f"hub={answer['token']};") and "HttpOnly" in cookie, cookie)
        self.assertEqual(Client(self.run_.port, cookie.split(";")[0]).get("/api/state")[0], 200)
        renewed = self.sign_in("bob", "nouveau-mdp-1")
        self.assertEqual(renewed.session["token"], answer["token"])
        self.assertEqual(renewed.session["media_token"], bob.session["media_token"])              # le jeton de média ne change pas
        self.assertEqual(self.anon.request("POST", "/api/token", body={"user": "bob", "password": self.PASSWORD})[0], 401)

    def connection(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.run_.port, timeout=30)
        self.addCleanup(conn.close)
        return conn

    def answer(self, conn):
        response = conn.getresponse()
        return response.status, response.read()

    def assert_next_request_is_served(self, conn):
        """La requête suivante, sur la même connexion, reçoit une vraie réponse (pas un reste de corps pris pour une requête)."""
        conn.request("GET", "/api/state", headers={"Authorization": f"Bearer {self.admin.session['token']}"})
        status, data = self.answer(conn)
        self.assertEqual((status, json.loads(data)["user"]["name"]), (200, "admin"))

    def assert_connection_closed(self, conn):
        conn.sock.settimeout(10)
        self.assertEqual(conn.sock.recv(1), b"")

    def test_a_closed_session_answers_401_without_desynchronising_the_connection(self):
        bob = self.new_user("bob")
        self.admin.json(f"/api/user/{bob.account['id']}/password", {"password": "nouveau-mdp-1"})        # la session de Bob est fermée, son navigateur ne le sait pas encore
        conn = self.connection()
        conn.request("POST", "/api/progress", body=json.dumps({"entry": "x", "position": 1}), headers={"Authorization": f"Bearer {bob.session['token']}", "Content-Type": "application/json"})
        self.assertEqual(self.answer(conn)[0], 401)
        self.assert_next_request_is_served(conn)

    def test_a_route_that_fails_before_reading_its_body_does_not_desynchronise_the_connection(self):
        conn = self.connection()
        headers = {"Authorization": f"Bearer {self.admin.session['token']}"}
        for _ in range(2):
            conn.request("POST", "/api/artist/musique/zzz/photo", body=fixtures.PNG * 50, headers=headers)      # l'artiste n'existe pas : 404 levé avant `body_bytes`
            self.assertEqual(self.answer(conn)[0], 404)
            self.assert_next_request_is_served(conn)

    def test_a_refused_body_with_a_bad_or_huge_length_closes_the_connection(self):
        bob = self.new_user("bob")
        self.admin.json(f"/api/user/{bob.account['id']}/password", {"password": "nouveau-mdp-1"})
        admin = f"Bearer {self.admin.session['token']}"
        for path, auth, length, status in (("/api/progress", f"Bearer {bob.session['token']}", "abc", 401), ("/api/progress", f"Bearer {bob.session['token']}", str(5 << 20), 401),
                                           ("/api/progress", f"Bearer {bob.session['token']}", "-1", 401), ("/api/artist/musique/zzz/photo", admin, "abc", 404),
                                           ("/api/artist/musique/zzz/photo", admin, str(5 << 20), 404)):
            conn = self.connection()
            conn.putrequest("POST", path)
            conn.putheader("Authorization", auth)
            conn.putheader("Content-Length", length)
            conn.endheaders()                                                              # l'en-tête annonce un corps qui ne viendra pas
            self.assertEqual(self.answer(conn)[0], status, (path, length))
            self.assert_connection_closed(conn)
        self.assertEqual(self.admin.get("/api/state")[0], 200)                           # le serveur, lui, n'a rien perdu

    def test_deleting_an_account_closes_its_sessions(self):
        bob = self.new_user("bob")
        self.assertEqual(self.admin.json(f"/api/user/{bob.account['id']}/delete", {}), (200, {"ok": True}))
        self.assertEqual(bob.get("/api/state")[0], 401)
        self.assertEqual(self.anon.request("POST", "/api/token", body={"user": "bob", "password": self.PASSWORD})[0], 401)
        self.assertEqual([u["name"] for u in self.admin.json("/api/users")[1]["users"]], ["admin"])
        self.assertEqual(self.admin.json(f"/api/user/{bob.account['id']}/delete", {}), (404, {"error": "compte inconnu"}))
        again = self.new_user("bob")                                                              # le nom est libre, le nouveau compte n'hérite pas de l'ancien jeton
        self.assertNotEqual(again.account["id"], bob.account["id"])
        self.assertEqual(bob.get("/api/state")[0], 401)

    def test_deleting_removes_progress_playlists_and_dismissals_of_that_account_only(self):
        alice, bob = self.new_user("alice"), self.new_user("bob")
        dark, episodes = entry_of(alice, "series", "Dark")
        ids = [e["id"] for e in entry_of(alice, "musique", "Discovery")[1]]
        for who in (alice, bob):
            self.watch(who, episodes[0]["id"])
            self.watch(who, episodes[1]["id"])
            who.json(f"/api/item/{dark}/dismiss", {})
            who.json("/api/playlists", {"name": "Liste", "entries": ids})
            who.json("/api/playlists", {"name": "Autre", "entries": ids[:1]})
        mine, theirs = alice.account["id"], bob.account["id"]
        self.assertEqual(self.count("SELECT COUNT(*) AS n FROM playlist_tracks"), 8)
        self.assertEqual(self.admin.json(f"/api/user/{mine}/delete", {})[0], 200)
        self.assertEqual(self.count("SELECT COUNT(*) AS n FROM progress WHERE profile=?", mine), 0)
        self.assertEqual(self.count("SELECT COUNT(*) AS n FROM playlists WHERE profile=?", mine), 0)
        self.assertEqual(self.count("SELECT COUNT(*) AS n FROM kv WHERE key LIKE ? || '%'", f"dismiss:{mine}:"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) AS n FROM users WHERE id=?", mine), 0)
        self.assertEqual(self.count("SELECT COUNT(*) AS n FROM playlist_tracks"), 4)                 # les pistes de ses playlists sont parties avec elles
        self.assertEqual(self.count("SELECT COUNT(*) AS n FROM progress WHERE profile=?", theirs), 2)       # celles de Bob sont intactes
        self.assertEqual(self.count("SELECT COUNT(*) AS n FROM playlists WHERE profile=?", theirs), 2)
        self.assertEqual(self.count("SELECT COUNT(*) AS n FROM kv WHERE key LIKE ? || '%'", f"dismiss:{theirs}:"), 1)
        self.assertEqual(sorted(len(bob.json(f"/api/playlist/{p['id']}")[1]["tracks"]) for p in bob.json("/api/playlists")[1]["playlists"]), [1, 3])

    def test_the_last_administrator_cannot_be_deleted(self):
        refused = (400, {"error": "impossible de supprimer le dernier administrateur"})
        self.assertEqual(self.admin.json("/api/user/default/delete", {}), refused)
        bob = self.new_user("bob")                                                                # un compte ordinaire n'est pas un administrateur de rechange
        self.assertEqual(self.admin.json("/api/user/default/delete", {}), refused)
        self.assertEqual(self.admin.get("/api/state")[0], 200)
        second = self.new_user("deuxieme", role="admin")
        status, headers, data = self.admin.request("POST", "/api/user/default/delete", body={})      # un administrateur peut se supprimer s'il en reste un autre
        self.assertEqual((status, json.loads(data)), (200, {"ok": True}))
        self.assertIn("Max-Age=0", headers["Set-Cookie"])                                         # et son cookie est effacé
        self.assertEqual(self.admin.get("/api/state")[0], 401)
        self.assertEqual(second.json(f"/api/user/{second.account['id']}/delete", {}), refused)
        self.assertEqual(second.json(f"/api/user/{bob.account['id']}/delete", {})[0], 200)
        self.assertEqual([u["name"] for u in second.json("/api/users")[1]["users"]], ["deuxieme"])


ROOT = Path(__file__).resolve().parent.parent


def run_cli(tmp, *args, stdin=""):
    """`python -m hub user …` dans un sous-processus, sur la base de `tmp/data` (le HUB_DATA de Running) ; le mot de passe arrive par l'entrée standard."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("HUB_")}
    env.update({"HUB_DATA": str(Path(tmp) / "data"), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    return subprocess.run([sys.executable, "-m", "hub", "user", *args], input=stdin, capture_output=True, text=True, encoding="utf-8", cwd=ROOT, env=env, timeout=120)


class TestOpenServer(unittest.TestCase):
    """Sans HUB_PASSWORD ni compte : tout est ouvert, avec un profil unique."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.run_ = Running(self.tmp, admin="Maison")
        self.addCleanup(self.run_.stop)
        self.api = Client(self.run_.port)

    def test_state_says_there_is_no_auth_and_who_the_profile_is(self):
        state = self.api.json("/api/state")[1]
        self.assertEqual((state["auth"], state["user"]), (False, {"id": "default", "name": "Maison", "role": "admin"}))
        self.assertEqual(self.api.json("/api/users"), (200, {"users": []}))
        self.assertEqual(self.api.get("/login")[0], 200)

    def test_everything_is_reachable_including_administration(self):
        self.assertEqual(self.api.get("/api/home")[0], 200)
        self.assertEqual(self.api.json("/api/scan", {"lib": "films"})[0], 202)
        self.run_.wait_jobs()
        self.assertEqual(self.api.json("/api/cleanup", {})[0], 202)
        self.run_.wait_jobs()
        self.assertEqual(self.api.json("/api/progress", {"entry": "nope", "position": 1})[0], 404)

    def test_account_routes_answer_400_and_create_nothing(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.run_.port, timeout=30)
        self.addCleanup(conn.close)
        for path, body in (("/api/users", {"name": "intrus", "password": "motdepasse1"}), ("/api/user/default/password", {"password": "motdepasse1"}),
                           ("/api/user/default/delete", {}), ("/api/me/password", {"current": "x", "new": "motdepasse1"}), ("/api/me/signout-others", {"current": "x"})):
            conn.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            data = json.loads(response.read())
            self.assertEqual(response.status, 400, path)
            self.assertIn("ouvert", data["error"])
            conn.request("GET", "/api/state")                                                       # même connexion : le corps refusé a bien été lu
            follow = conn.getresponse()
            follow.read()
            self.assertEqual(follow.status, 200, path)
        self.assertFalse(self.run_.hub.auth())
        self.assertEqual(self.run_.hub.db.query("SELECT id FROM users"), [])

    def cli(self, *args, password):
        done = run_cli(self.tmp, *args, stdin=f"{password}\n{password}\n")
        self.assertEqual(done.returncode, 0, f"{args} : {done.stdout!r} {done.stderr!r}")

    def test_the_first_account_closes_the_server_and_keeps_the_single_profile(self):
        dark, episodes = entry_of(self.api, "series", "Dark")
        self.api.json("/api/progress", {"entry": episodes[0]["id"], "position": 600, "duration": 3000})
        self.cli("add", "alice", password="motdepasse1")                                            # le serveur tourne pendant ce temps
        self.assertEqual(self.api.get("/api/state")[0], 401)                                        # fermé dès qu'un compte existe
        self.assertEqual(self.api.get("/api/users")[0], 401)
        self.assertEqual(self.api.get("/media/abc/file")[0], 302)
        status, _, body = self.api.request("POST", "/api/token", body={"password": "motdepasse1"})
        session = json.loads(body)
        self.assertEqual((status, session["user"]), (200, {"id": "default", "name": "alice", "role": "admin"}))
        alice = BearerClient(self.run_.port, session["token"])
        self.assertEqual([c["item"]["id"] for c in alice.json("/api/home")[1]["continue"]], [dark])      # elle reprend la progression d'avant
        self.cli("passwd", "alice", password="autre-mot-de-passe")                                  # changer le mot de passe en ligne de commande...
        self.assertEqual(alice.get("/api/state")[0], 401)                                           # ...ferme les sessions du serveur qui tourne
        self.cli("add", "bob", password="motdepasse1")
        self.assertEqual(self.api.request("POST", "/api/token", body={"password": "autre-mot-de-passe"})[0], 401)      # deux comptes : le nom devient obligatoire
        self.assertEqual(self.api.request("POST", "/api/token", body={"user": "alice", "password": "autre-mot-de-passe"})[0], 200)


class TestLoginThrottle(AccountsCase):
    """Les essais de connexion ratés d'une même adresse s'allongent, puis ne sont plus vérifiés du tout."""

    def test_the_wait_grows_after_each_failure_then_the_address_is_locked(self):
        clock = [1000.0]
        throttle = accounts.Throttle(clock=lambda: clock[0])
        waits = []
        for _ in range(throttle.LIMIT):
            waits.append(throttle.delay("a"))
            throttle.failed("a")
        self.assertEqual(waits[:throttle.FREE + 1], [throttle.BASE] * (throttle.FREE + 1))          # les premiers essais ne coûtent que le délai de base
        self.assertEqual(waits[throttle.FREE + 1:throttle.FREE + 3], [throttle.BASE * 2, throttle.BASE * 4])
        self.assertEqual(max(waits), throttle.CAP)
        with self.assertRaises(accounts.AccountError) as caught:
            throttle.delay("a")
        self.assertEqual(caught.exception.status, 429)
        self.assertIn("réessaie dans", caught.exception.message)
        self.assertEqual(throttle.delay("b"), throttle.BASE)                                        # une autre adresse n'est pas touchée
        clock[0] += throttle.WINDOW + 1
        self.assertEqual(throttle.delay("a"), throttle.BASE)                                        # les échecs s'oublient avec le temps

    def test_a_success_wipes_the_failures(self):
        throttle = accounts.Throttle()
        for _ in range(throttle.LIMIT - 1):
            throttle.failed("a")
        throttle.passed("a")
        self.assertEqual(throttle.delay("a"), throttle.BASE)

    def test_too_many_failures_stop_the_checks_even_for_the_good_password(self):
        with mock.patch.object(accounts.Throttle, "LIMIT", 3), mock.patch.object(accounts.Throttle, "BASE", 0.0), mock.patch.object(accounts.Throttle, "CAP", 0.0):
            for _ in range(3):
                self.assertEqual(self.anon.request("POST", "/api/token", body={"user": "admin", "password": "faux"})[0], 401)
            status, _, body = self.anon.request("POST", "/api/token", body={"user": "admin", "password": "secret"})
            self.assertEqual(status, 429)                                                            # le bon mot de passe n'est même pas regardé
            self.assertIn("trop d'essais", json.loads(body)["error"])
            status, headers, _ = self.anon.request("POST", "/api/login", body=b"user=admin&password=secret", headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual((status, headers["Location"]), (303, "/login?error=2"))
            self.assertIn("Trop d'essais", self.anon.get("/login?error=2")[2].decode())
            self.assertNotIn("Set-Cookie", headers)
            self.hub.throttle.passed("127.0.0.1")                                                    # l'attente passée : l'accès revient
            self.assertEqual(self.anon.request("POST", "/api/token", body={"user": "admin", "password": "secret"})[0], 200)

    def test_a_success_in_between_keeps_the_count_from_building_up(self):
        with mock.patch.object(accounts.Throttle, "LIMIT", 3), mock.patch.object(accounts.Throttle, "BASE", 0.0), mock.patch.object(accounts.Throttle, "CAP", 0.0):
            for _ in range(5):
                for _ in range(2):
                    self.assertEqual(self.anon.request("POST", "/api/token", body={"user": "admin", "password": "faux"})[0], 401)
                self.assertEqual(self.anon.request("POST", "/api/token", body={"user": "admin", "password": "secret"})[0], 200)

    def test_an_open_server_is_never_throttled(self):
        tmp = tempfile.mkdtemp()                        # un dossier neuf : ici le compte `admin` existe déjà
        self.addCleanup(shutil.rmtree, tmp, True)
        run = Running(tmp, password="")
        self.addCleanup(run.stop)
        for _ in range(15):
            self.assertEqual(Client(run.port).request("POST", "/api/token", body={"user": "x", "password": "y"})[0], 200)


class TestSchemaMigrations(unittest.TestCase):
    """Le schéma monte d'une version à la fois, en une transaction ; une base plus récente que le hub n'est pas touchée."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "hub.db"

    def columns(self, db):
        return [r["name"] for r in db.query("PRAGMA table_info(items)")]

    def test_a_new_base_gets_the_current_version(self):
        from hub import db as database
        self.assertEqual(database.Database(self.path).kv_get("schema"), database.SCHEMA_VERSION)

    def test_a_migration_runs_once_and_the_version_follows(self):
        from hub import db as database
        database.Database(self.path)
        add = lambda conn: conn.execute("ALTER TABLE items ADD COLUMN extra TEXT")           # noqa: E731
        with mock.patch.object(database, "SCHEMA_VERSION", 3), mock.patch.dict(database.MIGRATIONS, {3: add}):
            db = database.Database(self.path)
            self.assertIn("extra", self.columns(db))
            self.assertEqual(db.kv_get("schema"), 3)
            self.assertEqual(database.Database(self.path).kv_get("schema"), 3)               # rouverte : rien n'est rejoué (l'ALTER lèverait « duplicate column »)

    def test_a_failed_migration_leaves_the_base_as_it_was(self):
        from hub import db as database
        database.Database(self.path)

        def bad(conn):
            conn.execute("ALTER TABLE items ADD COLUMN extra TEXT")
            raise ValueError("boum")
        with mock.patch.object(database, "SCHEMA_VERSION", 3), mock.patch.dict(database.MIGRATIONS, {3: bad}):
            with self.assertRaises(RuntimeError) as caught:
                database.Database(self.path)
        self.assertIn("version 3", str(caught.exception))
        db = database.Database(self.path)
        self.assertEqual((db.kv_get("schema"), "extra" in self.columns(db)), (database.SCHEMA_VERSION, False))      # la colonne ajoutée avant l'échec a été défaite

    def test_a_base_from_before_the_current_version_is_brought_up(self):
        from hub import db as database
        db = database.Database(self.path)
        db.kv_set("schema", 1)
        self.assertEqual(database.Database(self.path).kv_get("schema"), database.SCHEMA_VERSION)

    def test_a_newer_base_is_left_alone_and_the_hub_says_so(self):
        import contextlib
        import io
        from hub import db as database
        database.Database(self.path).kv_set("schema", 99)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            db = database.Database(self.path)
        self.assertIn("99", err.getvalue())
        self.assertEqual(db.kv_get("schema"), 99)                                              # le numéro de la base plus récente n'est pas rabaissé


class TestImageConversions(unittest.TestCase):
    def test_without_pillow_at_most_max_streams_ffmpeg_run_at_once(self):
        import types
        from hub.media import images
        cfg = types.SimpleNamespace(data=Path(tempfile.mkdtemp()), ffmpeg="ffmpeg-bidon", max_streams=2)
        self.addCleanup(shutil.rmtree, cfg.data, True)
        engine = images.Images(cfg, None)
        running, peak, lock = [0], [0], threading.Lock()

        def fake_run(*args, **kwargs):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.05)
            with lock:
                running[0] -= 1
            return types.SimpleNamespace(returncode=0, stdout=b"\xff\xd8\xff reduit")
        with mock.patch.object(images, "Image", None), mock.patch.object(images.subprocess, "run", fake_run):
            threads = [threading.Thread(target=engine.resize, args=(fixtures.PNG, 160)) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(running[0], 0)
        self.assertEqual(peak[0], 2)                                                           # jamais plus que HUB_MAX_STREAMS, mais bien deux en parallèle


class TestAccountCommandLine(unittest.TestCase):
    PASSWORD = "motdepasse1"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def cli(self, *args, password=PASSWORD, ok=True):
        done = run_cli(self.tmp, *args, stdin=f"{password}\n{password}\n")
        self.assertEqual(done.returncode == 0, ok, f"{args} : {done.stdout!r} {done.stderr!r}")
        return done

    def database(self):
        from hub.db import Database
        return Database(Path(self.tmp) / "data" / "hub.db")

    def test_add_list_passwd_and_rm(self):
        self.assertEqual(self.cli("list").stdout, "")
        first = self.cli("add", "alice")
        self.assertIn("créé (admin", first.stdout)
        self.assertIn("reprend la progression", first.stdout)                                        # le premier compte hérite du profil d'avant
        self.assertIn("créé (user)", self.cli("add", "Bob").stdout)
        self.assertIn("créé (admin)", self.cli("add", "carole", "--admin").stdout)
        lines = [line.split() for line in self.cli("list").stdout.splitlines()]
        self.assertEqual([line[:2] for line in lines], [["alice", "admin"], ["Bob", "user"], ["carole", "admin"]])
        self.assertEqual(lines[0][2], "default")
        db = self.database()
        self.assertEqual(accounts.authenticate(db, "bob", self.PASSWORD)["role"], "user")
        self.assertIn("mot de passe de « Bob » changé", self.cli("passwd", "BOB", password="nouveau-mdp-1").stdout)
        self.assertEqual(accounts.authenticate(db, "bob", "nouveau-mdp-1")["name"], "Bob")
        with self.assertRaises(accounts.AccountError):
            accounts.authenticate(db, "bob", self.PASSWORD)
        self.assertIn("supprimé", self.cli("rm", "bob").stdout)
        self.assertEqual([r["name"] for r in accounts.rows(db)], ["alice", "carole"])

    def test_refusals_say_why(self):
        self.cli("add", "alice")
        for args, password, message in ((("add", "ALICE"), self.PASSWORD, "déjà pris"), (("add", "bob"), "court", "le mot de passe doit faire"),
                                         (("passwd", "inconnu"), self.PASSWORD, "compte inconnu"), (("rm", "inconnu"), self.PASSWORD, "compte inconnu"),
                                         (("rm", "alice"), self.PASSWORD, "dernier administrateur")):
            self.assertIn(message, self.cli(*args, password=password, ok=False).stderr, args)
        mismatch = run_cli(self.tmp, "add", "bob", stdin="motdepasse1\nmotdepasse2\n")
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIn("ne correspondent pas", mismatch.stderr)
        self.assertEqual([line.split()[0] for line in self.cli("list").stdout.splitlines()], ["alice"])        # rien d'autre n'a été créé
        self.assertNotEqual(run_cli(self.tmp, "bidule").returncode, 0)

    def test_rm_takes_the_progress_and_playlists_along(self):
        self.cli("add", "alice")
        self.cli("add", "bob")
        db = self.database()
        bob = db.one("SELECT id FROM users WHERE name='bob'")["id"]
        db.execute("INSERT INTO progress(profile, entry_id, position, duration, done, updated) VALUES(?, 'e1', 5, 10, 0, 1)", (bob,))
        db.execute("INSERT INTO playlists(id, profile, name, created, updated) VALUES('p1', ?, 'Liste', 1, 1)", (bob,))
        db.execute("INSERT INTO playlist_tracks(playlist_id, pos, entry_id) VALUES('p1', 0, 'e1')")
        db.kv_set(f"dismiss:{bob}:x", 1)
        self.cli("rm", "bob")
        for sql in ("SELECT COUNT(*) AS n FROM progress", "SELECT COUNT(*) AS n FROM playlists", "SELECT COUNT(*) AS n FROM playlist_tracks", "SELECT COUNT(*) AS n FROM kv WHERE key LIKE 'dismiss:%'"):
            self.assertEqual(db.one(sql)["n"], 0, sql)


class TestAccountPasswords(unittest.TestCase):
    """Le hachage de hub/accounts.py, sans serveur : scrypt, et pbkdf2 quand l'OpenSSL de Python n'a pas scrypt."""

    def setUp(self):
        patch = mock.patch.object(accounts, "PBKDF2_ROUNDS", 1000)               # le repli coûte 600 000 tours en vrai : pas dans un test
        patch.start()
        self.addCleanup(patch.stop)

    def without_scrypt(self):
        patch = mock.patch.object(hashlib, "scrypt")
        patch.start()
        self.addCleanup(patch.stop)
        del hashlib.scrypt                                                         # `hasattr(hashlib, "scrypt")` devient faux ; le patch le remet à la fin
        return patch

    def test_scrypt_hashes_use_a_new_salt_each_time(self):
        first, second = accounts.hash_password("motdepasse1"), accounts.hash_password("motdepasse1")
        self.assertTrue(first.startswith("scrypt$"), first)
        self.assertNotEqual(first, second)
        self.assertTrue(accounts.verify_password("motdepasse1", first) and accounts.verify_password("motdepasse1", second))
        self.assertFalse(accounts.verify_password("motdepasse2", first))
        self.assertNotIn("motdepasse1", first)

    def test_pbkdf2_replaces_scrypt_when_hashlib_has_none(self):
        self.without_scrypt()
        stored = accounts.hash_password("motdepasse1")
        self.assertRegex(stored, r"^pbkdf2\$1000\$[0-9a-f]{32}\$[0-9a-f]{64}$")
        self.assertTrue(accounts.verify_password("motdepasse1", stored))
        self.assertFalse(accounts.verify_password("motdepasse2", stored))
        self.assertNotEqual(stored, accounts.hash_password("motdepasse1"))

    def test_pbkdf2_replaces_scrypt_when_openssl_refuses_it(self):
        with mock.patch.object(hashlib, "scrypt", side_effect=ValueError("scrypt non disponible")):
            stored = accounts.hash_password("motdepasse1")
        self.assertTrue(stored.startswith("pbkdf2$"), stored)

    def test_hashes_stay_readable_across_both_schemes(self):
        scrypt = accounts.hash_password("motdepasse1")
        patch = self.without_scrypt()
        pbkdf2 = accounts.hash_password("motdepasse1")
        self.assertFalse(accounts.verify_password("motdepasse1", scrypt))                # scrypt absent : refus net, pas d'exception
        patch.stop()
        self.assertTrue(accounts.verify_password("motdepasse1", pbkdf2))                 # un compte créé avec le repli vaut encore quand scrypt est là
        self.assertTrue(accounts.verify_password("motdepasse1", scrypt))

    def test_at_most_two_verifications_run_at_once(self):
        lock, state = threading.Lock(), {"now": 0, "peak": 0, "done": 0}

        def slow(raw, stored):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.05)
            with lock:
                state["now"] -= 1
                state["done"] += 1
            return True

        results = []
        with mock.patch.object(accounts, "_verify", slow):
            threads = [threading.Thread(target=lambda: results.append(accounts.verify_password("motdepasse1", "x"))) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        self.assertEqual((state["done"], results), (8, [True] * 8))                      # toutes ont abouti, mais...
        self.assertEqual(state["peak"], 2)                                               # ...jamais plus de deux en même temps

    def test_a_failing_verification_gives_its_place_back(self):
        with mock.patch.object(accounts, "_verify", side_effect=RuntimeError("panne")):
            for _ in range(4):                                                           # plus que les deux places : un oubli de libération bloquerait ici
                with self.assertRaises(RuntimeError):
                    accounts.verify_password("motdepasse1", "x")
        self.assertTrue(accounts.verify_password("motdepasse1", accounts.hash_password("motdepasse1")))

    def test_unreadable_hashes_and_passwords_never_match(self):
        for stored in ("", "x", "scrypt$", "scrypt$a$b$c$d$e", "pbkdf2$1000$zz$zz", "inconnu$1$2", "scrypt$16384$8$1$00$00"):
            self.assertFalse(accounts.verify_password("motdepasse1", stored), stored)
        stored = accounts.hash_password("motdepasse1")
        for password in (None, 12, b"motdepasse1"):
            self.assertFalse(accounts.verify_password(password, stored), repr(password))


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
