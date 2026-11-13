"""L'avis personnel (J'aime / Bof / Je n'aime pas) sur les films et les séries : la route, le champ `rating` des vignettes et de la fiche, le tri « Mon avis », « Pas encore vu » sans ce qu'on n'aime pas, et la confidentialité d'un compte à l'autre."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from tests.rig import MiniHub, call
from tests.test_server_views import entry, item
from hub import accounts, config
from hub.server import app

PASSWORD = "un mot de passe assez long"
ERROR_UNKNOWN_ITEM = {"error": "élément inconnu"}


class RatingCase(unittest.TestCase):
    """Un hub à quatre bibliothèques (films, séries, musique, livres), un administrateur et deux comptes, Alice et Bob."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "web").mkdir()
        libs = [config.Library(ident, ident.title(), kind, self.root / ident) for ident, kind in (("films", "movies"), ("series", "shows"), ("musique", "music"), ("livres", "books"), ("prive", "movies"))]
        for lib in libs:
            lib.path.mkdir()
        self.hub = MiniHub(self.root, libs)
        self.db = self.hub.db
        self.boss = accounts.create(self.db, "boss", PASSWORD)
        self.alice = accounts.create(self.db, "alice", PASSWORD)
        self.bob = accounts.create(self.db, "bob", PASSWORD)
        accounts.set_rights(self.db, self.bob["id"], {lib.id for lib in libs}, {"prive": {"view": False}})
        self.fill("films", [item("m1", lib="films", title="Alpha"), item("m2", lib="films", title="Beta")], [entry("v1", "m1", lib="films"), entry("v2", "m2", lib="films")])
        self.fill("series", [item("s1", "show", lib="series", title="Serie")], [entry("e1", "s1", lib="series", season=1, number=1)])
        self.fill("musique", [item("al", "album", lib="musique", title="Album")], [entry("t1", "al", kind="audio", lib="musique")])
        self.fill("livres", [item("bk", "series", lib="livres", title="Livre")], [entry("k1", "bk", kind="book", lib="livres")])
        self.fill("prive", [item("p1", lib="prive", title="Secret")], [entry("v9", "p1", lib="prive")])

    def fill(self, lib, items, entries):
        self.db.replace_library(lib, items, entries)

    def headers(self, who):
        return self.hub.cookie(accounts.by_id(self.db, who["id"]))

    def get(self, who, target):
        _, reply = call(self.hub, "GET", target, headers=self.headers(who))
        return reply

    def post(self, who, target, body=None):
        _, reply = call(self.hub, "POST", target, {} if body is None else body, self.headers(who))
        return reply

    def rate(self, who, ident, value="up"):
        return self.post(who, f"/api/item/{ident}/rating", {"rating": value})

    def stored(self):
        return [tuple(r) for r in self.db.query("SELECT user_id, item_id, rating FROM ratings ORDER BY user_id, item_id")]

    def ratings_in(self, cards):
        return {c["id"]: c["rating"] for c in cards if "rating" in c}


class TestRatingRoute(RatingCase):
    def test_a_rating_is_set_changed_and_cleared(self):
        reply = self.rate(self.alice, "m1", "up")
        self.assertEqual((reply.status, reply.json()), (200, {"id": "m1", "rating": "up"}))
        self.assertEqual(self.stored(), [(self.alice["id"], "m1", "up")])
        self.assertEqual(self.rate(self.alice, "m1", "down").json(), {"id": "m1", "rating": "down"})
        self.assertEqual(self.stored(), [(self.alice["id"], "m1", "down")])
        self.assertEqual(self.rate(self.alice, "m1", "mid").json(), {"id": "m1", "rating": "mid"})
        reply = self.rate(self.alice, "m1", None)
        self.assertEqual((reply.status, reply.json()), (200, {"id": "m1", "rating": None}))
        self.assertEqual(self.stored(), [])

    def test_a_missing_key_clears_too_and_clearing_nothing_is_fine(self):
        self.rate(self.alice, "m1", "up")
        reply = self.post(self.alice, "/api/item/m1/rating", {})
        self.assertEqual((reply.status, reply.json()), (200, {"id": "m1", "rating": None}))
        self.assertEqual(self.stored(), [])
        self.assertEqual(self.post(self.alice, "/api/item/m1/rating").json(), {"id": "m1", "rating": None})

    def test_a_show_can_be_rated_too(self):
        self.assertEqual(self.rate(self.alice, "s1", "mid").json(), {"id": "s1", "rating": "mid"})

    def test_an_unknown_value_is_refused_and_nothing_changes(self):
        self.rate(self.alice, "m1", "up")
        for value in ("excellent", "UP", "", 5, True, ["up"], {"up": 1}):
            reply = self.rate(self.alice, "m1", value)
            self.assertEqual(reply.status, 400, value)
            self.assertTrue(reply.json()["error"].startswith("avis inconnu : "), value)
        self.assertEqual(self.rate(self.alice, "m1", "excellent").json(), {"error": "avis inconnu : excellent"})
        self.assertEqual(self.stored(), [(self.alice["id"], "m1", "up")])

    def test_an_enormous_unknown_value_is_not_echoed_back_whole(self):
        reply = self.rate(self.alice, "m1", "x" * 5000)
        self.assertEqual(reply.status, 400)
        self.assertLess(len(reply.json()["error"]), 100)

    def test_an_unknown_item_is_a_404(self):
        reply = self.rate(self.alice, "nope")
        self.assertEqual((reply.status, reply.json()), (404, ERROR_UNKNOWN_ITEM))
        self.assertEqual(self.stored(), [])

    def test_a_library_the_account_cannot_see_answers_like_an_unknown_item(self):
        hidden = self.rate(self.bob, "p1")
        self.assertEqual((hidden.status, hidden.json()), (404, ERROR_UNKNOWN_ITEM))
        self.assertEqual(self.rate(self.bob, "p1", None).status, 404)
        self.assertEqual(self.rate(self.bob, "nope").body, hidden.body)
        self.assertEqual(self.stored(), [])
        self.assertEqual(self.rate(self.alice, "p1").status, 200)            # Alice, elle, voit cette bibliothèque

    def test_music_and_books_cannot_be_rated(self):
        for ident in ("al", "bk"):
            for value in ("up", None):
                reply = self.rate(self.alice, ident, value)
                self.assertEqual((reply.status, reply.json()), (400, {"error": "cette bibliothèque ne se note pas"}), (ident, value))
        self.assertEqual(self.stored(), [])

    def test_neither_an_episode_nor_a_saga_has_an_opinion_of_its_own(self):
        self.assertEqual(self.rate(self.alice, "e1").status, 404)            # un fichier n'est pas un élément
        self.fill("films", [item("m1", lib="films", title="Alpha", grp="Saga"), item("m2", lib="films", title="Beta", grp="Saga")], [entry("v1", "m1", lib="films"), entry("v2", "m2", lib="films")])
        saga = next(c for c in self.get(self.alice, "/api/library/films?sagas=1").json()["items"] if c["type"] == "saga")
        self.assertEqual(self.rate(self.alice, saga["id"]).status, 404)      # la vignette d'une saga n'est pas dans la table des éléments
        self.assertEqual(self.stored(), [])

    def test_the_type_of_an_item_decides_not_only_its_library(self):
        self.fill("films", [item("sg", "saga", lib="films", title="Collection")], [])
        reply = self.rate(self.alice, "sg")
        self.assertEqual((reply.status, reply.json()), (400, {"error": "une saga ne se note pas"}))

    def test_the_body_is_limited_like_the_others(self):
        _, reply = call(self.hub, "POST", "/api/item/m1/rating", b"", {**self.headers(self.alice), "Content-Length": str((1 << 20) + 1)})
        self.assertEqual(reply.status, 413)
        self.assertEqual(self.stored(), [])

    def test_without_a_session_it_is_a_401(self):
        _, reply = call(self.hub, "POST", "/api/item/m1/rating", {"rating": "up"})
        self.assertEqual(reply.status, 401)
        self.assertEqual(self.stored(), [])

    def test_a_get_on_the_route_is_not_a_route(self):
        self.assertEqual(self.get(self.alice, "/api/item/m1/rating").status, 404)

    def test_a_rescan_and_a_new_scan_of_the_item_keep_the_rating(self):
        self.rate(self.alice, "m1", "up")
        self.fill("films", [item("m1", lib="films", title="Alpha, rescanné")], [entry("v1", "m1", lib="films")])
        self.assertEqual(self.stored(), [(self.alice["id"], "m1", "up")])
        self.assertEqual(self.get(self.alice, "/api/item/m1").json()["rating"], "up")

    def test_deleting_an_account_deletes_its_ratings_and_only_its_own(self):
        self.rate(self.alice, "m1", "up")
        self.rate(self.bob, "m1", "down")
        accounts.delete(self.db, self.alice["id"])
        self.assertEqual(self.stored(), [(self.bob["id"], "m1", "down")])


class TestRatingIsPrivate(RatingCase):
    def test_an_account_never_reads_the_rating_of_another(self):
        self.rate(self.alice, "m1", "up")
        self.rate(self.alice, "s1", "down")
        self.rate(self.boss, "m2", "mid")
        for target in ("/api/item/m1", "/api/item/s1", "/api/item/m2"):
            self.assertNotIn("rating", self.get(self.bob, target).json(), target)
        self.assertEqual(self.ratings_in(self.get(self.bob, "/api/library/films").json()["items"]), {})
        home = self.get(self.bob, "/api/home").json()
        self.assertEqual([self.ratings_in(row["recent"] + row.get("unseen", [])) for row in home["libraries"]], [{}, {}, {}, {}])
        self.assertEqual(self.ratings_in(self.get(self.bob, "/api/search?q=alpha").json()["results"][0]["items"]), {})
        self.assertEqual(self.get(self.alice, "/api/item/m1").json()["rating"], "up")
        self.assertEqual(self.get(self.boss, "/api/item/m1").json().get("rating"), None)

    def test_two_accounts_rate_the_same_item_differently(self):
        self.rate(self.alice, "m1", "up")
        self.rate(self.bob, "m1", "down")
        self.assertEqual(self.get(self.alice, "/api/item/m1").json()["rating"], "up")
        self.assertEqual(self.get(self.bob, "/api/item/m1").json()["rating"], "down")
        self.rate(self.bob, "m1", None)
        self.assertEqual(self.get(self.alice, "/api/item/m1").json()["rating"], "up")

    def test_no_route_takes_an_account_to_read_from(self):
        self.rate(self.alice, "m1", "up")
        for target in (f"/api/item/m1?user={self.alice['id']}", f"/api/library/films?user={self.alice['id']}&profile={self.alice['id']}"):
            self.assertNotIn('"rating"', json.dumps(self.get(self.bob, target).json()), target)


class TestRatingOnCards(RatingCase):
    def test_the_sheet_of_a_film_and_of_a_show(self):
        self.assertNotIn("rating", self.get(self.alice, "/api/item/m1").json())
        self.rate(self.alice, "m1", "mid")
        self.rate(self.alice, "s1", "up")
        self.assertEqual(self.get(self.alice, "/api/item/m1").json()["rating"], "mid")
        self.assertEqual(self.get(self.alice, "/api/item/s1").json()["rating"], "up")
        self.assertNotIn("rating", self.get(self.alice, "/api/item/m2").json())

    def test_the_library_grid_has_it_only_where_there_is_an_opinion(self):
        self.rate(self.alice, "m1", "down")
        self.assertEqual(self.ratings_in(self.get(self.alice, "/api/library/films").json()["items"]), {"m1": "down"})
        self.assertEqual(self.ratings_in(self.get(self.alice, "/api/library/series").json()["items"]), {})
        self.assertEqual(self.ratings_in(self.get(self.alice, "/api/library/musique").json()["items"]), {})

    def test_the_home_the_search_and_continue(self):
        self.rate(self.alice, "m1", "up")
        self.rate(self.alice, "m2", "mid")
        self.db.save_progress(self.alice["id"], "v2", 50, 100, False)
        home = self.get(self.alice, "/api/home").json()
        films = home["libraries"][0]
        self.assertEqual(self.ratings_in(films["recent"]), {"m1": "up", "m2": "mid"})
        self.assertEqual(self.ratings_in(films["unseen"]), {"m1": "up"})                  # m2, entamé, n'est plus « pas encore vu »
        self.assertEqual([c["item"].get("rating") for c in home["continue"]], ["mid"])
        found = self.get(self.alice, "/api/search?q=alpha").json()["results"][0]["items"]
        self.assertEqual(self.ratings_in(found), {"m1": "up"})

    def test_a_person_and_the_films_of_a_saga(self):
        directed = {"directors": ["Ann Réal"]}
        self.fill("films", [item("m1", lib="films", title="Alpha", grp="Saga", meta={**directed, "tmdb": "movie/11"}), item("m2", lib="films", title="Beta", grp="Saga", meta={**directed, "tmdb": "movie/12"})],
                  [entry("v1", "m1", lib="films"), entry("v2", "m2", lib="films")])
        self.hub.sagas.add("Liste", [11, 12])
        self.rate(self.alice, "m2", "down")
        films = self.get(self.alice, "/api/person?name=" + quote("Ann Réal")).json()["results"][0]["items"]
        self.assertEqual(self.ratings_in(films), {"m2": "down"})
        sheet = self.get(self.alice, "/api/item/m1").json()
        self.assertEqual(self.ratings_in(sheet["saga"]), {"m2": "down"})
        listed = next(s for s in sheet["sagas"] if s["name"] == "Liste")
        self.assertEqual(self.ratings_in(listed["films"]), {"m2": "down"})
        grid = self.get(self.alice, "/api/library/films?group=Saga").json()["items"]
        self.assertEqual(self.ratings_in(grid), {"m2": "down"})
        self.assertEqual(self.ratings_in(self.get(self.bob, "/api/library/films?group=Saga").json()["items"]), {})

    def test_the_ratings_are_read_once_per_request(self):
        directed = {"directors": ["Ann Réal"]}
        self.fill("films", [item(f"m{n}", lib="films", title=f"Film {n}", grp="Saga", meta={**directed, "tmdb": f"movie/{n}"}) for n in range(1, 6)], [entry(f"v{n}", f"m{n}", lib="films") for n in range(1, 6)])
        self.hub.sagas.add("Liste", [1, 2, 3])
        self.db.save_progress(self.alice["id"], "v1", 50, 100, False)
        for n in range(1, 6):
            self.rate(self.alice, f"m{n}", "up")
        for target in ("/api/home", "/api/search?q=film", "/api/library/films", "/api/library/films?sagas=1", "/api/library/films?group=Saga&sort=my-rating", "/api/item/m1", "/api/person?name=" + quote("Ann Réal"),
                       "/api/library/musique"):
            reads = self.reads_of(self.alice, target)
            self.assertLessEqual(reads, 1, target)
        self.assertEqual(self.reads_of(self.alice, "/api/home"), 1)
        self.assertEqual(self.reads_of(self.alice, "/api/item/m1"), 1)
        self.assertEqual(self.reads_of(self.alice, "/api/library/films"), 1)

    def reads_of(self, who, target):
        """Combien de fois la table des avis est lue pendant la requête (la base de ce fil trace tout ce qu'elle exécute)."""
        statements, conn = [], self.db.connection()
        conn.set_trace_callback(statements.append)
        try:
            self.assertEqual(self.get(who, target).status, 200, target)
        finally:
            conn.set_trace_callback(None)
        return sum("ratings" in s for s in statements)


class TestMyRatingSort(RatingCase):
    ORDER = ["b", "a", "c1", "c2", "e", "d", "f"]

    def films(self, order):
        rows = {"a": ("Zulu", "up"), "b": ("Alpha", "up"), "c1": ("Same", "mid"), "c2": ("Same", "mid"), "d": ("Beta", None), "e": ("Aleph", None), "f": ("Omega", "down")}
        self.fill("films", [item(i, lib="films", title=rows[i][0]) for i in order], [entry("v" + i, i, lib="films") for i in order])
        for ident, (_, value) in rows.items():
            if value:
                self.rate(self.alice, ident, value)

    def ids(self, who=None, extra=""):
        return [c["id"] for c in self.get(who or self.alice, "/api/library/films?sort=my-rating" + extra).json()["items"]]

    def test_up_then_mid_then_no_opinion_then_down_by_title_then_by_identifier(self):
        self.films(["a", "b", "c1", "c2", "d", "e", "f"])
        self.assertEqual(self.ids(), self.ORDER)

    def test_the_order_does_not_depend_on_how_the_base_hands_the_rows_back(self):
        self.films(["f", "e", "d", "c2", "c1", "b", "a"])
        self.assertEqual(self.ids(), self.ORDER)
        self.films(["c2", "a", "f", "c1", "d", "b", "e"])
        self.assertEqual(self.ids(), self.ORDER)

    def test_it_is_the_order_of_the_account_that_asks(self):
        self.films(["a", "b", "c1", "c2", "d", "e", "f"])
        self.assertEqual(self.ids(self.bob), ["e", "b", "d", "f", "c1", "c2", "a"])            # Bob n'a rien noté : l'ordre des titres

    def test_a_show_library_sorts_too(self):
        self.fill("series", [item("s1", "show", lib="series", title="Zed"), item("s2", "show", lib="series", title="Abe")], [entry("e1", "s1", lib="series"), entry("e2", "s2", lib="series")])
        self.rate(self.alice, "s1", "up")
        self.assertEqual([c["id"] for c in self.get(self.alice, "/api/library/series?sort=my-rating").json()["items"]], ["s1", "s2"])

    def test_the_sort_goes_with_the_filters_and_the_pages(self):
        self.films(["a", "b", "c1", "c2", "d", "e", "f"])
        self.assertEqual(self.ids(extra="&limit=3&offset=2"), ["c1", "c2", "e"])
        self.assertEqual(self.ids(extra="&filter=todo"), self.ORDER)

    def test_an_unknown_sort_and_a_library_without_opinions_keep_today_s_order(self):
        self.films(["a", "b", "c1", "c2", "d", "e", "f"])
        by_title = [c["id"] for c in self.get(self.alice, "/api/library/films?sort=title").json()["items"]]
        self.assertEqual([c["id"] for c in self.get(self.alice, "/api/library/films?sort=opinion").json()["items"]], by_title)
        self.assertEqual([c["id"] for c in self.get(self.alice, "/api/library/musique?sort=my-rating").json()["items"]], [c["id"] for c in self.get(self.alice, "/api/library/musique").json()["items"]])
        self.assertEqual(self.get(self.alice, "/api/library/livres?sort=my-rating").status, 200)

    def test_a_saga_card_counts_as_without_opinion(self):
        self.fill("films", [item("m1", lib="films", title="Alpha", grp="Saga"), item("m2", lib="films", title="Beta", grp="Saga"), item("m3", lib="films", title="Gamma"), item("m4", lib="films", title="Delta")],
                  [entry(f"v{n}", f"m{n}", lib="films") for n in range(1, 5)])
        self.rate(self.alice, "m3", "down")
        self.rate(self.alice, "m4", "up")
        cards = self.get(self.alice, "/api/library/films?sagas=1&sort=my-rating").json()["items"]
        self.assertEqual([c["title"] for c in cards], ["Delta", "Saga", "Gamma"])


class TestUnseenWithoutWhatIsNotLiked(RatingCase):
    def ids(self, rows):
        return [r["id"] for r in rows]

    def test_a_film_that_is_not_liked_is_not_offered_again_but_stays_everywhere_else(self):
        self.rate(self.alice, "m1", "down")
        home = self.get(self.alice, "/api/home").json()["libraries"][0]
        self.assertEqual(self.ids(home["unseen"]), ["m2"])
        self.assertEqual(sorted(self.ids(home["recent"])), ["m1", "m2"])
        self.assertEqual(sorted(self.ids(self.get(self.alice, "/api/library/films").json()["items"])), ["m1", "m2"])
        self.assertEqual(sorted(self.ids(self.get(self.alice, "/api/library/films?filter=todo").json()["items"])), ["m1", "m2"])
        self.assertEqual(self.ids(self.get(self.alice, "/api/search?q=alpha").json()["results"][0]["items"]), ["m1"])

    def test_up_mid_and_no_opinion_are_offered_and_so_is_it_for_the_others(self):
        self.rate(self.alice, "m1", "up")
        self.rate(self.alice, "m2", "mid")
        self.assertEqual(sorted(self.ids(self.get(self.alice, "/api/home").json()["libraries"][0]["unseen"])), ["m1", "m2"])
        self.rate(self.bob, "m1", "down")
        self.assertEqual(sorted(self.ids(self.get(self.alice, "/api/home").json()["libraries"][0]["unseen"])), ["m1", "m2"])
        self.assertEqual(self.ids(self.get(self.bob, "/api/home").json()["libraries"][0]["unseen"]), ["m2"])

    def test_a_show_that_is_not_liked_leaves_the_shelf_too_and_clearing_brings_it_back(self):
        self.rate(self.alice, "s1", "down")
        self.assertEqual(self.get(self.alice, "/api/home").json()["libraries"][1]["unseen"], [])
        self.rate(self.alice, "s1", None)
        self.assertEqual(self.ids(self.get(self.alice, "/api/home").json()["libraries"][1]["unseen"]), ["s1"])

    def test_the_cut_to_n_is_made_after_the_removal(self):
        rows = [item(f"i{n}", title=f"T{n}") for n in range(5)]
        out = app.unseen_row(rows, {}, "p", "2026-11-04", 3, "added", ratings={"i0": "down", "i1": "down"})
        self.assertEqual(len(out), 3)
        self.assertEqual({r["id"] for r in out}, {"i2", "i3", "i4"})


class TestRatingHelpers(unittest.TestCase):
    def test_a_summary_carries_the_rating_only_when_given(self):
        self.assertNotIn("rating", app.summary(item("m1")))
        self.assertEqual(app.summary(item("m1"), opinion="mid")["rating"], "mid")
        self.assertNotIn("rating", app.with_seen(item("m1"), {}))
        self.assertNotIn("rating", app.with_seen(item("m1"), {}, ratings={"autre": "up"}))
        self.assertEqual(app.with_seen(item("m1"), {}, ratings={"m1": "down"})["rating"], "down")


if __name__ == "__main__":
    unittest.main()
