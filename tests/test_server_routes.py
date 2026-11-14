"""Les routes de l'API, sans réseau : progression, éléments vus, bibliothèque, recherche, sagas, artistes, images."""

from __future__ import annotations

import json
import shutil
import tempfile
import types
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from tests.fixtures import movie_blocks, write_mkv
from tests.rig import MiniHub, call
from tests.test_server_views import entry, item
from hub import accounts, config, enrich, missing, people, scan, urls
from hub.media import images
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


def probed(width=1920, height=1080, hdr=False, audio=("fre",), subs=()):
    """Un résumé ffprobe, tel que la base le garde."""
    tracks = lambda langs: [{"lang": lang, "commentary": False, "forced": False} for lang in langs]      # noqa: E731
    return json.dumps({"video": {"width": width, "height": height, "hdr": hdr}, "audio": tracks(audio), "subs": tracks(subs)})


BADGES = ("quality", "hdr", "audio", "subs")


class TestCardQuality(RouteCase):
    """La qualité d'image sur la vignette d'un film (grille, accueil, recherche, sagas de la fiche) : celle de son premier fichier sondé ; rien sinon."""

    def setUp(self):
        super().setUp()
        self.fill([item("m1", title="Alpha", meta={"directors": ["Ann Réal"], "tmdb": "movie/11"}), item("m2", title="Beta", meta={"directors": ["Ann Réal"], "tmdb": "movie/12"}), item("m3", title="Gamma", grp="Saga"), item("m4", title="Delta", grp="Saga"), item("s1", "show", title="Serie")],
                  [entry("v1", "m1"), entry("v2", "m2"), entry("v3a", "m3", rel="m3/b.mkv"), entry("v3b", "m3", rel="m3/a.mkv"), entry("v4", "m4"), entry("e1", "s1", season=1, number=1)])
        self.db.set_probe("v1", json.loads(probed(3840, 1600, True)))
        self.db.set_probe("v3a", json.loads(probed(1920, 1080)))
        self.db.set_probe("v3b", json.loads(probed(1280, 720)))                      # a.mkv vient avant b.mkv : c'est lui qui compte
        self.db.set_probe("v4", {"video": None, "audio": [], "subs": []})           # pas d'image : pas de qualité
        self.db.set_probe("e1", json.loads(probed(3840, 2160)))                     # une série : pas sur sa vignette

    def cards(self, items):
        return {c["title"]: {k: c[k] for k in ("quality", "hdr") if k in c} for c in items}

    def test_the_library_grid(self):
        self.assertEqual(self.cards(self.get("/api/library/lib").json()["items"]),
                         {"Alpha": {"quality": "4k", "hdr": True}, "Beta": {}, "Gamma": {"quality": "hd", "hdr": False}, "Delta": {}, "Serie": {}})

    def test_a_file_probed_later_shows_up_at_once(self):
        self.get("/api/library/lib")
        self.db.set_probe("v2", json.loads(probed(720, 576)))                      # lu par le lecteur, ou par la tâche « Analyse des vidéos »
        self.assertEqual(self.cards(self.get("/api/library/lib").json()["items"])["Beta"], {"quality": "sd", "hdr": False})

    def test_the_home_the_search_and_the_sagas_of_a_film(self):
        home = self.get("/api/home").json()["libraries"][0]
        self.assertEqual(self.cards(home["recent"])["Alpha"], {"quality": "4k", "hdr": True})
        self.assertEqual(self.cards(home["unseen"])["Alpha"], {"quality": "4k", "hdr": True})
        found = self.get("/api/search?q=alpha").json()["results"][0]["items"]
        self.assertEqual(self.cards(found), {"Alpha": {"quality": "4k", "hdr": True}})
        saga = self.get("/api/item/m4").json()["saga"]
        self.assertEqual(self.cards(saga), {"Gamma": {"quality": "hd", "hdr": False}, "Delta": {}})
        films = self.get("/api/person?name=" + quote("Ann Réal")).json()["results"][0]["items"]
        self.assertEqual(self.cards(films), {"Alpha": {"quality": "4k", "hdr": True}, "Beta": {}})

    def test_a_saga_of_the_sagas_file_too(self):
        self.hub.sagas.add("Liste", [11, 12])
        listed = next(s for s in self.get("/api/item/m2").json()["sagas"] if s["name"] == "Liste")
        self.assertEqual(self.cards(listed["films"]), {"Alpha": {"quality": "4k", "hdr": True}, "Beta": {}})

    def test_the_saga_cards_have_none(self):
        sagas = [c for c in self.get("/api/library/lib?sagas=1").json()["items"] if c["type"] == "saga"]
        self.assertEqual(len(sagas), 1)
        self.assertNotIn("quality", sagas[0])


class TestItemBadges(RouteCase):
    """Qualité d'image et langues sur la fiche : seulement pour les fichiers déjà sondés."""

    def fill(self, items, entries):
        """Les sondes se posent après coup : replace_library ne garde que celles d'avant."""
        super().fill(items, [{**e, "probe": None} for e in entries])
        for e in entries:
            if e["probe"]:
                self.db.execute("UPDATE entries SET probe=? WHERE id=?", (e["probe"], e["id"]))

    def entries(self, ident):
        reply = self.get(f"/api/item/{ident}")
        self.assertEqual(reply.status, 200)
        return {e["id"]: {k: e[k] for k in BADGES if k in e} for e in reply.json()["entries"]}

    def test_a_film_that_is_not_probed_yet_has_no_badge(self):
        self.fill([item("m1")], [entry("v1", "m1")])
        self.assertEqual(self.entries("m1"), {"v1": {}})
        self.assertNotIn("season_badges", self.get("/api/item/m1").json())

    def test_a_probed_film_says_its_quality_and_its_languages(self):
        self.fill([item("m1")], [entry("v1", "m1", probe=probed(3840, 1600, True, ("eng", "fre"), ("fre",)))])
        self.assertEqual(self.entries("m1"), {"v1": {"quality": "4k", "hdr": True, "audio": ["en", "fr"], "subs": ["fr"]}})

    def test_the_subtitle_files_beside_the_video_count(self):
        self.fill([item("m1")], [entry("v1", "m1", rel="m1/Film.mkv", probe=probed(subs=("fre",))), entry("v2", "m1", rel="m1/Film - bonus.mkv")])
        folder = self.lib.path / "m1"
        folder.mkdir()
        for name in ("Film.en.srt", "Film.es.forced.srt", "Film - bonus.de.srt", "Autre.it.srt"):
            (folder / name).write_text("x", encoding="utf-8")
        found = self.entries("m1")
        self.assertEqual(found["v1"]["subs"], ["fr", "de", "en"])            # « Film - bonus.de.srt » porte aussi le nom de « Film » : comme dans le lecteur
        self.assertEqual(found["v2"], {})                                    # pas sondé : rien, même avec un fichier voisin

    def test_a_file_without_an_image_or_a_track_of_music_has_no_badge(self):
        self.fill([item("m1"), item("al", "album")], [entry("v1", "m1", probe=json.dumps({"video": None, "audio": [], "subs": []})), entry("t1", "al", kind="audio", probe=probed())])
        self.assertEqual((self.entries("m1"), self.entries("al")), ({"v1": {}}, {"t1": {}}))

    def test_a_season_sums_up_its_probed_episodes(self):
        self.fill([item("s1", "show", meta={"seasons": [1, 2, 3]})],
                  [entry("e1", "s1", season=1, number=1, probe=probed(1920, 1080, True, ("jpn", "fre"), ("fre", "eng"))),
                   entry("e2", "s1", season=1, number=2, probe=probed(1920, 800, False, ("fre", "jpn"), ("fre",))),
                   entry("e3", "s1", season=1, number=3, probe=probed(1280, 720, False, ("jpn", "fre", "eng"), ("eng", "fre"))),
                   entry("e4", "s1", season=1, number=4),                                           # pas sondé : ne compte pas
                   entry("e5", "s1", season=2, number=1, probe=probed(3840, 2160, True, ("eng",))),
                   entry("e6", "s1", season=3, number=1),                                           # saison sans épisode sondé : absente
                   entry("e7", "s1", probe=probed(720, 576))])                                      # hors saison
        out = self.get("/api/item/s1").json()
        self.assertEqual(out["seasons"], [1, 2, 3])
        self.assertEqual(out["season_badges"], {"1": {"quality": "fullhd", "hdr": False, "audio": ["ja", "fr"], "subs": ["fr"]},
                                                "2": {"quality": "4k", "hdr": True, "audio": ["en"], "subs": []}})
        self.assertEqual(self.entries("s1")["e7"]["quality"], "sd")

    def test_a_show_without_any_probed_episode_has_no_season_badge(self):
        self.fill([item("s1", "show", meta={"seasons": [1]})], [entry("e1", "s1", season=1, number=1), entry("e2", "s1", probe=probed())])       # e2, sondé, n'est dans aucune saison
        self.assertNotIn("season_badges", self.get("/api/item/s1").json())


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

    def test_the_sixty_most_frequent_genres_are_listed_alphabetically_without_accents_or_case(self):
        self.fill(self.films(), [])
        out = self.get("/api/library/lib").json()
        self.assertEqual(out["genres"], [["Commun", 2], ["G1", 1], ["G2", 1], ["G3", 1], ["G4", 1], ["G5", 1], ["G6", 1], ["G7", 1]])
        rows = [item(f"g{n}", title=f"Film {n}", genres=json.dumps([f"Genre {n:03d}"])) for n in range(64, -1, -1)]
        rows += [item(f"z{n}", title=f"Zèbre {n}", genres=json.dumps(["Zèbre", "électro"] if n < 2 else ["Zèbre"])) for n in range(3)]
        self.fill(rows, [])
        genres = self.get("/api/library/lib").json()["genres"]
        self.assertEqual(len(genres), 60)
        self.assertEqual((genres[0], genres[1], genres[-1]), (["électro", 2], ["Genre 000", 1], ["Zèbre", 3]))          # « électro » vient avant « Genre » : l'accent et la casse ne comptent pas
        self.assertEqual([g[0] for g in genres[1:-1]], [f"Genre {n:03d}" for n in range(58)])                             # la coupure garde les plus fréquents, puis les premiers par nom : Genre 058 à 064 sont écartés

    def test_a_filter_nobody_knows_is_refused_in_french_and_the_old_ones_still_work(self):
        self.fill(self.films(), [])
        reply = self.get("/api/library/lib?filter=autre")
        self.assertEqual((reply.status, reply.json()["error"]), (400, "filtre inconnu : autre"))
        for state in ("", "todo", "progress", "done", "unseen"):                                       # « unseen » : l'ancien nom de « todo », encore dans des favoris
            with self.subTest(filter=state):
                self.assertEqual(self.get(f"/api/library/lib?filter={state}").status, 200)
        self.assertEqual(self.get("/api/library/lib?filter=unseen").json()["total"], self.get("/api/library/lib?filter=todo").json()["total"])

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


class TestMusicCounts(ArtistCase):
    """Le compteur de la musique : les artistes et les albums de tout ce que les filtres laissent, quel que soit le mode."""

    def counts(self, query=""):
        out = self.get("/api/library/lib?" + query).json()
        return out["artists"], out["albums"], out["total"]

    def test_in_album_mode_the_albums_are_the_total_and_the_artists_are_those_of_the_albums(self):
        self.assertEqual(self.counts(), (2, 4, 4))
        self.assertEqual(self.counts("genre=B"), (1, 2, 2))                     # al1 et al2, tous deux de Daft Punk
        self.assertEqual(self.counts("q=autre"), (1, 1, 1))
        self.assertEqual(self.counts("genre=nulle-part"), (0, 0, 0))

    def test_in_artist_mode_the_artists_are_the_total_and_the_albums_are_the_sum_of_theirs(self):
        self.assertEqual(self.counts("view=artists"), (2, 4, 2))
        self.assertEqual(self.counts("view=artists&genre=B"), (1, 3, 1))        # le genre garde l'artiste, avec tous ses albums : c'est la somme de ses cartes
        self.assertEqual(self.counts("view=artists&q=autre"), (1, 1, 1))

    def test_the_counts_cover_the_whole_list_not_the_page(self):
        self.assertEqual(self.counts("limit=1"), (2, 4, 4))
        self.assertEqual(self.counts("view=artists&limit=1"), (2, 4, 2))

    def test_albums_without_an_artist_id_are_grouped_by_their_name(self):
        self.db.execute("UPDATE items SET meta='{}', grp='daft punk' WHERE id IN ('al1', 'al2')")
        self.assertEqual(self.counts(), (3, 4, 4))                              # al3 garde son identifiant : « Daft Punk » en fait deux, comme dans la liste des artistes
        self.assertEqual(self.counts("view=artists"), (3, 4, 3))


class TestNoMusicCountsElsewhere(RouteCase):
    def test_other_libraries_do_not_carry_them(self):
        self.fill([item("m1")], [])
        out = self.get("/api/library/lib").json()
        self.assertTrue("artists" not in out and "albums" not in out)


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


class FakeMusicBrainz:
    """Le client de MusicBrainz et de Wikipédia, sans réseau : une fiche d'artiste avec un site officiel, un réseau social, Wikidata et Wikipédia."""
    def __init__(self, official):
        self.official = official

    def artist(self, mbid):
        urls = [("official homepage", self.official), ("social network", "https://facebook.com/dp"), ("wikidata", "https://www.wikidata.org/wiki/Q1"),
                ("wikipedia", "https://fr.wikipedia.org/wiki/Daft_Punk")]
        return {"id": mbid, "type": "Group", "relations": [{"type": kind, "url": {"resource": url}} for kind, url in urls]}

    def external(self, url):
        if "wikidata" in url:
            return {"entities": {"Q1": {"sitelinks": {"frwiki": {"title": "Daft Punk"}}}}}
        return {"title": "Daft Punk", "extract": "Un groupe.", "content_urls": {"desktop": {"page": "https://fr.wikipedia.org/wiki/Daft_Punk"}}}


class TestArtistLinks(ArtistCase):
    OFFICIAL = "https://daftpunk.example/"
    WIKI = "https://fr.wikipedia.org/wiki/Daft_Punk"
    MB = "https://musicbrainz.org/artist/mb1"

    def setUp(self):
        super().setUp()
        self.db.kv_set("artist:mb1", {"fetched": "2026-01-01", "info": {"type": "Group", "wiki": {"lang": "fr", "title": "Daft Punk", "url": self.WIKI, "extract": ""}, "members": [],
                                                                       "links": [{"label": "Site officiel", "url": self.OFFICIAL}, {"label": "Facebook", "url": "https://facebook.com/dp"}]}})

    def urls(self, target="/api/artist/lib/ar1"):
        return [link["url"] for link in self.sheet(target).json()["info"]["links"]]

    def add(self, url, label=None, **kw):
        return self.post("/api/artist/lib/ar1/links", {"url": url} if label is None else {"url": url, "label": label}, **kw)

    def remove(self, url, **kw):
        return self.post("/api/artist/lib/ar1/links/remove", {"url": url}, **kw)

    def test_an_added_link_comes_before_wikipedia_and_musicbrainz_and_the_label_defaults_to_the_site(self):
        reply = self.add("https://www.facebook.com/daftpunk")
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.json()["links"], [{"label": "Site officiel", "url": self.OFFICIAL}, {"label": "Facebook", "url": "https://www.facebook.com/daftpunk"},
                                                 {"label": "Wikipédia", "url": self.WIKI}, {"label": "MusicBrainz", "url": self.MB}])
        self.assertEqual(self.sheet().json()["info"]["links"], reply.json()["links"])

    def test_a_label_can_be_given(self):
        self.assertEqual(self.add("https://example.org/dp", "  Mon   site ").json()["links"][1], {"label": "Mon site", "url": "https://example.org/dp"})

    def test_any_link_can_be_removed_wikipedia_and_musicbrainz_included(self):
        for url in (self.OFFICIAL, self.WIKI, self.MB):
            reply = self.remove(url)
            self.assertEqual(reply.status, 200)
        self.assertEqual(self.urls(), [])
        self.assertEqual(self.db.kv_get("artistlinks:mb1")["removed"], [self.OFFICIAL, self.WIKI, self.MB])

    def test_a_removed_link_stays_removed_when_the_sheet_is_rebuilt_and_adding_it_again_cancels_the_removal(self):
        self.remove(self.OFFICIAL)
        kept = self.db.kv_get("artist:mb1")
        self.db.kv_set("artist:mb1", {**kept, "fetched": "2026-02-02"})
        self.assertNotIn(self.OFFICIAL, self.urls())
        reply = self.add(self.OFFICIAL)
        self.assertEqual((reply.status, self.OFFICIAL in self.urls()), (200, True))
        self.assertIsNone(self.db.kv_get("artistlinks:mb1"))

    def test_an_added_link_is_forgotten_when_removed(self):
        self.add("https://example.org/dp")
        reply = self.remove("https://example.org/dp")
        self.assertNotIn("https://example.org/dp", [link["url"] for link in reply.json()["links"]])
        self.assertIsNone(self.db.kv_get("artistlinks:mb1"))

    def test_a_link_that_is_not_on_the_sheet_cannot_be_removed(self):
        self.assertEqual(self.remove("https://example.org/ailleurs").status, 404)
        self.assertEqual(self.remove("").status, 400)

    def test_only_web_addresses_are_accepted_once(self):
        for bad in ("javascript:alert(1)", "data:text/html,x", "ftp://example.org/", "example.org", "https://", "http://[::1", "https://example.org/a b", "", "  ", 12, None):
            self.assertEqual(self.add(bad).status, 400, bad)
        self.assertEqual(self.add("https://example.org/" + "x" * 2000).status, 400)
        self.assertEqual(self.add("https://example.org/", "x" * 81).status, 400)
        self.assertEqual(self.add("https://example.org/", 5).status, 400)
        self.assertEqual(self.add(self.OFFICIAL).status, 400)
        self.assertEqual(self.add(self.WIKI).status, 400)
        self.assertEqual(self.add("https://example.org/dp").status, 200)
        self.assertEqual(self.add("https://example.org/dp").status, 400)
        self.assertEqual(self.db.kv_get("artistlinks:mb1")["added"], [{"label": "example.org", "url": "https://example.org/dp"}])

    def test_the_error_is_in_french(self):
        self.assertEqual(self.add("javascript:alert(1)").json(), {"error": "l'adresse doit être un lien web (http:// ou https://)"})

    def test_an_artist_without_a_kept_sheet_or_unknown_is_a_404(self):
        self.db.kv_delete("artist:mb1")
        self.assertEqual(self.add("https://example.org/").status, 404)
        self.assertEqual(self.remove(self.OFFICIAL).status, 404)
        self.assertEqual(self.post("/api/artist/lib/nope/links", {"url": "https://example.org/"}).status, 404)

    def test_an_ordinary_account_cannot_change_the_links(self):
        accounts.create(self.db, "root", "motdepasse12")
        cookie = self.hub.cookie(accounts.create(self.db, "bob", "motdepasse12"))
        self.assertEqual(self.add("https://example.org/", headers=cookie).status, 403)
        self.assertEqual(self.remove(self.OFFICIAL, headers=cookie).status, 403)
        self.assertIsNone(self.db.kv_get("artistlinks:mb1"))

    def test_a_rescan_leaves_them_alone(self):
        self.add("https://example.org/dp")
        self.remove(self.OFFICIAL)
        before = self.db.kv_get("artistlinks:mb1")
        self.fill([item("al1", "album", grp="Daft Punk", meta={"artist_id": "ar1", "artist_mbid": "mb1"})], [])
        self.assertEqual(self.db.kv_get("artistlinks:mb1"), before)

    def test_the_changes_survive_a_real_refresh_of_the_sheet_and_a_rescan(self):
        extra = "https://example.org/dp"
        self.add(extra)
        self.remove(self.OFFICIAL)
        before = self.db.kv_get("artistlinks:mb1")
        self.db.kv_delete("artist:mb1")                              # « Fiches artistes » refait la fiche en entier : MusicBrainz et Wikipédia sont simulés
        with mock.patch.object(enrich, "musicbrainz", return_value=FakeMusicBrainz(self.OFFICIAL)):
            counts = enrich.enrich_artists(types.SimpleNamespace(data=self.root), self.db, self.lib, lambda line: None, force=True)
        self.assertEqual(counts, {"enriched": 1, "failed": 0})
        rebuilt = self.db.kv_get("artist:mb1")["info"]
        self.assertEqual([link["url"] for link in rebuilt["links"]], [self.OFFICIAL])           # la fiche refaite a bien remis le lien retiré...
        self.assertEqual(self.db.kv_get("artistlinks:mb1"), before)
        self.assertEqual(self.urls(), [extra, self.WIKI, self.MB])                              # ...mais on ne le voit pas, et le lien ajouté est toujours là
        self.fill([item("al1", "album", grp="Daft Punk", meta={"artist_id": "ar1", "artist_mbid": "mb1"})], [])
        self.assertEqual(self.urls(), [extra, self.WIKI, self.MB])

    def test_a_link_added_by_hand_then_found_by_a_refresh_is_shown_once_and_removed_in_one_go(self):
        extra = "https://example.org/dp"
        self.add(extra)
        kept = self.db.kv_get("artist:mb1")
        kept["info"]["links"].append({"label": "YouTube", "url": extra})                 # « Fiches artistes » trouve à son tour le lien que l'administrateur avait ajouté
        self.db.kv_set("artist:mb1", kept)
        self.assertEqual(self.urls(), [self.OFFICIAL, extra, self.WIKI, self.MB])
        self.assertEqual(self.add(extra).status, 400)
        reply = self.remove(extra)
        self.assertEqual((reply.status, extra in [link["url"] for link in reply.json()["links"]]), (200, False))
        self.assertNotIn(extra, self.urls())

    def test_an_added_link_is_not_dropped_for_sharing_a_label_with_an_automatic_one(self):
        other = "https://other.example/"
        reply = self.add(other, "Site officiel")                                           # même libellé qu'un lien automatique : pick_links ne touche pas aux ajouts
        self.assertEqual(reply.status, 200)
        self.assertEqual([(link["label"], link["url"]) for link in reply.json()["links"]],
                         [("Site officiel", self.OFFICIAL), ("Site officiel", other), ("Wikipédia", self.WIKI), ("MusicBrainz", self.MB)])
        self.assertEqual(self.urls(), [self.OFFICIAL, other, self.WIKI, self.MB])

    def test_an_added_link_never_runs_a_script(self):
        for bad in ("javascript:alert(1)", " JavaScript:alert(1)", "java	script:alert(1)", "vbscript:x", "//example.org/x"):
            self.assertEqual(self.add(bad).status, 400, bad)
        self.assertEqual(self.add("https://example.org/", "<img src=x onerror=alert(1)>").status, 200)       # le libellé n'est que du texte : l'interface ne l'insère jamais comme HTML
        shown = self.sheet().json()["info"]["links"]
        self.assertTrue(all(link["url"].startswith(("https://", "http://")) for link in shown), shown)
        self.assertEqual([a["label"] for a in self.db.kv_get("artistlinks:mb1")["added"]], ["<img src=x onerror=alert(1)>"])


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
        (self.root / "data" / "tmdb").mkdir()
        (self.root / "data" / "tmdb" / "film-123.jpg").write_bytes(JPEG)
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

    def test_the_poster_of_a_film_that_only_a_person_lists_is_served_too(self):
        """Une affiche posée par « Enrichir » sur la fiche d'une personne (voir enrich_person) : le film n'est pas dans `sagainfo`, le fichier suffit."""
        (self.root / "data" / "tmdb").mkdir()
        (self.root / "data" / "tmdb" / "film-77.jpg").write_bytes(JPEG)
        self.check_cache("/img/film/77")
        self.hub.images.file.assert_called_with("film:77", self.root / "data" / "tmdb" / "film-77.jpg", 0)


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
        self.hub.images.set_poster.assert_not_called()

    WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"x" * 20

    def test_a_webp_poster_is_converted_to_a_jpeg_before_it_is_kept(self):
        with mock.patch.object(app, "to_jpeg", return_value=JPEG) as convert:
            reply = self.post("/api/item/m1/poster", self.WEBP)
        self.assertEqual((reply.status, reply.json()), (200, {"ok": True}))
        convert.assert_called_once_with(self.WEBP, self.hub.cfg.ffmpeg)
        self.hub.images.set_poster.assert_called_once_with("m1", JPEG, ".jpg")

    def test_a_webp_that_cannot_be_read_or_converted_is_refused(self):
        with mock.patch.object(app, "to_jpeg", side_effect=images.ConvertError("image illisible")):
            self.assertEqual(self.post("/api/item/m1/poster", self.WEBP).status, 415)
        with mock.patch.object(app, "to_jpeg", side_effect=images.NoConverter("ni Pillow ni ffmpeg")):
            self.assertEqual(self.post("/api/item/m1/poster", self.WEBP).status, 501)
        self.hub.images.set_poster.assert_not_called()

    @unittest.skipUnless(images.Image is not None and images.can_webp(""), "Pillow sans WebP")
    def test_a_real_webp_comes_out_as_a_jpeg(self):
        import io
        out = io.BytesIO()
        images.Image.new("RGB", (30, 20), "red").save(out, "WEBP")
        self.assertEqual(self.post("/api/item/m1/poster", out.getvalue()).status, 200)
        data, ext = self.hub.images.set_poster.call_args.args[1:]
        self.assertEqual((ext, images.sniff(data), data[:3]), (".jpg", "image/jpeg", JPEG[:3]))

    def test_there_is_no_way_back_to_the_original_poster(self):
        self.assertEqual(self.post("/api/item/m1/poster/delete").status, 404)

    def test_the_sheet_does_not_say_whether_the_poster_was_chosen(self):
        self.assertNotIn("poster_custom", self.get("/api/item/m1").json())

    def test_an_ordinary_account_cannot_change_a_poster(self):
        accounts.create(self.db, "root", "motdepasse12")
        bob = accounts.create(self.db, "bob", "motdepasse12")
        cookie = self.hub.cookie(bob)
        self.assertEqual(self.post("/api/item/m1/poster", self.PNG, headers=cookie).status, 403)
        self.hub.images.set_poster.assert_not_called()


class TestImageRefreshRoute(RouteCase):
    def test_the_library_forgets_its_images_and_says_how_many(self):
        self.hub.images.forget_library.return_value = 12
        reply = self.post("/api/images/refresh", {"lib": "lib"})
        self.assertEqual((reply.status, reply.json()), (200, {"ok": True, "removed": 12}))
        self.hub.images.forget_library.assert_called_once_with("lib")

    def test_an_unknown_or_missing_library_is_a_404(self):
        self.assertEqual(self.post("/api/images/refresh", {"lib": "ailleurs"}).status, 404)
        self.assertEqual(self.post("/api/images/refresh", {}).status, 404)
        self.hub.images.forget_library.assert_not_called()

    def test_an_ordinary_account_cannot_do_it(self):
        accounts.create(self.db, "root", "motdepasse12")
        bob = accounts.create(self.db, "bob", "motdepasse12")
        self.assertEqual(self.post("/api/images/refresh", {"lib": "lib"}, headers=self.hub.cookie(bob)).status, 403)
        self.hub.images.forget_library.assert_not_called()


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
        accounts.create(self.db, "root", "motdepasse12")
        bob = accounts.create(self.db, "bob", "motdepasse12")
        cookie = self.hub.cookie(bob)
        self.assertEqual(self.get("/api/item/s1/tmdb?q=x", headers=cookie).status, 403)
        self.assertEqual(self.post("/api/item/s1/tmdb", {"tmdb": 1}, headers=cookie).status, 403)
        self.choose_show.assert_not_called()
        self.client.search_tv.assert_not_called()


class TestSettingsRoutes(RouteCase):
    def setUp(self):
        super().setUp()
        self.alice = accounts.create(self.db, "alice", "motdepasse12")
        self.bob = accounts.create(self.db, "bob", "motdepasse12")

    def as_(self, who):
        return self.hub.cookie(who)

    def test_the_settings_of_an_account_start_at_the_defaults(self):
        self.assertEqual(self.get("/api/me/settings", headers=self.as_(self.bob)).json(), {"settings": {"music_view": "albums", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show", "home_unseen_count": "12", "home_unseen_sort": "random", "home_requests": "show"}})

    def test_a_choice_is_kept_for_the_account_that_made_it(self):
        reply = self.post("/api/me/settings", {"music_view": "artists"}, headers=self.as_(self.bob))
        self.assertEqual((reply.status, reply.json()), (200, {"settings": {"music_view": "artists", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show", "home_unseen_count": "12", "home_unseen_sort": "random", "home_requests": "show"}}))
        self.assertEqual(self.get("/api/me/settings", headers=self.as_(self.bob)).json()["settings"]["music_view"], "artists")
        self.assertEqual(self.get("/api/me/settings", headers=self.as_(self.alice)).json()["settings"]["music_view"], "albums")

    def test_the_state_carries_the_settings_so_the_page_needs_no_second_call(self):
        self.post("/api/me/settings", {"music_view": "artists"}, headers=self.as_(self.bob))
        self.assertEqual(self.get("/api/state", headers=self.as_(self.bob)).json()["settings"], {"music_view": "artists", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show", "home_unseen_count": "12", "home_unseen_sort": "random", "home_requests": "show"})
        self.assertEqual(self.get("/api/state", headers=self.as_(self.alice)).json()["settings"], {"music_view": "albums", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show", "home_unseen_count": "12", "home_unseen_sort": "random", "home_requests": "show"})

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

    def test_the_genres_count_the_artists_not_the_albums(self):
        out = self.artists()
        self.assertEqual(dict(out["genres"]), {"Electro": 1, "House": 1, "Pop": 1})

    def test_the_filters_of_the_library_work_on_artists(self):
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


class TestLibraryFilterOfMusic(RouteCase):
    """« À voir », « En cours » et « Terminés » n'ont pas de sens pour la musique : on le dit au lieu de rendre toute la liste (les livres gardent le filtre)."""
    kind = "music"

    def test_the_progress_filters_do_not_exist_for_music(self):
        self.fill([item("al1", "album", title="Un album")], [entry("a1", "al1", kind="audio", duration=100.0)])
        for state in ("todo", "progress", "done", "unseen"):
            with self.subTest(filter=state):
                reply = self.get(f"/api/library/lib?filter={state}")
                self.assertEqual((reply.status, reply.json()["error"]), (400, "ce filtre n'existe pas pour la musique"))
        self.assertEqual(self.get("/api/library/lib?filter=").status, 200)
        self.assertEqual(self.get("/api/library/lib").json()["total"], 1)
        self.assertEqual(self.get("/api/library/lib?filter=autre").json()["error"], "filtre inconnu : autre")      # un nom inconnu reste inconnu, musique ou non



class TestLibraryFilterOfBooks(RouteCase):
    kind = "books"

    def test_books_keep_the_filter(self):
        self.fill([item("bk1", "book", title="Un livre")], [entry("k1", "bk1", kind="book", duration=10.0)])
        self.assertEqual([self.get(f"/api/library/lib?filter={state}").json()["total"] for state in ("todo", "progress", "done")], [1, 0, 0])


class TestHomeUnseen(RouteCase):
    """« Pas encore vu » à l'accueil : des films et des séries que le compte n'a pas touchés, dans un ordre qui change chaque jour."""

    def setUp(self):
        super().setUp()
        self.fill([item(f"mv{n:02d}", title=f"Film {n:02d}", added=1000 + n) for n in range(60)], [entry(f"v{n:02d}", f"mv{n:02d}", duration=100.0) for n in range(60)])

    def unseen(self, day="2026-11-04"):
        with mock.patch("hub.server.app.today", return_value=day):
            libs = self.get("/api/home").json()["libraries"]
        return [r["id"] for r in libs[0]["unseen"]]

    def test_the_films_offered_are_in_the_list_of_their_library(self):
        libs = self.get("/api/home").json()
        self.assertNotIn("unseen", libs)
        self.assertEqual([(l["id"], len(l["unseen"])) for l in libs["libraries"]], [("lib", app.UNSEEN)])

    def test_it_offers_at_most_forty_eight_films_the_account_has_not_started(self):
        ids = self.unseen()
        self.assertEqual((len(ids), len(set(ids))), (48, 48))                                         # de quoi remplir le rayon (24 au plus) une fois « Nouveautés » (24) retiré
        self.assertTrue(all(i.startswith("mv") for i in ids))

    def test_a_film_that_was_started_or_seen_is_not_offered_any_more(self):
        before = self.unseen()
        started, seen = before[0], before[1]
        self.assertEqual(self.post("/api/progress", {"entry": "v" + started[2:], "position": 10, "duration": 100}).status, 200)
        self.assertEqual(self.post("/api/progress", {"entry": "v" + seen[2:], "position": 99, "duration": 100}).status, 200)
        after = self.unseen()
        self.assertNotIn(started, after)
        self.assertNotIn(seen, after)
        self.assertEqual(len(after), 48)                                                              # la place laissée est reprise par un autre film
        self.assertEqual(after[:46], [i for i in before[2:] if i in after][:46])                      # le reste garde son ordre

    def test_the_order_is_the_same_all_day_and_changes_from_one_day_to_the_next(self):
        self.assertEqual(self.unseen("2026-11-04"), self.unseen("2026-11-04"))
        self.assertNotEqual(self.unseen("2026-11-04"), self.unseen("2026-11-05"))

    def test_a_library_that_is_all_started_offers_nothing(self):
        for n in range(60):
            self.post("/api/progress", {"entry": f"v{n:02d}", "position": 5, "duration": 100})
        self.assertEqual(self.unseen(), [])


class TestHomeUnseenSort(RouteCase):
    """Le réglage `home_unseen_sort` du compte : l'ordre du rayon « Pas encore vu » se décide sur le serveur, avant la coupe à 48 (sinon le haut du classement n'arriverait jamais au navigateur)."""

    def setUp(self):
        super().setUp()
        rating = lambda n, votes: {"tmdb_rating": {"tmdb": n, "vote": 5 + n / 10, "votes": votes, "at": 0}}
        films = []
        for n in range(60):                                                                           # 0-19 : notés (n/10 + 5), 20-24 : trop peu de votes pour être montrés, 25-59 : sans note
            meta = rating(n, 100) if n < 20 else rating(n, app.MIN_VOTES - 1) if n < 25 else {}
            films.append(item(f"mv{n:02d}", title=f"Film {n:02d}", added=1000 + n * 7 % 60, meta=meta))      # `added` ne suit ni le titre ni la note (7 est premier avec 60 : 60 dates distinctes)
        self.fill(films, [entry(f"v{n:02d}", f"mv{n:02d}", duration=100.0) for n in range(60)])

    def shelf(self, sort=None, day="2026-11-04"):
        if sort:
            self.assertEqual(self.post("/api/me/settings", {"home_unseen_sort": sort}).status, 200)
        with mock.patch("hub.server.app.today", return_value=day):
            return [r["id"] for r in self.get("/api/home").json()["libraries"][0]["unseen"]]

    def test_rating_puts_the_best_rated_first_and_what_has_no_shown_rating_last(self):
        ids = self.shelf("rating")
        self.assertEqual(ids[:20], [f"mv{n:02d}" for n in range(19, -1, -1)])                         # de la meilleure note à la moins bonne
        self.assertEqual(len(ids), app.UNSEEN)
        self.assertTrue(all(int(i[2:]) >= 20 for i in ids[20:]))                                      # puis des films sans note montrée (moins de 25 votes ou aucune)

    def test_the_cut_comes_after_the_sort(self):
        ids = self.shelf("rating")
        self.assertIn("mv19", ids)                                                                    # le mieux noté y est, qu'il tombe ou non dans le tirage du jour
        self.assertNotIn("mv59", ids)
        self.assertEqual(self.shelf("rating", "2026-11-05"), ids)                                     # l'ordre ne dépend plus du jour

    def test_added_puts_the_most_recent_first(self):
        by_added = sorted(range(60), key=lambda n: -(1000 + n * 7 % 60))
        self.assertEqual(self.shelf("added"), [f"mv{n:02d}" for n in by_added[:app.UNSEEN]])

    def test_random_is_the_default_and_keeps_the_daily_order(self):
        default = self.shelf()
        self.assertEqual(self.shelf("random"), default)
        self.assertNotEqual(self.shelf(day="2026-11-05"), default)
        self.assertNotEqual(default, self.shelf("rating"))

    def test_a_film_started_leaves_the_sorted_shelf_and_the_next_one_takes_its_place(self):
        before = self.shelf("rating")
        self.post("/api/progress", {"entry": "v19", "position": 5, "duration": 100})
        after = self.shelf()
        self.assertEqual(after, before[1:] + ["mv48"])                                                # le premier non montré de la fin de classement

    def test_each_account_has_its_own_order(self):
        alice, bob = accounts.create(self.db, "alice", "motdepasse12"), accounts.create(self.db, "bob", "motdepasse12")
        self.post("/api/me/settings", {"home_unseen_sort": "rating"}, headers=self.hub.cookie(alice))
        home = lambda who: [r["id"] for r in self.get("/api/home", headers=self.hub.cookie(who)).json()["libraries"][0]["unseen"]]
        self.assertEqual(home(alice)[0], "mv19")
        self.assertNotEqual(home(bob)[:3], home(alice)[:3])


class TestHomeUnseenBesideLatest(RouteCase):
    """Le navigateur retire du rayon « Pas encore vu » ce que « Nouveautés » montre déjà, puis coupe : si le serveur n'en offrait que 24, dans l'ordre « date d'ajout » (ou « note » quand les mieux notés sont les plus récents), le rayon serait vide."""

    def setUp(self):
        super().setUp()
        rating = lambda n: {"tmdb_rating": {"tmdb": n, "vote": 5 + n / 20, "votes": 100, "at": 0}}               # plus un film est récent, mieux il est noté
        self.fill([item(f"mv{n:02d}", title=f"Film {n:02d}", added=1000 + n, meta=rating(n)) for n in range(60)], [entry(f"v{n:02d}", f"mv{n:02d}", duration=100.0) for n in range(60)])

    def test_what_is_left_once_the_latest_are_removed_is_enough_to_fill_the_shelf_whatever_the_order(self):
        for sort in ("random", "added", "rating"):
            with self.subTest(sort=sort):
                self.assertEqual(self.post("/api/me/settings", {"home_unseen_sort": sort}).status, 200)
                shelf = self.get("/api/home").json()["libraries"][0]
                latest = {r["id"] for r in shelf["recent"]}
                left = [r["id"] for r in shelf["unseen"] if r["id"] not in latest]
                self.assertEqual(len(shelf["recent"]), 24)
                self.assertGreaterEqual(len(left), 12)                                                              # le plus grand rayon par défaut, 12, reste plein


class TestHomeUnseenPerLibrary(RouteCase):
    """Un rayon par bibliothèque vidéo : en ajouter une (de n'importe quel nom) suffit à lui en donner un."""

    def setUp(self):
        super().setUp()
        self.docs = config.Library("docs", "Documentaires", "movies", self.root / "docs")
        self.series = config.Library("series", "Séries", "shows", self.root / "series")
        self.music = config.Library("musique", "Musique", "music", self.root / "musique")
        self.hub.cfg.libraries = [self.lib, self.series, self.docs, self.music]
        self.db.replace_library("lib", [item("mv1", title="Film un")], [entry("v1", "mv1", duration=100.0)])
        self.db.replace_library("series", [item("sr1", "show", lib="series", title="Série un")], [entry("s1", "sr1", lib="series", duration=100.0)])
        self.db.replace_library("docs", [item("dc1", lib="docs", title="Doc un"), item("dc2", lib="docs", title="Doc deux")], [entry("d1", "dc1", lib="docs", duration=100.0), entry("d2", "dc2", lib="docs", duration=100.0)])
        self.db.replace_library("musique", [item("al1", "album", lib="musique", title="Un album")], [entry("a1", "al1", kind="audio", lib="musique", duration=100.0)])

    def shelves(self):
        return {l["id"]: [r["id"] for r in l["unseen"]] for l in self.get("/api/home").json()["libraries"] if "unseen" in l}

    def test_each_video_library_has_its_own_list_and_music_has_none(self):
        shelves = {lib: sorted(ids) for lib, ids in self.shelves().items()}
        self.assertEqual(shelves, {"lib": ["mv1"], "series": ["sr1"], "docs": ["dc1", "dc2"]})

    def test_what_one_account_has_started_leaves_only_its_own_library(self):
        self.post("/api/progress", {"entry": "s1", "position": 5, "duration": 100})
        self.assertEqual(self.shelves()["series"], [])
        self.assertEqual(self.shelves()["lib"], ["mv1"])


class TestHomeUnseenOtherKinds(RouteCase):
    kind = "music"

    def test_music_is_not_something_one_has_not_seen_yet(self):
        self.fill([item("al1", "album", title="Un album"), item("al2", "album", title="Un autre")], [entry("a1", "al1", kind="audio", duration=100.0), entry("a2", "al2", kind="audio", duration=100.0)])
        self.assertNotIn("unseen", self.get("/api/home").json()["libraries"][0])


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


class TestPersonRoute(unittest.TestCase):
    """GET /api/person : ce qu'une personne a réalisé ou joué, dans les bibliothèques que le compte voit."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "web").mkdir()
        self.libs = [config.Library("films", "Films", "movies", self.root / "films"), config.Library("series", "Séries", "shows", self.root / "series"),
                     config.Library("prive", "Privé", "movies", self.root / "prive")]
        for lib in self.libs:
            lib.path.mkdir()
        self.hub = MiniHub(self.root, self.libs)
        self.db = self.hub.db
        cruz = {"directors": ["Pedro Almodóvar"], "actors": [{"name": "Penélope Cruz", "role": "Raimunda"}, {"name": "Chris Evans Jr", "role": ""}]}
        self.db.replace_library("films", [
            item("m1", title="Volver", year="2006", lib="films", meta=cruz),
            item("m2", title="Vicky Cristina Barcelona", year="2008", lib="films", meta={"actors": [{"name": "Penélope Cruz"}, {"name": "Chris Evans"}]}),
            item("m3", title="Sans année", lib="films", meta={"actors": [{"name": "penelope cruz"}]}),
            item("m4", title="Million Dollar Baby", year="2004", lib="films", meta={"directors": ["Clint Eastwood"], "actors": [{"name": "Clint Eastwood"}, {"name": "Clint Eastwood"}]}),
            item("m5", title="Guy D'Artagnan & Fils/Co", year="1999", lib="films", meta={"directors": ["Guy D'Artagnan & Fils/Co"]}),
            item("al", "album", title="Un album", lib="films", meta={"directors": ["Pedro Almodóvar"]}),
        ], [])
        self.db.replace_library("series", [item("s1", "show", title="Une série", year="2020", lib="series", meta={"actors": ["Penélope Cruz", "Chris Evans"]})], [])      # fiche ancienne : des chaînes
        self.db.replace_library("prive", [
            item("p1", title="Secret", year="2015", lib="prive", meta={"directors": ["Pedro Almodóvar"], "actors": [{"name": "Penelope Cruz"}, {"name": "Penelope Cruz"}, {"name": "Seule Ici"}]}),
            item("p2", title="Autre secret", year="2016", lib="prive", meta={"actors": [{"name": "Penelope Cruz"}]}),
        ], [])

    def person(self, name, headers=None):
        reply = call(self.hub, "GET", "/api/person?name=" + quote(name, safe=""), headers=headers)[1]
        return reply.status, reply.json()

    def titles(self, body):
        return [[i["title"] for i in group["items"]] for group in body["results"]]

    def bob(self):
        """Les en-têtes d'un compte ordinaire qui ne voit pas la bibliothèque « prive »."""
        self.admin = self.hub.cookie(accounts.create(self.db, "root", "motdepasse12"))
        bob = accounts.create(self.db, "bob", "motdepasse12")
        accounts.set_rights(self.db, bob["id"], {"films", "series", "prive"}, {"prive": {"view": False}})
        return self.hub.cookie(bob)

    def test_accents_and_case_do_not_matter(self):
        for asked in ("Penelope Cruz", "PENÉLOPE CRUZ", "  penelope   cruz "):
            status, body = self.person(asked)
            self.assertEqual((status, body["name"]), (200, "Penélope Cruz"), asked)
            self.assertEqual(self.titles(body)[0], ["Vicky Cristina Barcelona", "Volver", "Sans année"], asked)

    def test_the_name_is_the_most_frequent_spelling_and_the_first_in_order_on_a_tie(self):
        self.assertEqual(self.person("penelope cruz")[1]["name"], "Penélope Cruz")      # 3 fois, contre « Penelope Cruz » 2 fois et « penelope cruz » 1 fois
        self.assertEqual(people.spelling({"names": {"x": Counter({"b": 1, "a": 1})}}, ["x"]), "a")
        self.assertIsNone(people.spelling({"names": {"x": Counter({"a": 1})}}, ["y"]))

    def test_the_name_must_match_exactly_not_as_a_part(self):
        self.assertEqual(self.titles(self.person("Chris Evans")[1]), [["Vicky Cristina Barcelona"], ["Une série"]])
        self.assertEqual(self.titles(self.person("Chris Evans Jr")[1]), [["Volver"]])
        for part in ("Chris", "Evans", "Chris Evan", "Penelope"):
            self.assertEqual(self.person(part)[1]["results"], [], part)

    def test_a_director_an_actor_and_both_at_once(self):
        self.assertEqual(self.person("Pedro Almodovar")[1]["roles"], {"director": 2, "actor": 0})        # Volver et Secret ; un album n'est ni un film ni une série
        _, body = self.person("Clint Eastwood")
        self.assertEqual(body["roles"], {"director": 1, "actor": 1})
        self.assertEqual(self.titles(body), [["Million Dollar Baby"]])                  # un seul film, listé une fois
        self.assertEqual(self.person("Penelope Cruz")[1]["roles"], {"director": 0, "actor": 6})      # citée deux fois dans Secret : une fois

    def test_a_show_is_found_whatever_the_form_of_its_cast(self):
        shows = [g for g in self.person("Penélope Cruz")[1]["results"] if g["id"] == "series"][0]
        self.assertEqual(([i["title"] for i in shows["items"]], shows["kind"], shows["label"], shows["items"][0]["type"]), (["Une série"], "shows", "Séries", "show"))

    def test_the_libraries_come_in_their_order_and_the_items_from_the_latest_to_the_oldest(self):
        _, body = self.person("Penelope Cruz")
        self.assertEqual([g["id"] for g in body["results"]], ["films", "series", "prive"])
        self.assertEqual(self.titles(body), [["Vicky Cristina Barcelona", "Volver", "Sans année"], ["Une série"], ["Autre secret", "Secret"]])      # sans année en dernier

    def test_an_item_has_the_shape_of_the_search(self):
        found = call(self.hub, "GET", "/api/search?q=volver")[1].json()["results"][0]["items"][0]
        self.assertEqual(self.person("Penelope Cruz")[1]["results"][0]["items"][1], found)

    def test_the_progress_of_the_account_is_shown(self):
        self.db.replace_library("films", [item("m1", title="Volver", year="2006", lib="films", meta={"actors": [{"name": "Penélope Cruz"}]})], [entry("e1", "m1", lib="films")])
        self.db.save_progress(ME, "e1", 100, 100, True)
        self.assertEqual(self.person("Penelope Cruz")[1]["results"][0]["items"][0]["seen"], 1)

    def test_an_account_that_cannot_see_a_library_finds_nothing_of_it(self):
        cookie = self.bob()
        status, body = self.person("Penelope Cruz", cookie)
        self.assertEqual(status, 200)
        self.assertEqual([g["id"] for g in body["results"]], ["films", "series"])
        self.assertEqual(body["roles"], {"director": 0, "actor": 4})
        self.assertNotIn("ecret", json.dumps(body))
        self.assertEqual(self.person("Pedro Almodovar", cookie)[1]["roles"], {"director": 1, "actor": 0})

    def test_what_only_a_hidden_library_knows_answers_as_an_unknown_name(self):
        cookie = self.bob()
        self.assertEqual(self.person("seule ici", cookie), (200, {"name": "seule ici", "roles": {"director": 0, "actor": 0}, "results": [], "enrichable": False}))
        self.assertEqual(self.person("Inconnu Total", cookie), (200, {"name": "Inconnu Total", "roles": {"director": 0, "actor": 0}, "results": [], "enrichable": False}))
        self.assertEqual(self.person("seule ici", self.admin)[1]["results"][0]["items"][0]["title"], "Secret")          # l'administrateur, lui, la voit

    def test_the_spelling_shown_comes_from_the_visible_libraries_only(self):
        self.db.replace_library("prive", [item(f"q{n}", title=f"Privé {n}", lib="prive", meta={"actors": [{"name": "PENELOPE CRUZ"}]}) for n in range(5)], [])
        self.assertEqual(self.person("penelope cruz")[1]["name"], "PENELOPE CRUZ")          # 5 fois contre 3
        self.assertEqual(self.person("penelope cruz", self.bob())[1]["name"], "Penélope Cruz")

    def test_an_unknown_name_is_a_200_with_nothing(self):
        self.assertEqual(self.person("Personne Du Tout"), (200, {"name": "Personne Du Tout", "roles": {"director": 0, "actor": 0}, "results": [], "enrichable": False}))

    def test_a_missing_empty_blank_or_too_long_name_is_a_400(self):
        for target in ("/api/person", "/api/person?name=", "/api/person?name=%20%20"):
            reply = call(self.hub, "GET", target)[1]
            self.assertEqual((reply.status, "nom attendu" in reply.json()["error"]), (400, True), target)
        self.assertEqual(self.person("x" * 201)[0], 400)
        self.assertEqual(self.person("x" * 200)[0], 200)

    def test_names_with_an_ampersand_a_quote_or_a_slash(self):
        for asked in ("Guy D'Artagnan & Fils/Co", "guy d'artagnan & fils/co"):
            status, body = self.person(asked)
            self.assertEqual((status, self.titles(body), body["roles"]["director"]), (200, [["Guy D'Artagnan & Fils/Co"]], 1), asked)
        status, body = self.person("Robert'; DROP TABLE items; --")
        self.assertEqual((status, body["results"]), (200, []))
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM items")["n"], 9)

    def test_an_item_with_odd_names_does_not_break_the_index(self):
        self.db.replace_library("prive", [item("o1", lib="prive", meta={"directors": [None, 3, "  "], "actors": [None, {"role": "x"}, {"name": 4}, "Odd Name"]}),
                                          item("o2", lib="prive"), item("o3", lib="prive")], [])
        self.db.execute("UPDATE items SET meta='[]' WHERE id='o2'")
        self.db.execute("UPDATE items SET meta='pas du json' WHERE id='o3'")
        self.assertEqual(self.titles(self.person("odd name")[1]), [["o1"]])
        self.assertEqual(people.names_in({"directors": "Nolan", "actors": {"name": "X"}}), [])          # pas une liste : ignoré, pas découpé en lettres

    def test_a_person_is_found_after_a_scan_and_after_an_enrichment(self):
        lib = self.libs[0]
        write_mkv(lib.path / "A (2001)" / "A (2001).mkv", movie_blocks("A", date="2001-01-01", directors=["Jane Campion"], actors=["Holly Hunter (Ada)"]))
        scan.scan_library(self.db, lib, log=lambda *a: None)
        self.assertEqual(self.titles(self.person("holly hunter")[1]), [["A"]])
        self.assertEqual(self.person("Jane Campion")[1]["roles"], {"director": 1, "actor": 0})
        self.assertEqual(self.person("Anna Paquin")[1]["results"], [])                  # l'index existe déjà : un film de plus doit le renouveler
        write_mkv(lib.path / "B (2003)" / "B (2003).mkv", movie_blocks("B", date="2003-01-01", actors=["Anna Paquin (Flora)", "Holly Hunter"]))
        scan.scan_library(self.db, lib, log=lambda *a: None)
        self.assertEqual(self.titles(self.person("Anna Paquin")[1]), [["B"]])
        self.assertEqual(self.titles(self.person("Holly Hunter")[1]), [["B", "A"]])
        self.db.update_item_meta("s1", meta={"actors": [{"name": "Anna Paquin", "role": "Sookie"}]})              # un enrichissement TMDB
        self.assertEqual(self.titles(self.person("Anna Paquin")[1]), [["B"], ["Une série"]])
        (lib.path / "B (2003)" / "B (2003).mkv").unlink()
        scan.scan_library(self.db, lib, log=lambda *a: None)
        self.assertEqual(self.titles(self.person("Anna Paquin")[1]), [["Une série"]])                              # un film disparu aussi


class TestPeopleInSearch(unittest.TestCase):
    """GET /api/search : `people`, les personnes dont le nom contient la recherche (8 au plus), avec leur nombre de titres visibles."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "web").mkdir()
        self.libs = [config.Library("films", "Films", "movies", self.root / "films"), config.Library("prive", "Privé", "movies", self.root / "prive")]
        for lib in self.libs:
            lib.path.mkdir()
        self.hub = MiniHub(self.root, self.libs)
        self.db = self.hub.db
        self.db.replace_library("films", [
            item("m1", title="Un", year="2001", lib="films", meta={"directors": ["Jean Reno"], "actors": [{"name": "Jean Reno"}, {"name": "Léa Seydoux"}, {"name": "Jean Dujardin"}]}),
            item("m2", title="Deux", year="2002", lib="films", meta={"actors": [{"name": "Jean Reno"}, {"name": "Jean Dujardin"}]}),
            item("m3", title="Trois", year="2003", lib="films", meta={"actors": [{"name": "Jean Dujardin"}, {"name": "Marie-Jeanne Dupont"}, {"name": "Renaud Capuçon"}]}),
            item("m4", title="Quatre", year="2004", lib="films", meta={"actors": [{"name": "Zoé O'Brien"}]}),
        ], [])
        self.db.replace_library("prive", [
            item("p1", title="Secret", lib="prive", meta={"actors": [{"name": "Seule Ici"}, {"name": "Jean Reno"}, {"name": "Léa Seydoux"}]}),
            item("p2", title="Autre", lib="prive", meta={"actors": [{"name": "Jean Reno"}]}),
        ], [])

    def search(self, text, headers=None):
        reply = call(self.hub, "GET", "/api/search?q=" + quote(text, safe=""), headers=headers)[1]
        self.assertEqual(reply.status, 200)
        return reply.json()

    def names(self, text, headers=None):
        return [(p["name"], p["titles"]) for p in self.search(text, headers)["people"]]

    def bob(self):
        """Les en-têtes d'un compte ordinaire qui ne voit pas la bibliothèque « prive »."""
        self.admin = self.hub.cookie(accounts.create(self.db, "root", "motdepasse12"))
        bob = accounts.create(self.db, "bob", "motdepasse12")
        accounts.set_rights(self.db, bob["id"], {"films", "prive"}, {"prive": {"view": False}})
        return self.hub.cookie(bob)

    def only_films(self, items):
        """Remplace le contenu des deux bibliothèques : `items` dans « films », rien dans « prive »."""
        self.db.replace_library("films", items, [])
        self.db.replace_library("prive", [], [])

    def test_accents_and_case_do_not_matter_and_the_name_shown_keeps_them(self):
        for asked in ("seydoux", "SEYDOUX", "léa", "lea seydoux", "  LEA   SEYDOUX "):
            self.assertEqual(self.names(asked)[0][0], "Léa Seydoux", asked)
        self.assertEqual(self.names("capucon"), [("Renaud Capuçon", 1)])
        self.assertEqual(self.names("o'brien"), [("Zoé O'Brien", 1)])
        self.assertEqual(self.names("zoe o"), [("Zoé O'Brien", 1)])

    def test_the_exact_name_comes_first_then_the_names_that_start_with_it_then_the_rest(self):
        self.only_films([
            item("m1", title="Un", lib="films", meta={"actors": [{"name": "Reno"}, {"name": "Renoir"}, {"name": "Jean Reno"}, {"name": "Jean Renoir"}, {"name": "Brenoit"}]}),
            item("m2", title="Deux", lib="films", meta={"actors": [{"name": "Jean Reno"}, {"name": "Jean Renoir"}, {"name": "Brenoit"}]}),
            item("m3", title="Trois", lib="films", meta={"actors": [{"name": "Jean Reno"}, {"name": "Brenoit"}]}),
        ])
        # Reno : exact ; Renoir (1 titre) : commence par le texte, même avec moins de titres que les suivants ; puis le reste, du plus de titres au moins (3, 3, 2), à égalité par nom
        self.assertEqual(self.names("reno"), [("Reno", 1), ("Renoir", 1), ("Brenoit", 3), ("Jean Reno", 3), ("Jean Renoir", 2)])

    def test_the_names_that_start_with_the_text_go_by_number_of_titles_then_the_others(self):
        self.assertEqual(self.names("jean"), [("Jean Reno", 4), ("Jean Dujardin", 3), ("Marie-Jeanne Dupont", 1)])
        self.assertEqual(self.names("ren")[0], ("Renaud Capuçon", 1))               # commence par « ren » : avant « Jean Reno », qui le contient seulement

    def test_the_same_number_of_titles_goes_by_name(self):
        self.only_films([item("m1", title="Un", lib="films", meta={"actors": [{"name": "Bob Bb"}, {"name": "Bob Aa"}, {"name": "Bob Cc"}]}),
                         item("m2", title="Deux", lib="films", meta={"actors": [{"name": "Bob Cc"}]})])
        self.assertEqual(self.names("bob"), [("Bob Cc", 2), ("Bob Aa", 1), ("Bob Bb", 1)])

    def test_the_titles_count_once_whatever_the_roles_and_match_the_person_route(self):
        found = dict(self.names("jean"))
        self.assertEqual(found["Jean Reno"], 4)             # réalisateur et acteur du même film : un titre ; plus deux titres de « Privé »
        for name, titles in found.items():
            person = call(self.hub, "GET", "/api/person?name=" + quote(name, safe=""))[1].json()
            self.assertEqual(titles, sum(len(group["items"]) for group in person["results"]), name)

    def test_at_most_eight_people_the_first_in_order(self):
        self.only_films([item("m1", title="Un", lib="films", meta={"actors": [{"name": f"Martin {chr(97 + n)}"} for n in range(12)]})])
        self.assertEqual([n for n, _ in self.names("martin")], [f"Martin {chr(97 + n)}" for n in range(8)])

    def test_a_person_with_no_visible_title_is_not_listed_and_the_count_is_the_visible_one(self):
        cookie = self.bob()
        self.assertEqual(self.names("seule ici", cookie), [])
        self.assertEqual(self.names("seule ici", self.admin), [("Seule Ici", 1)])
        self.assertEqual(self.names("jean reno", cookie), [("Jean Reno", 2)])
        self.assertEqual(self.names("jean reno", self.admin), [("Jean Reno", 4)])
        self.assertEqual(self.names("seydoux", cookie), [("Léa Seydoux", 1)])
        self.assertEqual(self.names("seydoux", self.admin), [("Léa Seydoux", 2)])
        self.assertNotIn("Seule", json.dumps(self.search("seule", cookie)))

    def test_a_search_that_is_too_short_has_no_people(self):
        for text in ("", " ", "j", " j "):
            self.assertEqual(self.search(text), {"results": [], "people": []}, repr(text))
        self.assertTrue(self.search("je")["people"])

    def test_the_results_are_unchanged_and_the_people_do_not_depend_on_them(self):
        body = self.search("deux")
        self.assertEqual(([g["id"] for g in body["results"]], [i["title"] for i in body["results"][0]["items"]], body["people"]), (["films"], ["Deux"], []))
        body = self.search("seydoux")
        self.assertEqual((body["results"], len(body["people"])), ([], 1))              # une personne trouvée sans aucun titre qui porte ce texte

    def test_a_search_with_signs_is_not_injected(self):
        body = self.search("'; DROP TABLE items; --")
        self.assertEqual((body["results"], body["people"]), ([], []))
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM items")["n"], 6)

    def test_the_index_is_renewed_after_a_write(self):
        self.assertEqual(self.names("anna"), [])
        self.db.update_item_meta("m4", meta={"actors": [{"name": "Anna Paquin"}]})
        self.assertEqual(self.names("anna"), [("Anna Paquin", 1)])


if __name__ == "__main__":
    unittest.main()
