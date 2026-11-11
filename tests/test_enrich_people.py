"""Les films manquants de tout le monde : qui parcourir, la passe qui interroge TMDB pour chacun (simulé), la tâche et sa route."""

from __future__ import annotations

import time
import types
import unittest
from pathlib import Path
from unittest import mock

from tests import fixtures
from tests import test_server_routes as base
from tests.rig import call
from tests.test_person_films import TempCase, cast, crew
from tests.test_server_views import item
from hub import accounts, config

fixtures.REPO  # noqa: B018  (rend `libraries` importable)
config.use_media_library(fixtures.REPO)

from libraries.video.tmdb import TmdbAuthError, TmdbError  # noqa: E402

from hub import enrich, missing, people  # noqa: E402

LIB = types.SimpleNamespace(id="films", kind="movies")
HIDDEN = types.SimpleNamespace(id="prive", kind="movies")


def film(ident, directors=(), actors=(), ids=None, lib="films"):
    """Un film de la table items : `ids` {nom: identifiant TMDB de la personne}."""
    return item(f"m{ident}", lib=lib, title=f"Film {ident}", meta={"tmdb": f"movie/{ident}", "directors": list(directors), "actors": [{"name": a} for a in actors], "person_ids": ids or {}})


class TestWho(TempCase):
    """`people.bulk_ids` : les réalisateurs, et les acteurs parmi les premiers rôles d'un film, dont le nom désigne une seule personne."""

    def ids(self, libs=("films",), billed=3):
        return people.bulk_ids(self.db, list(libs), billed)

    def test_the_directors_and_the_first_actors_of_each_film(self):
        self.db.replace_library("films", [film(1, ["Réal"], ["A", "B", "C", "D", "E"], {"Réal": 100, "A": 1, "B": 2, "C": 3, "D": 4, "E": 5})], [])
        self.assertEqual(sorted(self.ids()), [1, 2, 3, 100])                  # D et E sont au-delà des trois premiers rôles

    def test_an_actor_in_the_first_roles_of_one_film_only_is_enough(self):
        self.db.replace_library("films", [film(1, [], ["A", "B", "C", "Z"], {"A": 1, "B": 2, "C": 3, "Z": 26}), film(2, [], ["Z"], {"Z": 26})], [])
        self.assertEqual(sorted(self.ids()), [1, 2, 3, 26])

    def test_nobody_is_asked_twice(self):
        self.db.replace_library("films", [film(1, ["Réal"], ["A"], {"Réal": 100, "A": 1}), film(2, ["Réal"], ["A"], {"Réal": 100, "A": 1})], [])
        self.assertEqual(sorted(self.ids()), [1, 100])

    def test_someone_without_an_id_is_left_out(self):
        self.db.replace_library("films", [film(1, ["Sans id"], ["A", "Sans id aussi"], {"A": 1})], [])
        self.assertEqual(self.ids(), [1])

    def test_namesakes_are_left_out(self):
        self.db.replace_library("films", [film(1, [], ["Doublon", "A"], {"Doublon": 300, "A": 1}), film(2, [], ["Doublon"], {"Doublon": 301})], [])
        self.assertEqual(self.ids(), [1])

    def test_only_the_libraries_asked_count(self):
        self.db.replace_library("films", [film(1, ["Réal"], [], {"Réal": 100})], [])
        self.db.replace_library("prive", [film(2, ["Autre"], [], {"Autre": 200}, lib="prive")], [])
        self.assertEqual(self.ids(["films"]), [100])
        self.assertEqual(sorted(self.ids(["films", "prive"])), [100, 200])
        self.assertEqual(self.ids([]), [])

    def test_the_leading_actors_of_a_show_count_by_their_ids(self):
        actors = [{"name": n, "role": ""} for n in "ABCD"]
        self.db.replace_library("series", [item("s1", "show", lib="series", meta={"actors": actors, "person_ids": {"A": 1, "B": 2, "C": 3, "D": 4}})], [])
        self.assertEqual(self.ids(["series"]), [1, 2, 3])                          # D est au-delà des trois premiers rôles ; une série n'a pas de réalisateur

    def test_a_show_that_was_not_read_for_ids_gives_nobody(self):
        self.db.replace_library("series", [item("s1", "show", lib="series", meta={"actors": [{"name": "A", "role": ""}, "B"]})], [])          # fiche d'avant `person_ids` (et une fiche encore plus ancienne : des chaînes)
        self.assertEqual(self.ids(["series"]), [])

    def test_the_same_person_in_a_film_and_a_show_is_asked_once(self):
        self.db.replace_library("films", [film(1, [], ["A"], {"A": 7})], [])
        self.db.replace_library("series", [item("s1", "show", lib="series", meta={"actors": [{"name": "A", "role": ""}], "person_ids": {"A": 7}})], [])
        self.assertEqual(self.ids(["films", "series"]), [7])

    def test_a_namesake_in_a_show_stops_the_name_everywhere(self):
        self.db.replace_library("films", [film(1, [], ["A"], {"A": 7})], [])
        self.db.replace_library("series", [item("s1", "show", lib="series", meta={"actors": [{"name": "A", "role": ""}], "person_ids": {"A": 8}})], [])
        self.assertEqual(self.ids(["films", "series"]), [])
        self.assertEqual(self.ids(["films"]), [7])                                 # vu seulement des films, le nom désigne une seule personne

    def test_only_the_libraries_of_series_asked_count(self):
        self.db.replace_library("series", [item("s1", "show", lib="series", meta={"actors": [{"name": "A", "role": ""}], "person_ids": {"A": 1}})], [])
        self.assertEqual(self.ids(["films"]), [])


class FakeMany:
    """TMDB avec une réponse par personne (`credits` : {adresse: réponse}, ou une exception à lever)."""

    def __init__(self, credits):
        self.credits, self.asked, self.saved = credits, [], []

    def get(self, endpoint):
        self.asked.append(endpoint)
        found = self.credits[endpoint]
        if isinstance(found, Exception):
            raise found
        return found

    def save_image(self, path, size, dest):
        self.saved.append(path)
        Path(dest).write_bytes(b"jpeg")


class TestEnrichPeople(TempCase):
    """Réal (100) a réalisé le film 1 (qu'on a) et le 50 (qu'on n'a pas) ; A (201) a joué dans le 1 et le 60 ; B (202) dans le 1."""

    def setUp(self):
        super().setUp()
        self.db.replace_library("films", [film(1, ["Réal"], ["A", "B"], {"Réal": 100, "A": 201, "B": 202}), film(2, [], ["A"], {"A": 201})], [])
        self.credits = {
            "person/100/movie_credits": {"crew": [crew(1, "Film 1", poster="/p1.jpg"), crew(50, "Film 50", date="2010-01-01", poster="/p50.jpg")]},
            "person/201/movie_credits": {"cast": [cast(1, "Film 1", poster="/p1.jpg"), cast(60, "Film 60", date="2012-01-01", poster="/p60.jpg")]},
            "person/202/movie_credits": {"cast": [cast(1, "Film 1", poster="/p1.jpg")]}}

    def run_pass(self, credits=None, force=None, now=1_000_000_000, libs=(LIB,), key="cle", all_libs=None):
        """`force` None : on ne le donne pas, la passe prend son réglage par défaut."""
        tmdb, log = FakeMany(credits or self.credits), []
        posters = self.tmp / "tmdb"
        posters.mkdir(exist_ok=True)
        cfg = types.SimpleNamespace(tmdb_key=key, data=self.tmp, libraries=list(all_libs or libs))
        with mock.patch.object(enrich, "tmdb_client", return_value=(tmdb, posters)) as client, mock.patch.object(enrich, "time", types.SimpleNamespace(time=lambda: now)), \
                mock.patch.object(missing, "today", return_value="2026-11-08"):
            try:
                counts = enrich.enrich_people(cfg, self.db, list(libs), log.append, *(() if force is None else (force,)))
            except RuntimeError as e:
                counts = e
        return counts, tmdb, log, client

    def test_each_person_is_asked_once_and_kept(self):
        counts, tmdb, log, client = self.run_pass()
        self.assertEqual(sorted(tmdb.asked), ["person/100/movie_credits", "person/201/movie_credits", "person/202/movie_credits"])
        self.assertEqual(counts, {"people": 3, "asked": 3, "fresh": 0, "failed": 0, "films": 5})
        self.assertEqual([f["tmdb"] for f in self.db.kv_get("personinfo:100")["films"]], [1, 50])          # du plus récent au plus ancien
        self.assertEqual(client.call_count, 1)                                                      # un seul client pour toute la passe

    def test_no_poster_is_downloaded_for_a_film_we_have(self):
        _, tmdb, _, _ = self.run_pass()
        self.assertEqual(sorted(tmdb.saved), ["/p50.jpg", "/p60.jpg"])                              # 50 et 60 manquent ; 1 est dans la médiathèque
        films = {f["tmdb"]: f["poster"] for f in self.db.kv_get("personinfo:100")["films"]}
        self.assertEqual(films, {50: "film-50.jpg", 1: ""})
        self.assertEqual(sorted(p.name for p in (self.tmp / "tmdb").iterdir()), ["film-50.jpg", "film-60.jpg"])

    def test_a_film_we_have_in_a_library_of_series_is_not_downloaded_either(self):
        shows = types.SimpleNamespace(id="tele", kind="shows")
        self.db.replace_library("films", [film(2, ["Réal"], [], {"Réal": 100})], [])               # le film 1 n'est plus là...
        self.db.replace_library("tele", [item("a1", "show", lib="tele")], [base.entry("e1", "a1", lib="tele", meta={"info": {"tmdb": "movie/1"}})])          # ... il est rangé dans une bibliothèque de séries
        _, tmdb, _, _ = self.run_pass({"person/100/movie_credits": self.credits["person/100/movie_credits"]}, libs=(LIB, shows))
        self.assertEqual(tmdb.saved, ["/p50.jpg"])

    def test_a_film_we_have_in_another_library_than_the_one_of_the_pass_is_not_downloaded_either(self):
        shows = types.SimpleNamespace(id="tele", kind="shows")
        self.db.replace_library("tele", [item("a1", "show", lib="tele", meta={"actors": [{"name": "A", "role": ""}], "person_ids": {"A": 201}})], [])
        self.db.replace_library("films", [film(1, [], [], {})], [])                                          # le film 1 est dans Films ; la passe ne porte que sur la bibliothèque de séries
        _, tmdb, _, _ = self.run_pass({"person/201/movie_credits": self.credits["person/201/movie_credits"]}, libs=(shows,), all_libs=(LIB, shows))
        self.assertEqual(tmdb.saved, ["/p60.jpg"])

    def test_a_person_known_for_less_than_thirty_days_is_not_asked_again(self):
        self.run_pass(now=1_000_000_000)
        counts, tmdb, _, _ = self.run_pass(now=1_000_000_000 + enrich.PERSON_STALE - 1)
        self.assertEqual((tmdb.asked, counts), ([], {"people": 3, "asked": 0, "fresh": 3, "failed": 0, "films": 0}))

    def test_a_person_known_for_thirty_days_is_asked_again_and_so_is_everybody_with_force(self):
        self.run_pass(now=1_000_000_000)
        counts, tmdb, _, _ = self.run_pass(now=1_000_000_000 + enrich.PERSON_STALE)
        self.assertEqual((len(tmdb.asked), counts["fresh"]), (3, 0))
        counts, tmdb, _, _ = self.run_pass(now=1_000_000_000 + enrich.PERSON_STALE + 5, force=True)
        self.assertEqual(len(tmdb.asked), 3)

    def test_only_the_missing_part_is_asked_after_an_interruption(self):
        self.run_pass({"person/100/movie_credits": self.credits["person/100/movie_credits"], "person/201/movie_credits": TmdbAuthError("clé refusée"), "person/202/movie_credits": {}})
        counts, tmdb, _, _ = self.run_pass()
        self.assertEqual(sorted(tmdb.asked), ["person/201/movie_credits", "person/202/movie_credits"])          # Réal avait été gardé avant l'arrêt
        self.assertEqual((counts["fresh"], counts["asked"]), (1, 2))

    def test_a_person_tmdb_refuses_is_counted_and_the_others_go_on(self):
        credits = {**self.credits, "person/201/movie_credits": TmdbError("introuvable sur TMDB (HTTP 404)")}
        counts, _, log, _ = self.run_pass(credits)
        self.assertEqual((counts["failed"], counts["asked"], counts["films"]), (1, 3, 3))
        self.assertIsNone(self.db.kv_get("personinfo:201"))
        self.assertIsNotNone(self.db.kv_get("personinfo:202"))
        self.assertTrue(any("introuvable sur TMDB" in line for line in log))

    def test_tmdb_out_of_reach_stops_the_pass_after_some_failures_in_a_row(self):
        down = {endpoint: TmdbError("HTTP 503 Service Unavailable") for endpoint in self.credits}
        with mock.patch.object(enrich, "PEOPLE_MAX_FAILS", 2):
            counts, tmdb, _, _ = self.run_pass(down)
        self.assertIsInstance(counts, RuntimeError)
        self.assertIn("TMDB injoignable", str(counts))
        self.assertIn("2 échecs de suite", str(counts))
        self.assertEqual(len(tmdb.asked), 2)                                                        # la troisième personne n'est pas demandée : un job ne reste pas des heures sur un réseau coupé
        self.assertIsNone(self.db.kv_get("personinfo:100"))

    def test_a_success_in_between_starts_the_count_again(self):
        credits = {**self.credits, "person/100/movie_credits": TmdbError("panne"), "person/202/movie_credits": TmdbError("panne")}          # échec, réussite (201), échec
        with mock.patch.object(enrich, "PEOPLE_MAX_FAILS", 2):
            counts, tmdb, _, _ = self.run_pass(credits)
        self.assertEqual((counts["failed"], counts["asked"], len(tmdb.asked)), (2, 3, 3))

    def test_the_journal_keeps_the_first_notes_and_says_it_stops(self):
        down = {endpoint: TmdbError("introuvable sur TMDB (HTTP 404)") for endpoint in self.credits}
        with mock.patch.object(enrich, "PEOPLE_MAX_NOTES", 1), mock.patch.object(enrich, "PEOPLE_MAX_FAILS", 99):
            _, _, log, _ = self.run_pass(down)
        self.assertEqual(len(log), 4)                                                               # début, une note, « … », bilan
        self.assertIn("introuvable", log[1])
        self.assertIn("ne sont pas écrites", log[2])
        self.assertIn("3 en échec", log[3])

    def test_a_refused_key_stops_everything_and_keeps_what_was_done(self):
        credits = {**self.credits, "person/201/movie_credits": TmdbAuthError("clé refusée")}
        counts, tmdb, _, _ = self.run_pass(credits)
        self.assertIsInstance(counts, RuntimeError)
        self.assertIn("TMDB : clé refusée", str(counts))
        self.assertEqual(tmdb.asked, ["person/100/movie_credits", "person/201/movie_credits"])      # 202 n'est pas demandé
        self.assertIsNotNone(self.db.kv_get("personinfo:100"))

    def test_no_key_is_a_clear_error(self):
        cfg = types.SimpleNamespace(tmdb_key="", data=self.tmp)
        with self.assertRaisesRegex(RuntimeError, "Pas de clé TMDB"):
            enrich.enrich_people(cfg, self.db, [LIB], lambda line: None)

    def test_the_journal_has_a_summary_and_not_a_line_per_person(self):
        _, _, log, _ = self.run_pass()
        self.assertEqual(len(log), 2)
        self.assertIn("3 personne(s)", log[0])
        self.assertIn("5 film(s) retenu(s)", log[1])

    def test_the_progress_is_noted_regularly(self):
        with mock.patch.object(enrich, "PEOPLE_PROGRESS", 2):
            _, _, log, _ = self.run_pass()
        self.assertEqual(len(log), 3)
        self.assertIn("2/3", log[1])

    def test_the_last_person_does_not_get_a_progress_line(self):
        with mock.patch.object(enrich, "PEOPLE_PROGRESS", 3):
            _, _, log, _ = self.run_pass()
        self.assertEqual(len(log), 2)                                                               # 3/3 n'est pas écrit : le bilan le dit

    def test_the_journal_says_once_that_it_stops_writing(self):
        down = {endpoint: TmdbError("introuvable sur TMDB (HTTP 404)") for endpoint in self.credits}
        with mock.patch.object(enrich, "PEOPLE_MAX_NOTES", 0), mock.patch.object(enrich, "PEOPLE_MAX_FAILS", 99):
            _, _, log, _ = self.run_pass(down)
        self.assertEqual(len(log), 3)                                                               # début, « … » une seule fois, bilan
        self.assertEqual(sum("ne sont pas écrites" in line for line in log), 1)

    def test_a_pass_is_not_forced_unless_asked_and_the_cache_of_tmdb_is_read_only_then(self):
        _, _, _, client = self.run_pass()
        client.assert_called_once_with(mock.ANY, read=True)
        counts, tmdb, _, _ = self.run_pass()                                                        # sans `force` : les personnes connues sont sautées
        self.assertEqual((tmdb.asked, counts["fresh"]), ([], 3))
        _, _, _, client = self.run_pass(force=True)
        client.assert_called_once_with(mock.ANY, read=False)

    def test_the_leading_actors_are_those_the_person_pages_count(self):
        names = [f"P{n}" for n in range(enrich.MAX_ORDER + 3)]
        self.db.replace_library("films", [film(1, [], names, {name: 1000 + n for n, name in enumerate(names)})], [])
        credits = {f"person/{1000 + n}/movie_credits": {} for n in range(len(names))}
        _, tmdb, _, _ = self.run_pass(credits)
        self.assertEqual(sorted(tmdb.asked), sorted(f"person/{1000 + n}/movie_credits" for n in range(enrich.MAX_ORDER + 1)))          # les rôles 0 à MAX_ORDER, pas un de plus ni de moins

    def test_nobody_to_ask_is_not_an_error(self):
        self.db.replace_library("films", [film(1, ["Sans id"], [])], [])
        counts, tmdb, log, _ = self.run_pass()
        self.assertEqual((counts, tmdb.asked), ({"people": 0, "asked": 0, "fresh": 0, "failed": 0, "films": 0}, []))


class TestTask(unittest.TestCase):
    def test_the_task_has_its_own_label_and_asks_the_pass_for_every_library(self):
        from hub.server import jobs
        libs = [LIB, HIDDEN]
        queue = jobs.Jobs(types.SimpleNamespace(libraries=libs), "base")
        with mock.patch.object(jobs.enrich, "enrich_people", return_value={"people": 1, "failed": 0}) as asked:
            job = queue.enrich_people(libs, "bibliothèques de films", True)
            for _ in range(500):
                if job.status == "done":
                    break
                time.sleep(0.01)
            other = queue.enrich_people([HIDDEN], "Séries")
            for _ in range(500):
                if other.status == "done":
                    break
                time.sleep(0.01)
        self.assertEqual((job.status, job.label, job.result), ("done", "Films manquants · bibliothèques de films", {"people": 1, "failed": 0}))
        self.assertEqual((asked.call_args_list[0].args[1:3], asked.call_args_list[0].args[4]), (("base", libs), True))
        self.assertEqual((other.label, asked.call_args_list[1].args[2], asked.call_args_list[1].args[4]), ("Films manquants · Séries", [HIDDEN], False))          # une tâche par catégorie


class TestRoute(base.RouteCase):
    def setUp(self):
        super().setUp()
        self.hub.cfg.tmdb_key = "cle"
        self.queue = self.hub.jobs.enrich_people
        self.queue.return_value.public.return_value = {"id": 9, "label": "Films manquants · bibliothèques de films"}

    def enrich(self, body=None, headers=None):
        reply = call(self.hub, "POST", "/api/enrich/people", {} if body is None else body, headers=headers)[1]
        return reply.status, reply.json()

    def libraries(self):
        """Une bibliothèque de séries et une de musique en plus de celle des films (`self.lib`)."""
        shows = config.Library("series", "Séries", "shows", self.root / "series")
        music = config.Library("musique", "Musique", "music", self.root / "musique")
        self.hub.cfg.libraries += [shows, music]
        return shows, music

    def test_it_starts_the_task_for_the_libraries_of_films(self):
        self.assertEqual(self.enrich(), (202, {"job": {"id": 9, "label": "Films manquants · bibliothèques de films"}}))
        self.queue.assert_called_once_with([self.lib], "bibliothèques de films", False)

    def test_everybody_means_the_libraries_of_films_and_not_the_series(self):
        self.libraries()
        self.enrich()
        self.queue.assert_called_once_with([self.lib], "bibliothèques de films", False)

    def test_a_category_is_one_library_and_the_task_carries_its_name(self):
        shows, _ = self.libraries()
        self.assertEqual(self.enrich({"lib": "series"})[0], 202)
        self.queue.assert_called_once_with([shows], "Séries", False)
        self.queue.reset_mock()
        self.enrich({"lib": "lib"})                                                  # une bibliothèque de films aussi
        self.queue.assert_called_once_with([self.lib], "Bibliothèque", False)

    def test_a_library_that_is_neither_films_nor_series_or_does_not_exist_is_refused(self):
        self.libraries()
        status, body = self.enrich({"lib": "musique"})
        self.assertEqual((status, "films" in body["error"]), (400, True))
        self.assertEqual(self.enrich({"lib": "absente"})[0], 404)
        for lib in (5, ["lib"], True):
            self.assertEqual(self.enrich({"lib": lib})[0], 400, lib)
        self.queue.assert_not_called()

    def test_force_is_passed_on(self):
        self.enrich({"force": True})
        self.queue.assert_called_once_with([self.lib], "bibliothèques de films", True)

    def test_the_administrator_may_and_an_ordinary_account_may_not(self):
        root = self.hub.cookie(accounts.create(self.db, "root", "motdepasse12"))
        bob = accounts.create(self.db, "bob", "motdepasse12")
        accounts.set_rights(self.db, bob["id"], {"lib"}, {"lib": {"view": True}})
        self.assertEqual(self.enrich(headers=self.hub.cookie(bob)), (403, {"error": "réservé à l'administrateur"}))
        self.queue.assert_not_called()
        self.assertEqual(self.enrich(headers=root)[0], 202)

    def test_without_a_tmdb_key_the_error_says_so(self):
        self.hub.cfg.tmdb_key = ""
        status, body = self.enrich()
        self.assertEqual((status, "TMDB_KEY" in body["error"]), (400, True))
        self.queue.assert_not_called()

    def test_the_route_needs_a_post(self):
        self.assertEqual(call(self.hub, "GET", "/api/enrich/people")[1].status, 404)
