"""Les routes de l'API, sans réseau : progression, éléments vus, bibliothèque, recherche, sagas, artistes, images."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.rig import MiniHub, call
from tests.test_server_views import entry, item
from hub import accounts, config, missing, urls
from hub.server import app

ME = accounts.FIRST_ID
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 20


class RouteCase(unittest.TestCase):
    kind = "movies"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "web").mkdir()
        self.lib = config.Library("lib", "Bibliothèque", self.kind, self.root / "lib")
        self.lib.path.mkdir()
        self.hub = MiniHub(self.root, [self.lib])
        self.db = self.hub.db

    def fill(self, items, entries):
        self.db.replace_library("lib", items, entries)

    def get(self, target, **kw):
        return call(self.hub, "GET", target, **kw)[1]

    def post(self, target, body=None, **kw):
        return call(self.hub, "POST", target, {} if body is None else body, **kw)[1]

    def progress(self, entry_id):
        return self.db.progress_for(ME, [entry_id]).get(entry_id)


class TestProgressRoute(RouteCase):
    def setUp(self):
        super().setUp()
        self.fill([item("al", "album"), item("mv"), item("bk", "books")],
                  [entry("a1", "al", kind="audio", duration=100.0), entry("a0", "al", kind="audio", duration=0.0), entry("v1", "mv", duration=100.0),
                   entry("k1", "bk", kind="book", duration=10.0)])

    def send(self, entry_id, **body):
        reply = self.post("/api/progress", {"entry": entry_id, **body})
        return reply.status, reply.json()

    def test_a_track_is_heard_once_it_is_more_than_four_fifths_through(self):
        self.assertEqual(self.send("a1", position=80, duration=100), (200, {"ok": True, "done": False}))
        row = self.progress("a1")
        self.assertEqual((row["position"], row["duration"], row["done"]), (80, 100, 0))
        self.assertEqual(self.send("a1", position=81, duration=100), (200, {"ok": True, "done": True}))
        row = self.progress("a1")
        self.assertEqual((row["position"], row["done"]), (100, 1))

    def test_a_track_far_from_its_end_is_not_heard(self):
        self.assertFalse(self.send("a1", position=50, duration=100)[1]["done"])

    def test_a_player_can_say_it_is_done_whatever_the_position(self):
        self.assertTrue(self.send("a1", position=5, duration=100, done=True)[1]["done"])

    def test_a_track_without_any_duration_is_never_heard_and_does_not_break(self):
        self.assertEqual(self.send("a0", position=5), (200, {"ok": True, "done": False}))

    def test_a_very_short_track_is_heard_too(self):
        self.assertTrue(self.send("a1", position=0.45, duration=0.5)[1]["done"])

    def test_the_duration_of_the_file_stands_in_when_the_player_gives_none(self):
        self.assertTrue(self.send("v1", position=95)[1]["done"])
        self.assertFalse(self.send("v1", position=50)[1]["done"])
        self.assertEqual(self.progress("v1")["duration"], 100.0)

    def test_a_film_is_done_from_ninety_two_percent_and_remembers_the_end(self):
        self.send("v1", position=50, duration=100)
        row = self.progress("v1")
        self.assertEqual((row["position"], row["done"]), (50, 0))
        self.send("v1", position=92, duration=100)
        row = self.progress("v1")
        self.assertEqual((row["position"], row["done"]), (100, 1))

    def test_a_book_keeps_its_page_when_done(self):
        self.send("k1", position=2, duration=10)
        row = self.progress("k1")
        self.assertEqual((row["position"], row["done"]), (2, 0))
        self.assertTrue(self.send("k1", position=9, duration=10)[1]["done"])
        row = self.progress("k1")
        self.assertEqual((row["position"], row["duration"], row["done"]), (9, 10, 1))

    def test_a_bad_request_is_refused(self):
        self.assertEqual(self.send("nope", position=1), (404, {"error": "fichier inconnu"}))
        self.assertEqual(self.send("v1", position="abc")[0], 400)
        self.assertEqual(self.post("/api/progress", {}).status, 404)


class TestProgressNumbers(unittest.TestCase):
    def test_what_the_player_did_not_say_is_zero(self):
        self.assertEqual(app.progress_numbers({}), (0.0, 0.0))
        self.assertEqual(app.progress_numbers({"position": 5}), (5.0, 0.0))
        self.assertEqual(app.progress_numbers({"duration": 7}), (0.0, 7.0))

    def test_a_negative_number_is_zero_and_a_text_number_is_read(self):
        self.assertEqual(app.progress_numbers({"position": -3, "duration": "12.5"}), (0.0, 12.5))


class TestSeenRoutes(RouteCase):
    def setUp(self):
        super().setUp()
        self.fill([item("s1", "show"), item("s2", "show")],
                  [entry("e1", "s1", season=1, number=1, duration=50.0), entry("e2", "s1", season=1, number=2, duration=60.0), entry("e3", "s1", season=2, number=1, duration=70.0),
                   entry("x1", "s2", season=1, number=1)])

    def test_a_whole_item_is_marked_seen_by_default(self):
        reply = self.post("/api/item/s1/seen")
        self.assertEqual((reply.status, reply.json()), (200, {"ok": True, "count": 3}))
        for ident, duration in (("e1", 50.0), ("e2", 60.0), ("e3", 70.0)):
            row = self.progress(ident)
            self.assertEqual((row["position"], row["duration"], row["done"]), (duration, duration, 1), ident)
        self.assertIsNone(self.progress("x1"))

    def test_one_season_only(self):
        self.assertEqual(self.post("/api/item/s1/seen", {"season": 1}).json()["count"], 2)
        self.assertIsNone(self.progress("e3"))
        self.assertEqual(self.post("/api/item/s1/seen", {"season": 0}).json()["count"], 0)

    def test_marking_unseen_removes_the_progress_of_the_account_only(self):
        self.post("/api/item/s1/seen")
        self.db.save_progress("autre", "e1", 5, 50, True)
        reply = self.post("/api/item/s1/seen", {"done": False, "season": 2})
        self.assertEqual(reply.json(), {"ok": True, "count": 1})
        self.assertIsNone(self.progress("e3"))
        self.assertIsNotNone(self.progress("e1"))
        self.post("/api/item/s1/seen", {"done": False})
        self.assertIsNone(self.progress("e1"))
        self.assertIsNotNone(self.db.progress_for("autre", ["e1"]).get("e1"))

    def test_an_unknown_item_is_a_404(self):
        self.assertEqual(self.post("/api/item/nope/seen").status, 404)

    def test_a_file_is_marked_seen_or_unseen(self):
        self.assertEqual(self.post("/api/entry/e1/seen").json(), {"ok": True})
        row = self.progress("e1")
        self.assertEqual((row["position"], row["duration"], row["done"]), (50.0, 50.0, 1))
        self.post("/api/entry/e1/seen", {"done": False})
        self.assertIsNone(self.progress("e1"))
        self.post("/api/entry/e1/seen", {"done": True})
        self.assertIsNotNone(self.progress("e1"))
        self.assertEqual(self.post("/api/entry/nope/seen").status, 404)

    def test_dismissing_remembers_the_last_progress_or_zero(self):
        self.assertEqual(self.post("/api/item/s1/dismiss").json(), {"ok": True})
        self.assertEqual(self.db.kv_get(app.dismiss_key(ME, "s1")), 0)
        self.db.save_progress(ME, "e1", 5, 50, False)
        self.db.execute("UPDATE progress SET updated=1234 WHERE entry_id='e1'")
        self.post("/api/item/s1/dismiss")
        self.assertEqual(self.db.kv_get(app.dismiss_key(ME, "s1")), 1234)
        self.assertEqual(self.post("/api/item/nope/dismiss").status, 404)


class TestPlaylistRoute(RouteCase):
    kind = "music"

    def test_deleting_a_playlist_says_so(self):
        self.fill([item("al", "album")], [entry("a1", "al", kind="audio")])
        pid = self.post("/api/playlists", {"name": "Ma liste", "entries": ["a1"]}).json()["id"]
        reply = self.post(f"/api/playlist/{pid}/delete")
        self.assertEqual((reply.status, reply.json()), (200, {"ok": True}))
        self.assertEqual(self.get("/api/playlists").json(), {"playlists": []})


class TestSearch(RouteCase):
    def setUp(self):
        super().setUp()
        self.fill([item(f"f{n:02d}", title=f"Film {n:02d}") for n in range(45)] + [item("zz", title="Autre", grp="Film saga")], [])

    def search(self, text):
        return self.get("/api/search?q=" + text).json()["results"]

    def test_two_letters_are_enough_and_one_is_not(self):
        self.assertEqual(self.search("f"), [])
        self.assertEqual(len(self.search("fi")), 1)
        self.assertEqual(self.search("zz"), [])

    def test_at_most_forty_items_per_library_in_the_order_of_the_titles(self):
        found, = self.search("film")
        self.assertEqual(len(found["items"]), 40)
        self.assertEqual(found["items"][0]["title"], "Autre")                    # la saga « Film saga » aussi est cherchée
        self.assertEqual((found["id"], found["label"], found["kind"]), ("lib", "Bibliothèque", "movies"))

    def test_exactly_forty_found_are_all_given(self):
        self.db.execute("DELETE FROM items WHERE sort > 'film 38' OR sort = 'autre'")
        found, = self.search("film")
        self.assertEqual(len(found["items"]), 39)
        self.db.execute("INSERT INTO items SELECT 'nouveau', lib, type, 'Film 99', 'film 99', letter, year, summary, genres, grp, meta, added, count FROM items WHERE id='f00'")
        self.assertEqual(len(self.search("film")[0]["items"]), 40)
        self.assertEqual(len(self.search("%20film%20")[0]["items"]), 40)


class TestLibraryRoute(RouteCase):
    def films(self):
        rows = []
        for n in range(1, 8):
            rows.append(item(f"m{n}", title=f"Film {n}", letter=("#" if n == 7 else "A" if n < 5 else "B"), grp="Saga" if n < 4 else "", year=str(2000 + n), genres=json.dumps([f"G{n}", "Commun"] if n < 3 else [f"G{n}"])))
        return rows

    def test_the_letters_go_in_order_with_the_digits_last(self):
        self.fill(self.films(), [])
        out = self.get("/api/library/lib").json()
        self.assertEqual(out["letters"], [["A", 4], ["B", 2], ["#", 1]])                      # par lettre, pas par effectif

    def test_the_genres_go_by_count_then_name_and_no_more_than_sixty(self):
        self.fill(self.films(), [])
        out = self.get("/api/library/lib").json()
        self.assertEqual(out["genres"][:3], [["Commun", 2], ["G1", 1], ["G2", 1]])
        rows = [item(f"g{n}", title=f"Film {n}", genres=json.dumps([f"Genre {n:03d}"])) for n in range(64, -1, -1)]
        self.fill(rows, [])
        out = self.get("/api/library/lib").json()
        self.assertEqual((len(out["genres"]), out["genres"][0], out["genres"][-1]), (60, ["Genre 000", 1], ["Genre 059", 1]))

    def test_a_page_of_the_list(self):
        self.fill(self.films(), [])
        out = self.get("/api/library/lib?offset=2&limit=3").json()
        self.assertEqual((out["total"], out["offset"], [i["title"] for i in out["items"]]), (7, 2, ["Film 3", "Film 4", "Film 5"]))

    def test_the_films_of_a_saga_are_numbered_only_when_asked_in_the_order_of_the_saga(self):
        self.fill(self.films(), [])
        out = self.get("/api/library/lib?group=Saga&sort=part").json()
        self.assertEqual([(i["title"], i["part"]) for i in out["items"]], [("Film 1", 1), ("Film 2", 2), ("Film 3", 3)])
        self.assertTrue(all("part" not in i for i in self.get("/api/library/lib?group=Saga").json()["items"]))
        self.assertTrue(all("part" not in i for i in self.get("/api/library/lib?sort=part").json()["items"]))

    def test_numbers_continue_on_the_next_page(self):
        self.fill(self.films(), [])
        out = self.get("/api/library/lib?group=Saga&sort=part&offset=1&limit=5").json()
        self.assertEqual([i["part"] for i in out["items"]], [2, 3])

    def test_an_unknown_library_is_a_404(self):
        self.assertEqual(self.get("/api/library/nope").status, 404)


class TestSagaRanks(unittest.TestCase):
    def test_the_films_of_a_tagged_saga_are_ranked_from_one_in_release_order(self):
        rows = [item("a", title="A", grp="Saga", year="2002"), item("b", title="B", grp="Saga", year="2001")]
        for row in rows:
            row["meta"] = "{}"
        sagas, home = app.saga_groups(rows, {})
        self.assertEqual([(film["id"], rank) for film, rank in sagas["Saga"]], [("b", 1), ("a", 2)])
        self.assertEqual(home, {"a": "Saga", "b": "Saga"})


class TestFirstScan(RouteCase):
    def test_only_the_libraries_never_scanned_are_scanned_at_startup(self):
        other = config.Library("autre", "Autre", self.kind, self.root / "autre")
        self.hub.cfg.libraries = [self.lib, other]
        self.db.kv_set("scanned:lib", {"at": 1})
        self.hub.first_scan()
        self.hub.jobs.scan.assert_called_once_with(other)


class TestGroupRanks(RouteCase):
    def setUp(self):
        super().setUp()
        self.fill([item("m1", title="Film 1", meta={"tmdb": "movie/1"})], [])
        self.rows = self.db.query("SELECT * FROM items")
        self.custom = {"Saga": [({"id": "m1"}, 1)], "": [({"id": "m1"}, 1)]}

    def test_a_saga_of_the_file_ranks_its_films_by_their_place_in_the_list(self):
        self.assertEqual(app.group_ranks(self.hub, self.rows, self.custom, "Saga", True), ({"m1": 1}, []))

    def test_a_page_that_is_not_films_or_a_group_without_a_name_has_no_ranks(self):
        for movies, group in ((False, "Saga"), (True, ""), (False, "")):
            self.assertEqual(app.group_ranks(self.hub, self.rows, self.custom, group, movies), (None, []), (movies, group))


class TestMovieSagas(RouteCase):
    def setUp(self):
        super().setUp()
        self.fill([item(f"m{n}", title=f"Film {n}", meta={"tmdb": f"movie/{n}"}) for n in range(1, 5)], [])
        (self.root / "data" / "sagas.json").write_text(json.dumps({"sagas": [{"name": "Première", "films": [1, 2]}, {"name": "Seconde", "films": [3, 4]}]}), encoding="utf-8")

    def sagas_of(self, ident):
        row = self.db.one("SELECT * FROM items WHERE id=?", (ident,))
        return app.movie_sagas(self.hub, row, self.lib, ME)

    def test_a_film_belongs_to_the_sagas_of_the_file_that_list_it_and_only_those(self):
        out = self.sagas_of("m1")
        self.assertEqual([s["name"] for s in out["sagas"]], ["Première"])
        self.assertEqual([f["id"] for f in out["sagas"][0]["films"]], ["m1", "m2"])
        self.assertEqual([s["name"] for s in self.sagas_of("m4")["sagas"]], ["Seconde"])

    def test_a_film_in_no_saga_has_none(self):
        self.db.execute("UPDATE items SET meta='{}' WHERE id='m1'")
        self.assertEqual(self.sagas_of("m1"), {})


class ArtistCase(RouteCase):
    kind = "music"

    def setUp(self):
        super().setUp()
        (self.lib.path / "Daft").mkdir()
        genres = {"al1": ["G", "F", "E", "D", "C", "B"], "al2": ["A", "B"], "al3": []}
        albums = [item(ident, "album", title=ident.upper(), grp="Daft Punk", year=str(2000 + n), genres=json.dumps(genres[ident]),
                       meta={"artist_id": "ar1", "artist_mbid": "mb1", "artist_dir": "Daft" if ident == "al1" else ""}) for n, ident in enumerate(("al1", "al2", "al3"))]
        albums.append(item("other", "album", title="Autre", grp="Air", meta={"artist_id": "ar2"}))
        self.fill(albums, [entry("t1", "al1", kind="audio", number=1, duration=30.0), entry("t2", "al1", kind="audio", number=2, duration=0.0), entry("t3", "al2", kind="audio", number=1, duration=12.5)])

    def sheet(self, target="/api/artist/lib/ar1"):
        return self.get(target)


class TestArtistRoutes(ArtistCase):
    def test_the_sheet_gives_the_albums_the_tracks_and_the_totals(self):
        out = self.sheet().json()
        self.assertEqual((out["id"], out["name"], [a["id"] for a in out["albums"]], [t["id"] for t in out["tracks"]]), ("ar1", "Daft Punk", ["al1", "al2", "al3"], ["t1", "t2", "t3"]))
        self.assertEqual((out["duration"], out["library"]), (42.5, {"id": "lib", "label": "Bibliothèque", "kind": "music"}))
        self.assertEqual((out["wanted"], out["photo"]), ([], False))
        self.assertNotIn("image", out)
        self.assertNotIn("info", out)

    def test_the_five_main_genres_go_by_number_of_albums_then_by_name(self):
        self.assertEqual(self.sheet().json()["genres"], ["B", "A", "C", "D", "E"])

    def test_an_artist_without_name_is_unknown(self):
        self.db.execute("UPDATE items SET grp='' WHERE id IN ('al1', 'al2', 'al3')")
        self.assertEqual(self.sheet().json()["name"], "Artiste inconnu")

    def test_the_sheet_kept_by_the_artists_job_is_given(self):
        self.db.kv_set("artist:mb1", {"fetched": "2026-01-01", "info": {"type": "Group", "area": "FR", "begin": "1993", "end": "2021", "wiki": None, "members": []}})
        out = self.sheet().json()
        self.assertIn("Groupe", out["info"]["facts"])
        self.assertEqual([link["label"] for link in out["info"]["links"]], ["MusicBrainz"])

    def test_the_albums_that_are_missing_come_with_their_cover_when_they_have_one(self):
        self.db.kv_set("wanted:lib", {"ar1": [{"key": "k1", "year": "1997", "title": "Homework", "cover": "Daft/k1.jpg"}, {"key": "k2", "year": "", "title": "Autre", "cover": ""}]})
        self.assertEqual(self.sheet().json()["wanted"], [{"year": "1997", "title": "Homework", "cover": "/img/wanted/lib/ar1/k1"}, {"year": "", "title": "Autre", "cover": ""}])

    def test_a_photo_is_announced_with_its_address(self):
        (self.root / "data" / "artists").mkdir()
        (self.root / "data" / "artists" / "ar1.jpg").write_bytes(JPEG)
        out = self.sheet().json()
        self.assertEqual((out["photo"], out["image"]), (True, urls.artist_image("lib", "ar1")))

    def test_the_folder_of_the_artist_is_the_one_the_scan_found_if_it_is_still_there(self):
        rows = [dict(r) for r in self.db.query("SELECT * FROM items WHERE id IN ('al1','al2') ORDER BY id")]
        self.assertEqual(app.artist_home(self.lib, rows), self.lib.path / "Daft")
        rows[0]["meta"] = json.dumps({"artist_dir": "Disparu"})
        self.assertIsNone(app.artist_home(self.lib, rows))
        rows[0]["meta"] = json.dumps({"artist_dir": ""})
        self.assertIsNone(app.artist_home(self.lib, rows))

    def test_an_unknown_artist_or_a_library_that_is_not_music_is_a_404(self):
        self.assertEqual(self.sheet("/api/artist/lib/nope").status, 404)
        self.assertEqual(self.sheet("/api/artist/nope/ar1").status, 404)


class TestArtistPhoto(ArtistCase):
    def test_an_uploaded_photo_is_saved_with_its_crop(self):
        with mock.patch.object(app, "save_artist_photo") as save:
            reply = self.post("/api/artist/lib/ar1/photo?crop=0.1,0.2,0.5,0.5", JPEG)
        self.assertEqual((reply.status, reply.json()), (200, {"ok": True}))
        args = save.call_args.args
        self.assertEqual((args[3], args[4], args[5]), ("ar1", JPEG, (0.1, 0.2, 0.5, 0.5)))

    def test_something_that_is_not_an_image_or_a_bad_crop_is_refused(self):
        self.assertEqual(self.post("/api/artist/lib/ar1/photo", b"du texte").status, 415)
        self.assertEqual(self.post("/api/artist/lib/ar1/photo?crop=2,2,2,2", JPEG).status, 400)

    def test_a_photo_is_cropped_from_the_one_the_artist_has(self):
        reply = self.post("/api/artist/lib/ar1/photo/crop", {"x": 0, "y": 0, "w": 1, "h": 0.5})
        self.assertEqual((reply.status, reply.json()), (404, {"error": "pas de photo à recadrer"}))
        (self.root / "data" / "artists").mkdir()
        (self.root / "data" / "artists" / "ar1.jpg").write_bytes(JPEG)
        with mock.patch.object(app, "save_artist_photo") as save:
            reply = self.post("/api/artist/lib/ar1/photo/crop", {"x": 0, "y": 0.25, "w": 1, "h": 0.5})
        self.assertEqual((reply.status, reply.json()), (200, {"ok": True}))
        self.assertEqual((save.call_args.args[4], save.call_args.args[5]), (JPEG, (0.0, 0.25, 1.0, 0.5)))
        self.assertEqual(self.post("/api/artist/lib/ar1/photo/crop", {"x": 5}).status, 400)


class TestImageRoutes(RouteCase):
    kind = "music"

    def setUp(self):
        super().setUp()
        (self.lib.path / "Daft").mkdir()
        self.fill([item("al1", "album", grp="Daft Punk", meta={"artist_id": "ar1", "artist_dir": "Daft"})], [])
        self.hub.images.file.return_value = (b"IMAGE", "image/jpeg", "cle")
        self.hub.images.get.return_value = (b"IMAGE", "image/jpeg", "cle")

    def width_asked(self, mock_call):
        return mock_call.call_args.args[-1]

    def check_cache(self, target):
        reply = self.get(target)
        self.assertEqual((reply.status, reply.body, reply.headers["etag"], reply.headers["cache-control"], reply.headers["content-type"]),
                         (200, b"IMAGE", '"cle"', "private, no-cache", "image/jpeg"))
        again = self.get(target, headers={"If-None-Match": '"cle"'})
        self.assertEqual((again.status, again.body, again.headers["etag"]), (304, b"", '"cle"'))
        self.assertEqual(self.get(target, headers={"If-None-Match": '"autre"'}).status, 200)
        self.assertEqual(call(self.hub, "HEAD", target)[1].body, b"")

    def test_an_item_or_entry_image_is_served_with_its_stamp(self):
        self.check_cache("/img/item/al1")
        self.hub.images.get.assert_called_with("item", "al1", 0)
        self.get("/img/entry/e1?w=320")
        self.hub.images.get.assert_called_with("entry", "e1", 320)
        self.get("/img/item/al1?w=large")
        self.hub.images.get.assert_called_with("item", "al1", 0)

    def test_an_item_without_image_is_a_404(self):
        self.hub.images.get.return_value = None
        self.assertEqual(self.get("/img/item/al1").json(), {"error": "pas d'image"})

    def test_the_photo_of_an_artist_is_served_from_its_file(self):
        self.assertEqual(self.get("/img/artist/lib/ar1").json(), {"error": "pas de photo"})
        (self.root / "data" / "artists").mkdir()
        photo = self.root / "data" / "artists" / "ar1.jpg"
        photo.write_bytes(JPEG)
        self.check_cache("/img/artist/lib/ar1")
        self.hub.images.file.assert_called_with("artist:lib:ar1", photo, 0)
        self.get("/img/artist/lib/ar1?w=480")
        self.hub.images.file.assert_called_with("artist:lib:ar1", photo, 480)
        self.get("/img/artist/lib/ar1?w=x")
        self.hub.images.file.assert_called_with("artist:lib:ar1", photo, 0)
        self.hub.images.file.return_value = None
        self.assertEqual(self.get("/img/artist/lib/ar1").status, 404)

    def test_the_cover_of_a_missing_album_comes_from_the_folder_of_the_artist(self):
        (self.lib.path / "Daft" / "k1.jpg").write_bytes(JPEG)
        self.db.kv_set("wanted:lib", {"ar1": [{"key": "k1", "year": "1997", "title": "Homework", "cover": "Daft/k1.jpg"}, {"key": "k2", "year": "", "title": "Rien", "cover": ""},
                                              {"key": "k3", "year": "", "title": "Sorti", "cover": "../../hors.jpg"}]})
        self.check_cache("/img/wanted/lib/ar1/k1")
        self.hub.images.file.assert_called_with("wanted:lib:ar1:k1", (self.lib.path / "Daft" / "k1.jpg").resolve(), 0)
        self.get("/img/wanted/lib/ar1/k1?w=160")
        self.hub.images.file.assert_called_with("wanted:lib:ar1:k1", (self.lib.path / "Daft" / "k1.jpg").resolve(), 160)
        self.get("/img/wanted/lib/ar1/k1?w=x")
        self.hub.images.file.assert_called_with("wanted:lib:ar1:k1", (self.lib.path / "Daft" / "k1.jpg").resolve(), 0)
        for key in ("k2", "k3", "k9"):
            self.assertEqual(self.get(f"/img/wanted/lib/ar1/{key}").status, 404, key)
        self.hub.images.file.return_value = None
        self.assertEqual(self.get("/img/wanted/lib/ar1/k1").status, 404)

    def test_the_poster_of_a_missing_film_comes_from_the_data_folder(self):
        self.db.kv_set(missing.KEY, {"films": {"123": {"poster": "film-123.jpg"}, "456": {"poster": ""}}, "collections": {}})
        self.check_cache("/img/film/123")
        expected = self.root / "data" / "tmdb" / missing.poster_name("123")
        self.hub.images.file.assert_called_with("film:123", expected, 0)
        self.get("/img/film/123?w=342")
        self.hub.images.file.assert_called_with("film:123", expected, 342)
        self.get("/img/film/123?w=x")
        self.hub.images.file.assert_called_with("film:123", expected, 0)
        for ident in ("456", "789"):
            self.assertEqual(self.get(f"/img/film/{ident}").json(), {"error": "pas d'affiche"}, ident)
        self.hub.images.file.return_value = None
        self.assertEqual(self.get("/img/film/123").status, 404)


class TestPosterRoutes(RouteCase):
    PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 20

    def setUp(self):
        super().setUp()
        self.fill([item("m1", "movie", title="Film"), item("s1", "show", title="Serie"), item("a1", "album", title="Album")], [])

    def test_a_poster_is_sent_as_the_body_and_kept_with_its_extension(self):
        reply = self.post("/api/item/m1/poster", self.PNG)
        self.assertEqual((reply.status, reply.json()), (200, {"ok": True}))
        self.hub.images.set_poster.assert_called_once_with("m1", self.PNG, ".png")
        self.assertEqual(self.post("/api/item/s1/poster", JPEG).status, 200)
        self.hub.images.set_poster.assert_called_with("s1", JPEG, ".jpg")

    def test_what_is_not_an_image_or_is_too_big_is_refused(self):
        self.assertEqual(self.post("/api/item/m1/poster", b"du texte").status, 415)
        self.assertEqual(self.post("/api/item/m1/poster", b"").status, 415)
        too_big = JPEG + b"x" * app.PHOTO_UPLOAD_MAX
        self.assertEqual(self.post("/api/item/m1/poster", too_big).status, 413)
        self.hub.images.set_poster.assert_not_called()

    def test_only_films_and_shows_have_a_poster_to_change(self):
        reply = self.post("/api/item/a1/poster", self.PNG)
        self.assertEqual((reply.status, "affiche" in reply.json()["error"]), (400, True))
        self.assertEqual(self.post("/api/item/inconnu/poster", self.PNG).status, 404)
        self.assertEqual(self.post("/api/item/a1/poster/delete").status, 400)
        self.hub.images.set_poster.assert_not_called()

    def test_the_original_poster_comes_back(self):
        reply = self.post("/api/item/m1/poster/delete")
        self.assertEqual((reply.status, reply.json()), (200, {"ok": True}))
        self.hub.images.clear_poster.assert_called_once_with("m1")

    def test_the_sheet_says_whether_the_poster_is_a_custom_one(self):
        self.hub.images.has_poster.return_value = True
        self.assertIs(self.get("/api/item/m1").json()["poster_custom"], True)
        self.hub.images.has_poster.return_value = False
        self.assertNotIn("poster_custom", self.get("/api/item/m1").json())
        self.assertNotIn("poster_custom", self.get("/api/item/a1").json())

    def test_an_ordinary_account_cannot_change_a_poster(self):
        accounts.create(self.db, "root", "motdepasse1")
        bob = accounts.create(self.db, "bob", "motdepasse1")
        cookie = self.hub.cookie(bob)
        for target in ("/api/item/m1/poster", "/api/item/m1/poster/delete"):
            self.assertEqual(self.post(target, self.PNG, headers=cookie).status, 403, target)
        self.hub.images.set_poster.assert_not_called()
        self.hub.images.clear_poster.assert_not_called()


class TestTmdbSheetRoutes(RouteCase):
    def setUp(self):
        super().setUp()
        self.fill([item("s1", "show", title="Hercule"), item("m1", "movie", title="Film")], [])
        self.client = mock.Mock()
        self.client.search_tv.return_value = [{"id": 2567, "name": "Hercule", "first_air_date": "1998-08-31", "overview": "Disney", "popularity": 9}] + [{"id": n, "name": f"Autre {n}"} for n in range(20)]
        self.hub.cfg.tmdb_key = "cle"
        real_tmdb_client = app.enrich.tmdb_client
        for patched in (mock.patch.object(app.enrich, "tmdb_client", return_value=(self.client, self.root / "posters")), mock.patch.object(app.enrich, "choose_show")):
            setattr(self, patched.attribute, patched.start())
            self.addCleanup(patched.stop)
        self.choose_show.return_value = ({"name": "Hercule"}, {"year": "1995"})
        self.real_tmdb_client = real_tmdb_client

    def test_the_search_gives_the_series_tmdb_proposes_ten_at_most(self):
        reply = self.get("/api/item/s1/tmdb?q=Hercule")
        self.client.search_tv.assert_called_once_with("Hercule")
        results = reply.json()["results"]
        self.assertEqual((reply.status, len(results)), (200, 10))
        self.assertEqual(results[0], {"tmdb": 2567, "name": "Hercule", "year": "1998", "overview": "Disney"})
        self.assertEqual(results[1], {"tmdb": 0, "name": "Autre 0", "year": "", "overview": ""})

    def test_an_empty_search_is_refused(self):
        for target in ("/api/item/s1/tmdb?q=", "/api/item/s1/tmdb?q=%20%20", "/api/item/s1/tmdb"):
            self.assertEqual(self.get(target).status, 400, target)
        self.client.search_tv.assert_not_called()

    def test_a_number_or_the_address_of_the_sheet_chooses_it(self):
        for given in (4615, "4615", " 4615 ", "https://www.themoviedb.org/tv/4615-hercules-the-legendary-journeys", "https://themoviedb.org/tv/4615", "http://www.themoviedb.org/tv/4615/seasons"):
            self.choose_show.reset_mock()
            reply = self.post("/api/item/s1/tmdb", {"tmdb": given})
            self.assertEqual((reply.status, reply.json()), (200, {"ok": True}), given)
            self.assertEqual(self.choose_show.call_args.args[-1], 4615, given)

    def test_the_choice_is_made_for_the_series_in_its_library_with_the_client_and_the_posters(self):
        self.post("/api/item/s1/tmdb", {"tmdb": 4615})
        client, posters, db, lib, row, show_id = self.choose_show.call_args.args
        self.assertEqual((client, posters, db, lib.id, row["id"], show_id), (self.client, self.root / "posters", self.db, "lib", "s1", 4615))

    def test_what_is_not_a_series_of_tmdb_is_refused(self):
        for given in ("", "Hercule", "https://example.com/tv/4615", "https://www.themoviedb.org/movie/4615", "movie/4615", -4, None, [4615], {}):
            self.assertEqual(self.post("/api/item/s1/tmdb", {"tmdb": given}).status, 400, given)
        self.assertEqual(self.post("/api/item/s1/tmdb", {}).status, 400)
        self.choose_show.assert_not_called()

    def test_only_a_series_has_a_sheet_to_choose(self):
        self.assertEqual(self.post("/api/item/m1/tmdb", {"tmdb": 1}).status, 400)
        self.assertEqual(self.get("/api/item/m1/tmdb?q=x").status, 400)
        self.assertEqual(self.post("/api/item/inconnu/tmdb", {"tmdb": 1}).status, 404)
        self.choose_show.assert_not_called()

    def test_without_a_tmdb_key_nothing_is_possible(self):
        self.hub.cfg.tmdb_key = ""
        self.tmdb_client.side_effect = self.real_tmdb_client
        for reply in (self.get("/api/item/s1/tmdb?q=x"), self.post("/api/item/s1/tmdb", {"tmdb": 1})):
            self.assertEqual((reply.status, "TMDB_KEY" in reply.json()["error"]), (400, True))
        self.choose_show.assert_not_called()

    def test_what_tmdb_answers_badly_is_a_gateway_error(self):
        for failure in (app.tmdb.TmdbError("HTTP 500"), app.tmdb.TmdbAuthError("clé refusée")):
            self.choose_show.side_effect = failure
            self.client.search_tv.side_effect = failure
            for reply in (self.post("/api/item/s1/tmdb", {"tmdb": 1}), self.get("/api/item/s1/tmdb?q=x")):
                self.assertEqual((reply.status, str(failure) in reply.json()["error"]), (502, True))

    def test_the_doubts_of_the_search_reach_the_sheet(self):
        self.db.update_item_meta("s1", meta={"doubts": ["titre partagé avec Hercule (1995)"]})
        self.assertEqual(self.get("/api/item/s1").json()["doubts"], ["titre partagé avec Hercule (1995)"])
        self.db.update_item_meta("s1", meta={"doubts": []})
        self.assertNotIn("doubts", self.get("/api/item/s1").json())

    def test_an_ordinary_account_cannot_choose_a_sheet(self):
        accounts.create(self.db, "root", "motdepasse1")
        bob = accounts.create(self.db, "bob", "motdepasse1")
        cookie = self.hub.cookie(bob)
        self.assertEqual(self.get("/api/item/s1/tmdb?q=x", headers=cookie).status, 403)
        self.assertEqual(self.post("/api/item/s1/tmdb", {"tmdb": 1}, headers=cookie).status, 403)
        self.choose_show.assert_not_called()
        self.client.search_tv.assert_not_called()


class TestSettingsRoutes(RouteCase):
    def setUp(self):
        super().setUp()
        self.alice = accounts.create(self.db, "alice", "motdepasse1")
        self.bob = accounts.create(self.db, "bob", "motdepasse1")

    def as_(self, who):
        return self.hub.cookie(who)

    def test_the_settings_of_an_account_start_at_the_defaults(self):
        self.assertEqual(self.get("/api/me/settings", headers=self.as_(self.bob)).json(), {"settings": {"music_view": "albums", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show"}})

    def test_a_choice_is_kept_for_the_account_that_made_it(self):
        reply = self.post("/api/me/settings", {"music_view": "artists"}, headers=self.as_(self.bob))
        self.assertEqual((reply.status, reply.json()), (200, {"settings": {"music_view": "artists", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show"}}))
        self.assertEqual(self.get("/api/me/settings", headers=self.as_(self.bob)).json()["settings"]["music_view"], "artists")
        self.assertEqual(self.get("/api/me/settings", headers=self.as_(self.alice)).json()["settings"]["music_view"], "albums")

    def test_the_state_carries_the_settings_so_the_page_needs_no_second_call(self):
        self.post("/api/me/settings", {"music_view": "artists"}, headers=self.as_(self.bob))
        self.assertEqual(self.get("/api/state", headers=self.as_(self.bob)).json()["settings"], {"music_view": "artists", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show"})
        self.assertEqual(self.get("/api/state", headers=self.as_(self.alice)).json()["settings"], {"music_view": "albums", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show"})

    def test_a_wrong_setting_is_a_400_with_the_reason(self):
        for body, reason in (({"couleur": "rouge"}, "réglage inconnu"), ({"music_view": "mosaique"}, "valeur inconnue"), ({}, "aucun réglage")):
            reply = self.post("/api/me/settings", body, headers=self.as_(self.bob))
            self.assertEqual(reply.status, 400, body)
            self.assertIn(reason, reply.json()["error"])
        self.assertEqual(self.get("/api/me/settings", headers=self.as_(self.bob)).json()["settings"]["music_view"], "albums")

    def test_an_open_server_has_one_profile_with_its_own_settings(self):
        (self.root / "ouvert").mkdir()
        (self.root / "ouvert" / "web").mkdir()
        hub = MiniHub(self.root / "ouvert", [])
        self.assertEqual(call(hub, "POST", "/api/me/settings", {"music_view": "artists"})[1].json()["settings"]["music_view"], "artists")
        self.assertEqual(call(hub, "GET", "/api/state")[1].json()["settings"]["music_view"], "artists")


class TestArtistsView(RouteCase):
    kind = "music"

    def setUp(self):
        super().setUp()
        albums = [item("d1", "album", title="Discovery", grp="Daft Punk", year="2001", genres=json.dumps(["Electro", "House"]), meta={"artist_id": "ar1"}),
                  item("d2", "album", title="Homework", grp="Daft Punk", year="1997", genres=json.dumps(["Electro"]), meta={"artist_id": "ar1"}),
                  item("a1", "album", title="Moon Safari", grp="Air", year="1998", genres=json.dumps(["Pop"]), meta={"artist_id": "ar2"}),
                  item("v1", "album", title="Diablo III Soundtrack", grp="Various Artists", year="2012", meta={"artist_id": "ar3"}),
                  item("u1", "album", title="Sans nom", grp="Artiste inconnu", meta={"artist_id": "ar4"}),
                  item("z1", "album", title="Zéro", grp="Zaz", year="2010", meta={"artist_id": "ar5"})]
        self.fill(albums, [])

    def artists(self, query=""):
        return self.get("/api/library/lib?view=artists" + query).json()

    def test_the_artists_come_in_order_with_the_unknown_ones_last_and_the_albums_counted(self):
        out = self.artists()
        self.assertEqual([(a["title"], a["n"]) for a in out["items"]], [("Air", 1), ("Daft Punk", 2), ("Zaz", 1), ("Artiste inconnu", 1), ("Artistes divers", 1)])
        self.assertEqual(out["total"], 5)
        self.assertEqual({a["id"] for a in out["items"]}, {"ar1", "ar2", "ar3", "ar4", "ar5"})

    def test_an_artist_card_gives_the_oldest_album_for_the_cover_and_no_album_list(self):
        card = {a["id"]: a for a in self.artists()["items"]}["ar1"]
        self.assertEqual((card["type"], card["album"], card["title"], card["lib"]), ("artist", "d2", "Daft Punk", "lib"))
        self.assertNotIn("albums", card)

    def test_various_artists_is_said_in_french_without_changing_its_page(self):
        various = {a["id"]: a for a in self.artists()["items"]}["ar3"]
        self.assertEqual(various["title"], "Artistes divers")

    def test_the_letters_and_the_genres_count_the_artists_not_the_albums(self):
        out = self.artists()
        self.assertEqual(dict(out["letters"]), {"A": 3, "D": 1, "Z": 1})
        self.assertEqual(dict(out["genres"]), {"Electro": 1, "House": 1, "Pop": 1})

    def test_the_filters_of_the_library_work_on_artists(self):
        self.assertEqual([a["title"] for a in self.artists("&letter=D")["items"]], ["Daft Punk"])
        self.assertEqual([a["title"] for a in self.artists("&genre=Electro")["items"]], ["Daft Punk"])
        self.assertEqual([a["title"] for a in self.artists("&q=zaz")["items"]], ["Zaz"])

    def test_an_album_title_finds_its_artist(self):
        self.assertEqual([a["title"] for a in self.artists("&q=homework")["items"]], ["Daft Punk"])
        self.assertEqual([a["title"] for a in self.artists("&q=diablo")["items"]], ["Artistes divers"])

    def test_the_artists_are_paged_like_the_albums(self):
        out = self.artists("&limit=2&offset=1")
        self.assertEqual(([a["title"] for a in out["items"]], out["total"], out["offset"]), (["Daft Punk", "Zaz"], 5, 1))

    def test_without_the_view_the_albums_stay_and_another_kind_of_library_ignores_it(self):
        self.assertEqual(len(self.get("/api/library/lib").json()["items"]), 6)
        self.assertEqual(self.get("/api/library/lib?view=albums").json()["items"][0]["type"], "album")


class TestHomeUnseen(RouteCase):
    """« Pas encore vu » à l'accueil : des films et des séries que le compte n'a pas touchés, dans un ordre qui change chaque jour."""

    def setUp(self):
        super().setUp()
        self.fill([item(f"mv{n:02d}", title=f"Film {n:02d}", added=1000 + n) for n in range(30)], [entry(f"v{n:02d}", f"mv{n:02d}", duration=100.0) for n in range(30)])

    def unseen(self, day="2026-11-04"):
        with mock.patch("hub.server.app.today", return_value=day):
            return [r["id"] for r in self.get("/api/home").json()["unseen"]]

    def test_each_film_offered_names_its_library_since_it_is_not_in_a_list_of_its_own(self):
        offered = self.get("/api/home").json()["unseen"]
        self.assertEqual({r["lib"] for r in offered}, {"lib"})

    def test_it_offers_at_most_twenty_four_films_the_account_has_not_started(self):
        ids = self.unseen()
        self.assertEqual((len(ids), len(set(ids))), (24, 24))
        self.assertTrue(all(i.startswith("mv") for i in ids))

    def test_a_film_that_was_started_or_seen_is_not_offered_any_more(self):
        before = self.unseen()
        started, seen = before[0], before[1]
        self.assertEqual(self.post("/api/progress", {"entry": "v" + started[2:], "position": 10, "duration": 100}).status, 200)
        self.assertEqual(self.post("/api/progress", {"entry": "v" + seen[2:], "position": 99, "duration": 100}).status, 200)
        after = self.unseen()
        self.assertNotIn(started, after)
        self.assertNotIn(seen, after)
        self.assertEqual(len(after), 24)                                                              # la place laissée est reprise par un autre film
        self.assertEqual(after[:22], [i for i in before[2:] if i in after][:22])                      # le reste garde son ordre

    def test_the_order_is_the_same_all_day_and_changes_from_one_day_to_the_next(self):
        self.assertEqual(self.unseen("2026-11-04"), self.unseen("2026-11-04"))
        self.assertNotEqual(self.unseen("2026-11-04"), self.unseen("2026-11-05"))

    def test_a_library_that_is_all_started_offers_nothing(self):
        for n in range(30):
            self.post("/api/progress", {"entry": f"v{n:02d}", "position": 5, "duration": 100})
        self.assertEqual(self.unseen(), [])


class TestHomeUnseenOtherKinds(RouteCase):
    kind = "music"

    def test_music_is_not_something_one_has_not_seen_yet(self):
        self.fill([item("al1", "album", title="Un album"), item("al2", "album", title="Un autre")], [entry("a1", "al1", kind="audio", duration=100.0), entry("a2", "al2", kind="audio", duration=100.0)])
        self.assertEqual(self.get("/api/home").json()["unseen"], [])


class TestHomeMusic(RouteCase):
    """Les nouveautés de la musique à l'accueil : un album ajouté à un artiste connu reste un album ; l'arrivée d'un artiste n'est que sa carte."""
    kind = "music"
    DAY = 86400
    T = 1_700_000_000

    def album(self, ident, title, grp, artist, added):
        return item(ident, "album", title=title, grp=grp, year="2020", meta={"artist_id": artist}, added=self.T + added)

    def recent(self):
        return self.get("/api/home").json()["libraries"][0]["recent"]

    def setUp(self):
        super().setUp()
        D = self.DAY
        self.fill([
            self.album("f1", "Echoes", "Foo Fighters", "ar1", -10), self.album("f2", "Wasting Light", "Foo Fighters", "ar1", -D), self.album("f3", "Concrete", "Foo Fighters", "ar1", -2 * D),
            self.album("a1", "Talkie", "Air", "ar2", -20), self.album("a2", "Moon Safari", "Air", "ar2", -200 * D),
            self.album("z1", "Zaz", "Zaz", "ar3", -30),
            self.album("v1", "Compil 1", "Various Artists", "ar4", -40), self.album("v2", "Compil 2", "Various Artists", "ar4", -D - 40),
            self.album("o1", "Perdu", "Orelsan", "ar5", -10 * D), self.album("o2", "Le chant", "Orelsan", "ar5", -17 * D),
            self.album("s1", "Roots", "Skindred", "ar6", -11 * D), self.album("s2", "Big Tings", "Skindred", "ar6", -18 * D - 1),
        ], [])

    def test_an_artist_who_just_arrived_is_one_card_and_an_album_added_to_a_known_artist_stays_an_album(self):
        out = [(r["type"], r["id"]) for r in self.recent()]
        self.assertEqual(out, [("artist", "ar1"), ("album", "a1"), ("album", "z1"), ("album", "v1"), ("album", "v2"), ("artist", "ar5"), ("album", "s1"), ("album", "s2"), ("album", "a2")])

    def test_the_card_of_an_arrived_artist_has_his_name_his_albums_counted_and_the_date_of_the_latest(self):
        card = self.recent()[0]
        self.assertEqual((card["title"], card["n"], card["lib"], card["added"]), ("Foo Fighters", 3, "lib", self.T - 10))
        self.assertEqual(card["album"], "f3")                                  # le plus ancien : sa pochette, faute de photo
        self.assertNotIn("f1", [r["id"] for r in self.recent()])               # ses albums ne sont plus là un par un

    def test_two_albums_a_week_apart_count_as_an_arrival_and_a_second_more_do_not(self):
        types = {r["id"]: r["type"] for r in self.recent()}
        self.assertEqual((types["ar5"], types["s1"], types["s2"]), ("artist", "album", "album"))

    def test_compilations_and_unknown_artists_stay_albums_even_when_they_arrive_together(self):
        self.assertEqual({r["id"]: r["type"] for r in self.recent()}["v1"], "album")
        self.fill([self.album("u1", "Un", "Artiste inconnu", "ar9", -1), self.album("u2", "Deux", "Artiste inconnu", "ar9", -2)], [])
        self.assertEqual([(r["type"], r["id"]) for r in self.recent()], [("album", "u1"), ("album", "u2")])

    def test_a_single_album_artist_stays_an_album_and_the_list_stops_at_24(self):
        self.fill([self.album(f"x{n:02d}", f"Titre {n:02d}", f"Artiste {n:02d}", f"id{n:02d}", -n) for n in range(30)], [])
        out = self.recent()
        self.assertEqual((len(out), {r["type"] for r in out}, out[0]["id"]), (24, {"album"}, "x00"))


if __name__ == "__main__":
    unittest.main()
