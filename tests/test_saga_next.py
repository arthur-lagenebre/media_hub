"""`next` du plan de lecture : à la fin d'un film d'une saga, le film suivant de cette saga."""

from __future__ import annotations

import json
import unittest

from tests.rig import call
from tests.test_server_routes import ME, RouteCase
from tests.test_server_views import entry, item
from hub import accounts, config, missing
from hub.server import app

PROBE = {"duration": 100.0, "audio": [], "subs": [], "video": {"codec": "h264", "width": 1920, "height": 1080}}
PASSWORD = "un-mot-de-passe"


def film(n, **over):
    return item(f"m{n}", title=f"Film {n}", meta={"tmdb": f"movie/{n}"}, **over)


def following(n, saga):
    return {"id": f"e{n}", "title": f"Film {n}", "season": None, "number": None, "saga": saga}


class SagaNextCase(RouteCase):
    def stock(self, items, entries=None):
        """Les films donnés, chacun avec un fichier vidéo `e<n>` sauf si `entries` dit lesquels."""
        entries = [entry(f"e{row['id'][1:]}", row["id"]) for row in items] if entries is None else entries
        self.fill(items, entries)
        self.probed(entries)

    def probed(self, entries):
        for row in entries:
            self.db.set_probe(row["id"], PROBE)

    def saga_file(self, **sagas):
        (self.root / "data" / "sagas.json").write_text(json.dumps({"sagas": [{"name": name, "films": films} for name, films in sagas.items()]}), encoding="utf-8")

    def collection(self, name, films):
        self.db.kv_set(missing.KEY, {"films": {}, "collections": {name: {"films": films}}})

    def play(self, ident, **kw):
        reply = self.post(f"/api/play/{ident}", {}, **kw)
        self.assertEqual(reply.status, 200, reply.body)
        return reply.json()

    def next_of(self, ident):
        return self.play(ident)["next"]


class TestSagaOfTheFile(SagaNextCase):
    def test_a_film_in_the_middle_of_a_saga_is_followed_by_the_next_one(self):
        self.stock([film(1), film(2), film(3)])
        self.saga_file(Saga=[1, 2, 3])
        self.assertEqual(self.next_of("e1"), following(2, "Saga"))
        self.assertEqual(self.next_of("e2"), following(3, "Saga"))

    def test_the_order_is_the_one_of_the_list_not_the_one_of_the_titles(self):
        self.stock([film(1), film(2), film(3)])
        self.saga_file(Saga=[3, 1, 2])
        self.assertEqual(self.next_of("e3"), following(1, "Saga"))

    def test_the_last_film_of_a_saga_has_nothing_after_it(self):
        self.stock([film(1), film(2)])
        self.saga_file(Saga=[1, 2])
        self.assertIsNone(self.next_of("e2"))

    def test_a_film_outside_any_saga_has_nothing_after_it(self):
        self.stock([film(1), film(2), film(3)])
        self.saga_file(Saga=[1, 2])
        self.assertIsNone(self.next_of("e3"))

    def test_a_missing_film_is_skipped(self):
        self.stock([film(1), film(3)])
        self.saga_file(Saga=[1, 2, 3])
        self.assertEqual(self.next_of("e1"), following(3, "Saga"))

    def test_a_film_without_a_video_file_is_skipped(self):
        items = [film(1), film(2), film(3)]
        entries = [entry("e1", "m1"), entry("e3", "m3")]
        self.stock(items, entries)
        self.saga_file(Saga=[1, 2, 3])
        self.assertEqual(self.next_of("e1"), following(3, "Saga"))

    def test_the_first_saga_of_the_file_with_a_next_film_wins(self):
        self.stock([film(1), film(2), film(3), film(4)])
        self.saga_file(Fin=[1, 2], Premiere=[2, 3], Seconde=[2, 4])
        self.assertEqual(self.next_of("e2"), following(3, "Premiere"))

    def test_the_versions_of_a_film_share_its_place(self):
        versions = [item("m2a", title="Film 2", meta={"tmdb": "movie/2"}), item("m2b", title="Film 2 version longue", meta={"tmdb": "movie/2"})]
        entries = [entry("e1", "m1"), entry("e2a", "m2a"), entry("e2b", "m2b"), entry("e3", "m3")]
        self.stock([film(1), *versions, film(3)], entries)
        self.saga_file(Saga=[1, 2, 3])
        self.assertEqual(self.next_of("e1")["id"], "e2a")
        self.assertEqual(self.next_of("e2a"), following(3, "Saga"))
        self.assertEqual(self.next_of("e2b"), following(3, "Saga"))


class TestSagaOfTheLabel(SagaNextCase):
    def test_without_a_collection_the_saga_follows_the_release_dates(self):
        self.stock([film(1, grp="Saga", year="2003"), film(2, grp="Saga", year="2001"), film(3, grp="Saga", year="2002")])
        self.assertEqual(self.next_of("e2"), following(3, "Saga"))
        self.assertEqual(self.next_of("e3"), following(1, "Saga"))
        self.assertIsNone(self.next_of("e1"))

    def test_a_known_collection_gives_the_order_and_a_missing_film_is_skipped(self):
        self.stock([film(1, grp="Saga", year="2001"), film(2, grp="Saga", year="2002"), film(4, grp="Saga", year="2000")])
        self.collection("Saga", [2, 3, 4, 1])
        self.assertEqual(self.next_of("e2"), following(4, "Saga"))
        self.assertEqual(self.next_of("e4"), following(1, "Saga"))
        self.assertIsNone(self.next_of("e1"))

    def test_the_versions_of_a_film_are_not_each_others_next_without_a_collection(self):
        versions = [item("m2a", title="Film 2", year="2002", grp="Saga", meta={"tmdb": "movie/2"}), item("m2b", title="Film 2 long", year="2004", grp="Saga", meta={"tmdb": "movie/2"})]
        entries = [entry("e2a", "m2a"), entry("e2b", "m2b"), entry("e3", "m3")]
        self.stock([*versions, film(3, grp="Saga", year="2003")], entries)
        self.assertEqual(self.next_of("e2a"), following(3, "Saga"))
        self.assertIsNone(self.next_of("e3"))

    def test_the_versions_of_a_film_are_not_each_others_next_with_a_collection(self):
        versions = [item("m2a", title="Film 2", grp="Saga", meta={"tmdb": "movie/2"}), item("m2b", title="Film 2 long", grp="Saga", meta={"tmdb": "movie/2"})]
        entries = [entry("e2a", "m2a"), entry("e2b", "m2b"), entry("e3", "m3")]
        self.stock([*versions, film(3, grp="Saga")], entries)
        self.collection("Saga", [2, 3])
        self.assertEqual(self.next_of("e2a"), following(3, "Saga"))
        self.assertEqual(self.next_of("e2b"), following(3, "Saga"))

    def test_the_sagas_of_the_file_come_before_the_label(self):
        self.stock([film(1, grp="Etiquette", year="2001"), film(2, grp="Etiquette", year="2002"), film(3)])
        self.saga_file(Fichier=[1, 3])
        self.assertEqual(self.next_of("e1"), following(3, "Fichier"))

    def test_the_label_is_used_when_the_sagas_of_the_file_have_nothing_after_the_film(self):
        self.stock([film(1, grp="Etiquette", year="2001"), film(2, grp="Etiquette", year="2002"), film(3)])
        self.saga_file(Fichier=[3, 1])
        self.assertEqual(self.next_of("e1"), following(2, "Etiquette"))

    def test_a_saga_of_the_file_with_the_same_name_replaces_the_label(self):
        self.stock([film(1, grp="Saga", year="2001"), film(2, grp="Saga", year="2002"), film(3)])
        self.saga_file(Saga=[2, 3])
        self.assertIsNone(self.next_of("e1"))
        self.assertEqual(self.next_of("e2"), following(3, "Saga"))


class TestWhatStaysTheSame(SagaNextCase):
    def test_the_next_part_of_a_film_comes_before_the_saga_and_the_saga_after_the_last_part(self):
        parts = [entry("p2", "m1", number=2), entry("p1", "m1", number=1)]
        sequel = [entry("s2", "m2", number=2), entry("s1", "m2", number=1)]
        self.stock([film(1), film(2)], parts + sequel)
        self.saga_file(Saga=[1, 2])
        self.assertEqual(self.next_of("p1"), {"id": "p2", "title": "Titre p2", "season": None, "number": 2})
        self.assertEqual(self.next_of("p2"), {"id": "s1", "title": "Film 2", "season": None, "number": 1, "saga": "Saga"})

    def test_an_episode_keeps_its_next_without_a_saga(self):
        episodes = [entry("t1", "s", season=1, number=1), entry("t2", "s", season=1, number=2)]
        self.stock([item("s", "show", title="Série", grp="Saga"), film(1, grp="Saga")], episodes + [entry("e1", "m1")])
        self.assertEqual(self.next_of("t1"), {"id": "t2", "title": "Titre t2", "season": 1, "number": 2})
        self.assertIsNone(self.next_of("t2"))

    def test_a_finished_film_of_a_saga_still_has_no_resume_card(self):
        self.stock([film(1), film(2)])
        self.saga_file(Saga=[1, 2])
        self.db.save_progress(ME, "e1", 100.0, 100.0, True)
        self.assertEqual(app.continue_row(self.hub, ME), [])
        self.assertIsNone(app.next_entry(self.hub, self.hub.entry("e1")))


class TestOtherLibraries(SagaNextCase):
    def setUp(self):
        super().setUp()
        self.other = config.Library("autre", "Autre", "movies", self.root / "autre")
        self.other.path.mkdir()
        self.hub.cfg.libraries.append(self.other)
        self.stock([film(1), film(3)])
        elsewhere = [entry("e2", "m2", lib="autre")]
        self.db.replace_library("autre", [film(2, lib="autre")], elsewhere)
        self.probed(elsewhere)
        self.saga_file(Saga=[1, 2, 3])
        self.admin = accounts.create(self.db, "root", PASSWORD)
        self.user = accounts.create(self.db, "alice", PASSWORD)

    def session(self, row):
        return self.hub.cookie(accounts.by_id(self.db, row["id"]))

    def test_a_film_of_another_library_is_never_proposed(self):
        self.assertEqual(self.play("e1", headers=self.session(self.admin))["next"], following(3, "Saga"))

    def test_an_account_gets_only_a_film_of_the_library_it_just_played_from(self):
        accounts.set_rights(self.db, self.user["id"], {"lib", "autre"}, {"autre": {"play": False}})
        self.assertEqual(self.play("e1", headers=self.session(self.user))["next"], following(3, "Saga"))

    def test_an_account_that_cannot_play_the_library_gets_no_plan_at_all(self):
        accounts.set_rights(self.db, self.user["id"], {"lib", "autre"}, {"lib": {"play": False}})
        reply = call(self.hub, "POST", "/api/play/e1", {}, headers=self.session(self.user))[1]
        self.assertEqual((reply.status, reply.json()), (403, {"error": "lecture non autorisée"}))


if __name__ == "__main__":
    unittest.main()
