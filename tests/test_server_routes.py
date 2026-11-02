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


if __name__ == "__main__":
    unittest.main()
