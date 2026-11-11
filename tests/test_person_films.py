"""Les films qui manquent à une personne : son identifiant TMDB (lu dans les étiquettes), ce que TMDB dit de sa filmographie (simulé), et ce que `/api/person` en montre."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from tests import fixtures
from tests import test_server_routes as base
from tests.rig import call
from tests.test_server_views import item
from hub import accounts, config

fixtures.REPO  # noqa: B018  (rend `libraries` importable)
config.use_media_library(fixtures.REPO)

from libraries.video.tmdb import TmdbAuthError, TmdbError  # noqa: E402

from hub import enrich, missing, people  # noqa: E402
from hub.db import Database  # noqa: E402

TODAY = "2026-11-08"
CRUZ, EVANS = 955, 16828


def cast(ident, title, date="2020-05-01", order=0, votes=500, poster="/p.jpg"):
    return {"id": ident, "title": title, "release_date": date, "order": order, "vote_count": votes, "poster_path": poster, "character": "Quelqu'un"}


def crew(ident, title, job="Director", date="2020-05-01", votes=5, poster="/p.jpg"):
    return {"id": ident, "title": title, "release_date": date, "job": job, "vote_count": votes, "poster_path": poster}


class FakeTmdb:
    """Le client TMDB : `credits` est la réponse de person/<id>/movie_credits ; ce qui est demandé est noté dans `asked`."""

    def __init__(self, credits=None, error=None):
        self.credits, self.error, self.asked, self.saved = credits or {}, error, [], []

    def get(self, endpoint):
        self.asked.append(endpoint)
        if self.error:
            raise self.error
        return self.credits

    def save_image(self, path, size, dest):
        self.saved.append((path, size, Path(dest).name))
        Path(dest).write_bytes(b"jpeg")


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = Database(self.tmp / "hub.db")


class TestIdsInTheIndex(TempCase):
    """`people.build` : les identifiants que les étiquettes donnent à une personne, avec le nombre d'items qui les portent."""

    def index(self, *metas):
        self.db.replace_library("films", [item(f"m{n}", lib="films", meta=meta) for n, meta in enumerate(metas)], [])
        return people.index(self.db)

    def test_an_id_is_counted_once_per_item_whatever_the_roles_and_the_spelling(self):
        found = self.index(
            {"directors": ["Pénélope Cruz"], "actors": [{"name": "Pénélope Cruz", "role": ""}], "person_ids": {"Pénélope Cruz": CRUZ}},
            {"actors": [{"name": "Penelope  Cruz"}], "person_ids": {"Penelope  Cruz": CRUZ}})
        cruz = found[people.key_of("Penelope Cruz")]
        self.assertEqual(dict(self.seen(cruz)), {CRUZ: 2})
        self.assertEqual(cruz["titles"], {"films": 2})          # un titre compte une fois, quels que soient les rôles

    def seen(self, person, libs=("films",)):
        return people.ids_seen(person, libs)

    def test_a_person_has_an_id_when_there_is_exactly_one(self):
        found = self.index({"actors": [{"name": "Chris Evans"}], "person_ids": {"Chris Evans": EVANS}}, {"actors": [{"name": "Chris Evans"}]}, {"actors": [{"name": "Chris Evans"}], "person_ids": {"Chris Evans": EVANS}})
        person = found["chris evans"]
        self.assertEqual((dict(self.seen(person)), people.tmdb_id(person, ["films"])), ({EVANS: 2}, EVANS))          # un film sans identifiant ne contredit pas les autres

    def test_namesakes_have_no_id(self):
        person = self.index({"actors": [{"name": "Chris Evans"}], "person_ids": {"Chris Evans": 16828}}, {"actors": [{"name": "Chris Evans"}], "person_ids": {"Chris Evans": 1}})["chris evans"]
        self.assertEqual((dict(self.seen(person)), people.tmdb_id(person, ["films"])), ({16828: 1, 1: 1}, None))

    def test_a_person_without_any_id_has_none(self):
        person = self.index({"actors": [{"name": "Sans Identifiant"}]})["sans identifiant"]
        self.assertEqual((dict(self.seen(person)), people.tmdb_id(person, ["films"])), ({}, None))
        self.assertIsNone(people.tmdb_id(None, ["films"]))

    def test_only_the_names_of_the_item_take_an_id(self):
        found = self.index({"actors": [{"name": "Un"}], "person_ids": {"Un": 1, "Quelqu'un d'autre": 2}})
        self.assertEqual(dict(self.seen(found["un"])), {1: 1})
        self.assertNotIn("quelqu'un d'autre", found)

    def test_what_is_not_an_id_is_ignored(self):
        for bad in ("abc", True, None, 1.5, [], {}, -3, 0):
            found = self.index({"actors": [{"name": "Un"}], "person_ids": {"Un": bad}})
            self.assertEqual(dict(self.seen(found["un"])), {}, bad)
        for not_a_dict in ([1], "Un", 5, None):
            self.assertEqual(dict(self.seen(self.index({"actors": [{"name": "Un"}], "person_ids": not_a_dict})["un"])), {}, not_a_dict)

    def test_a_show_without_ids_leaves_the_ids_of_the_films_alone(self):
        self.db.replace_library("series", [item("s1", "show", lib="series", meta={"actors": ["Un"]})], [])
        self.db.replace_library("films", [item("m1", lib="films", meta={"actors": [{"name": "Un"}], "person_ids": {"Un": 1}})], [])
        person = people.index(self.db)["un"]
        self.assertEqual((dict(self.seen(person, ["films", "series"])), people.tmdb_id(person, ["films", "series"])), ({1: 1}, 1))

    def test_the_ids_are_those_of_the_libraries_asked_and_only_those(self):
        self.db.replace_library("films", [item("m1", lib="films", meta={"actors": [{"name": "Un"}]})], [])          # sans identifiant
        self.db.replace_library("prive", [item("p1", lib="prive", meta={"actors": [{"name": "Un"}], "person_ids": {"Un": 1}})], [])
        person = people.index(self.db)["un"]
        self.assertEqual((people.tmdb_id(person, ["films"]), people.tmdb_id(person, ["films", "prive"]), people.tmdb_id(person, []), people.tmdb_id(person, ["absente"])), (None, 1, None, None))

    def test_a_namesake_of_another_library_does_not_make_the_id_ambiguous_for_those_who_cannot_see_it(self):
        self.db.replace_library("films", [item("m1", lib="films", meta={"actors": [{"name": "Un"}], "person_ids": {"Un": 1}})], [])
        self.db.replace_library("prive", [item("p1", lib="prive", meta={"actors": [{"name": "Un"}], "person_ids": {"Un": 2}})], [])
        person = people.index(self.db)["un"]
        self.assertEqual((people.tmdb_id(person, ["films"]), people.tmdb_id(person, ["films", "prive"])), (1, None))


class TestPersonFilms(unittest.TestCase):
    """`enrich.person_films` : ce que la filmographie de TMDB garde, et dans quel ordre."""

    def films(self, credits, today=TODAY):
        with mock.patch.object(missing, "today", return_value=today):
            return enrich.person_films(credits)

    def test_a_director_is_a_crew_member_whose_job_is_director(self):
        found = self.films({"crew": [crew(1, "Réalisé"), crew(2, "Produit", job="Producer"), crew(3, "Scénario", job="Screenplay")]})
        self.assertEqual([(f["tmdb"], f["role"]) for f in found], [(1, "director")])

    def test_an_actor_is_in_the_first_ten_of_the_cast_and_in_a_film_many_have_voted_for(self):
        found = self.films({"cast": [cast(1, "Premier", order=0), cast(2, "Dixième", order=10), cast(3, "Onzième", order=11), cast(4, "Peu vu", votes=99), cast(5, "Juste assez", votes=100),
                                     cast(6, "Sans ordre", order=None), cast(7, "Sans vote", votes=None)]})
        self.assertEqual(sorted(f["tmdb"] for f in found), [1, 2, 5])
        self.assertEqual({f["role"] for f in found}, {"actor"})

    def test_the_limits_are_named(self):
        self.assertEqual((enrich.MAX_ORDER, enrich.MIN_VOTES, enrich.MAX_FILMS, enrich.PERSON_STALE), (10, 100, 40, 30 * 24 * 3600))

    def test_only_films_that_are_out_count(self):
        found = self.films({"cast": [cast(1, "Hier", date="2026-11-07"), cast(2, "Aujourd'hui", date="2026-11-08"), cast(3, "Demain", date="2026-11-09"), cast(4, "Sans date", date=""), cast(5, "Date nulle", date=None)],
                            "crew": [crew(6, "Futur", date="2027-01-01")]})
        self.assertEqual(sorted(f["tmdb"] for f in found), [1, 2])

    def test_a_film_where_she_is_both_is_listed_once_as_director(self):
        found = self.films({"cast": [cast(1, "Les deux")], "crew": [crew(1, "Les deux")]})
        self.assertEqual([(f["tmdb"], f["role"]) for f in found], [(1, "director")])

    def test_the_same_film_twice_in_the_cast_is_listed_once(self):
        self.assertEqual(len(self.films({"cast": [cast(1, "Double"), cast(1, "Double", order=1)], "crew": [crew(2, "Deux fois"), crew(2, "Deux fois")]})), 2)

    def test_the_latest_first_at_most_forty_and_nothing_without_a_usable_id(self):
        credits = {"cast": [cast(n, f"Film {n}", date=f"{1950 + n}-01-01") for n in range(1, 51)] + [{"title": "Sans identifiant", "release_date": "2000-01-01", "order": 0, "vote_count": 999}]}
        found = self.films(credits)
        self.assertEqual((len(found), found[0]["tmdb"], found[-1]["tmdb"]), (40, 50, 11))
        self.assertEqual([f["tmdb"] for f in found], sorted((f["tmdb"] for f in found), reverse=True))

    def test_a_film_has_the_shape_the_api_needs(self):
        found = self.films({"cast": [cast(7, "Volver", date="2006-03-17", poster="/v.jpg")]})
        self.assertEqual(found, [{"tmdb": 7, "title": "Volver", "year": "2006", "date": "2006-03-17", "role": "actor", "poster_path": "/v.jpg"}])

    def test_garbage_gives_nothing(self):
        for credits in ({}, {"cast": None, "crew": None}, {"cast": "x", "crew": 5}, {"cast": [None, 5, "x", {}]}):
            self.assertEqual(self.films(credits), [], credits)


class TestEnrichPerson(TempCase):
    def run_pass(self, tmdb, person_id=CRUZ, force=False, now=1_000_000_000, key="cle"):
        log = []
        posters = self.tmp / "tmdb"
        posters.mkdir(exist_ok=True)
        cfg = types.SimpleNamespace(tmdb_key=key, data=self.tmp)
        with mock.patch.object(enrich, "tmdb_client", return_value=(tmdb, posters)) as client, mock.patch.object(enrich, "time", types.SimpleNamespace(time=lambda: now)), \
                mock.patch.object(missing, "today", return_value=TODAY):
            counts = enrich.enrich_person(cfg, self.db, person_id, log.append, force)
        return counts, log, client

    CREDITS = {"cast": [cast(1, "Volver", "2006-03-17", poster="/v.jpg"), cast(2, "Sans affiche", "2010-01-01", poster=None), cast(3, "Peu connu", "2012-01-01", votes=3)], "crew": [crew(4, "Son film", date="1999-09-09", poster="/s.jpg")]}

    def test_the_credits_are_asked_once_and_kept_under_the_person(self):
        tmdb = FakeTmdb(self.CREDITS)
        counts, _, _ = self.run_pass(tmdb)
        self.assertEqual(tmdb.asked, [f"person/{CRUZ}/movie_credits"])          # sans « / » au début : l'adresse de base n'en finit pas par un
        kept = self.db.kv_get(f"personinfo:{CRUZ}")
        self.assertEqual(kept["at"], 1_000_000_000)
        self.assertEqual(kept["films"], [{"tmdb": 2, "title": "Sans affiche", "year": "2010", "date": "2010-01-01", "role": "actor", "poster": ""},
                                         {"tmdb": 1, "title": "Volver", "year": "2006", "date": "2006-03-17", "role": "actor", "poster": "film-1.jpg"},
                                         {"tmdb": 4, "title": "Son film", "year": "1999", "date": "1999-09-09", "role": "director", "poster": "film-4.jpg"}])
        self.assertEqual(counts, {"films": 3, "failed": 0})

    def test_the_posters_go_to_the_data_folder_like_those_of_the_sagas(self):
        tmdb = FakeTmdb(self.CREDITS)
        self.run_pass(tmdb)
        self.assertEqual(sorted(tmdb.saved), [("/s.jpg", "w342", "film-4.jpg.tmp"), ("/v.jpg", "w342", "film-1.jpg.tmp")])
        self.assertEqual(sorted(p.name for p in (self.tmp / "tmdb").iterdir()), ["film-1.jpg", "film-4.jpg"])          # d'un bloc : aucun .tmp ne reste

    def test_a_poster_already_there_is_not_downloaded_again_unless_forced(self):
        (self.tmp / "tmdb").mkdir()
        (self.tmp / "tmdb" / "film-1.jpg").write_bytes(b"ancienne")
        tmdb = FakeTmdb(self.CREDITS)
        self.run_pass(tmdb)
        self.assertEqual([s[0] for s in tmdb.saved], ["/s.jpg"])
        self.assertEqual((self.tmp / "tmdb" / "film-1.jpg").read_bytes(), b"ancienne")
        again = FakeTmdb(self.CREDITS)
        self.run_pass(again, force=True)
        self.assertEqual(sorted(s[0] for s in again.saved), ["/s.jpg", "/v.jpg"])

    def test_known_for_less_than_thirty_days_it_is_not_asked_again(self):
        self.run_pass(FakeTmdb(self.CREDITS), now=1_000_000_000)
        tmdb = FakeTmdb({"cast": [cast(9, "Nouveau")]})
        counts, log, client = self.run_pass(tmdb, now=1_000_000_000 + enrich.PERSON_STALE - 1)
        self.assertEqual((tmdb.asked, [f["tmdb"] for f in self.db.kv_get(f"personinfo:{CRUZ}")["films"]], counts["failed"]), ([], [2, 1, 4], 0))
        client.assert_not_called()          # pas même un client TMDB : la clé n'est pas utile

    def test_the_default_is_not_to_force_and_a_known_person_reports_her_films(self):
        self.db.kv_set(f"personinfo:{CRUZ}", {"at": int(time.time()), "films": [{"tmdb": 1}, {"tmdb": 2}]})
        with mock.patch.object(enrich, "tmdb_client") as client:
            counts = enrich.enrich_person(types.SimpleNamespace(tmdb_key="cle", data=self.tmp), self.db, CRUZ)
        self.assertEqual((counts, client.called), ({"films": 2, "failed": 0}, False))

    def test_what_was_kept_without_a_date_is_asked_again(self):
        self.db.kv_set(f"personinfo:{CRUZ}", {"films": [{"tmdb": 1}]})
        tmdb = FakeTmdb({"cast": [cast(9, "Nouveau", "2025-01-01")]})
        self.run_pass(tmdb, now=enrich.PERSON_STALE)          # sans date, une personne est aussi vieille que l'horloge
        self.assertEqual((len(tmdb.asked), [f["tmdb"] for f in self.db.kv_get(f"personinfo:{CRUZ}")["films"]]), (1, [9]))

    def test_known_for_thirty_days_it_is_asked_again(self):
        self.run_pass(FakeTmdb(self.CREDITS), now=1_000_000_000)
        tmdb = FakeTmdb({"cast": [cast(9, "Nouveau", "2025-01-01")]})
        self.run_pass(tmdb, now=1_000_000_000 + enrich.PERSON_STALE)
        kept = self.db.kv_get(f"personinfo:{CRUZ}")
        self.assertEqual(([f["tmdb"] for f in kept["films"]], kept["at"], len(tmdb.asked)), ([9], 1_000_000_000 + enrich.PERSON_STALE, 1))

    def test_force_asks_again_whatever_the_age_and_reads_no_cache(self):
        self.run_pass(FakeTmdb(self.CREDITS))
        tmdb = FakeTmdb({"cast": [cast(9, "Nouveau", "2025-01-01")]})
        _, _, client = self.run_pass(tmdb, force=True)
        self.assertEqual(len(tmdb.asked), 1)
        self.assertIs(client.call_args.kwargs["read"], False)

    def test_the_cache_of_tmdb_is_read_unless_forced(self):
        _, _, client = self.run_pass(FakeTmdb(self.CREDITS))
        self.assertIs(client.call_args.kwargs["read"], True)

    def test_another_person_is_not_the_one_that_is_known(self):
        self.run_pass(FakeTmdb(self.CREDITS))
        tmdb = FakeTmdb({"cast": [cast(9, "Autre", "2025-01-01")]})
        self.run_pass(tmdb, person_id=EVANS)
        self.assertEqual(len(tmdb.asked), 1)
        self.assertEqual([f["tmdb"] for f in self.db.kv_get(f"personinfo:{EVANS}")["films"]], [9])

    def test_a_person_with_no_film_is_kept_as_known(self):
        counts, _, _ = self.run_pass(FakeTmdb({"cast": [], "crew": []}))
        self.assertEqual((self.db.kv_get(f"personinfo:{CRUZ}")["films"], counts), ([], {"films": 0, "failed": 0}))

    def test_a_tmdb_error_is_logged_counted_and_keeps_what_was_known(self):
        self.run_pass(FakeTmdb(self.CREDITS), now=1)
        counts, log, _ = self.run_pass(FakeTmdb(error=TmdbError("introuvable sur TMDB (HTTP 404)")), force=True, now=2)
        self.assertEqual(counts, {"films": 0, "failed": 1})
        self.assertIn("introuvable sur TMDB", log[-1])
        self.assertEqual(self.db.kv_get(f"personinfo:{CRUZ}")["at"], 1)

    def test_a_tmdb_error_on_a_first_request_stores_nothing(self):
        self.run_pass(FakeTmdb(error=TmdbError("panne")))
        self.assertIsNone(self.db.kv_get(f"personinfo:{CRUZ}"))

    def test_a_refused_key_stops_with_a_clear_error(self):
        with self.assertRaisesRegex(RuntimeError, "TMDB : clé refusée"):
            self.run_pass(FakeTmdb(error=TmdbAuthError("clé refusée")))
        self.assertIsNone(self.db.kv_get(f"personinfo:{CRUZ}"))

    def test_no_key_is_a_clear_error_and_asks_nothing(self):
        cfg = types.SimpleNamespace(tmdb_key="", data=self.tmp)
        with self.assertRaisesRegex(RuntimeError, "Pas de clé TMDB"):
            enrich.enrich_person(cfg, self.db, CRUZ, lambda line: None)

    def test_a_broken_poster_does_not_lose_the_film(self):
        class NoPoster(FakeTmdb):
            def save_image(self, path, size, dest):
                raise TmdbError("image introuvable")

        self.run_pass(NoPoster(self.CREDITS))
        films = self.db.kv_get(f"personinfo:{CRUZ}")["films"]
        self.assertEqual([(f["tmdb"], f["poster"]) for f in films], [(2, ""), (1, ""), (4, "")])

    def test_a_scan_does_not_touch_it(self):
        self.run_pass(FakeTmdb(self.CREDITS))
        self.db.replace_library("films", [item("m1", lib="films")], [])
        self.assertEqual(len(self.db.kv_get(f"personinfo:{CRUZ}")["films"]), 3)


class TestJob(unittest.TestCase):
    def test_the_task_asks_the_enrichment_for_that_person_and_is_labelled_with_her_name(self):
        from hub.server import jobs
        queue = jobs.Jobs(types.SimpleNamespace(libraries=[]), "base")
        with mock.patch.object(jobs.enrich, "enrich_person", return_value={"films": 2, "failed": 0}) as asked:
            job = queue.enrich_person("Penélope Cruz", CRUZ, True)
            for _ in range(500):
                if job.status == "done":
                    break
                time.sleep(0.01)
        self.assertEqual((job.status, job.label, job.result), ("done", "Films de Penélope Cruz", {"films": 2, "failed": 0}))
        self.assertEqual(asked.call_args.args[1:3] + (asked.call_args.args[4],), ("base", CRUZ, True))


class TestOfPerson(unittest.TestCase):
    """`missing.of_person` : les films de la fiche d'une personne qu'on n'a pas."""

    FILMS = [{"tmdb": 3, "title": "Trois", "year": "2012", "date": "2012-01-01", "role": "actor", "poster": "film-3.jpg"},
             {"tmdb": 2, "title": "Deux", "year": "2006", "date": "2006-01-01", "role": "director", "poster": ""},
             {"tmdb": 1, "title": "Un", "year": "1999", "date": "1999-01-01", "role": "actor", "poster": "film-1.jpg"}]

    def test_the_films_we_own_are_left_out_and_the_others_keep_their_order(self):
        self.assertEqual(missing.of_person(self.FILMS, {2}), [
            {"tmdb": 3, "title": "Trois", "year": "2012", "rank": 1, "cover": "/img/film/3", "role": "actor"},
            {"tmdb": 1, "title": "Un", "year": "1999", "rank": 2, "cover": "/img/film/1", "role": "actor"}])

    def test_a_film_without_a_poster_has_no_cover_url(self):
        self.assertEqual(missing.of_person(self.FILMS, {1, 3})[0]["cover"], "")

    def test_nothing_missing_is_an_empty_list(self):
        self.assertEqual(missing.of_person(self.FILMS, {1, 2, 3}), [])
        self.assertEqual(missing.of_person([], set()), [])


class PersonCase(base.RouteCase):
    """Deux bibliothèques de films, dont une qu'un compte ordinaire ne voit pas ; Penélope Cruz a un identifiant dans toutes les deux."""

    def setUp(self):
        super().setUp()
        self.hidden = config.Library("prive", "Privé", "movies", self.root / "prive")
        self.hidden.path.mkdir()
        self.hub.cfg.libraries.append(self.hidden)
        self.hub.cfg.tmdb_key = "cle"
        self.db.replace_library("lib", [
            item("m1", title="Volver", year="2006", meta={"tmdb": "movie/1", "actors": [{"name": "Penélope Cruz"}], "person_ids": {"Penélope Cruz": CRUZ}}),
            item("m2", title="Autre", year="2008", meta={"tmdb": "movie/2", "actors": [{"name": "Penélope Cruz"}, {"name": "Chris Evans"}], "person_ids": {"Penélope Cruz": CRUZ, "Chris Evans": EVANS}}),
            item("m3", title="Sans identifiant", year="2010", meta={"tmdb": "movie/3", "actors": [{"name": "Seule Ici"}]}),
        ], [])
        self.db.replace_library("prive", [item("p1", lib="prive", title="Secret", meta={"tmdb": "movie/4", "actors": [{"name": "Penélope Cruz"}], "person_ids": {"Penélope Cruz": CRUZ}})], [])
        self.film = lambda n, **over: {"tmdb": n, "title": f"Film {n}", "year": "2001", "date": "2001-01-01", "role": "actor", "poster": f"film-{n}.jpg", **over}
        self.db.kv_set(f"personinfo:{CRUZ}", {"at": 1, "films": [self.film(1), self.film(4), self.film(5), self.film(6, poster="", role="director")]})

    def person(self, name="Penélope Cruz", headers=None):
        reply = call(self.hub, "GET", "/api/person?name=" + quote(name, safe=""), headers=headers)[1]
        return reply.status, reply.json()

    def bob(self):
        """Les en-têtes d'un compte ordinaire qui ne voit pas « Privé » ; ceux de l'administrateur (le premier compte) sont dans `root_cookie`."""
        self.root_cookie = self.hub.cookie(accounts.create(self.db, "root", "motdepasse12"))
        bob = accounts.create(self.db, "bob", "motdepasse12")
        known = {lib.id for lib in self.hub.cfg.libraries}
        accounts.set_rights(self.db, bob["id"], known, {lib: {"view": False} for lib in ("prive", "tele") if lib in known})          # « Téléfilms », quand il y en a une, lui est cachée aussi
        return self.hub.cookie(bob)


class TestMissingInThePerson(PersonCase):
    def test_what_we_do_not_own_is_listed_in_the_shape_of_the_sagas(self):
        self.assertEqual(self.person()[1]["missing"], [
            {"tmdb": 5, "title": "Film 5", "year": "2001", "rank": 1, "cover": "/img/film/5", "role": "actor"},
            {"tmdb": 6, "title": "Film 6", "year": "2001", "rank": 2, "cover": "", "role": "director"}])          # 1 est dans « Films », 4 dans « Privé » : l'un et l'autre sont chez nous

    def test_a_film_of_a_library_the_account_cannot_see_is_missing_for_it_and_not_for_the_administrator(self):
        bob = self.bob()
        self.assertEqual([m["tmdb"] for m in self.person(headers=bob)[1]["missing"]], [4, 5, 6])
        self.assertEqual([m["tmdb"] for m in self.person(headers=self.root_cookie)[1]["missing"]], [5, 6])

    def test_it_is_compared_at_each_request(self):
        self.assertEqual([m["tmdb"] for m in self.person()[1]["missing"]], [5, 6])
        self.db.replace_library("lib", [item("m5", title="Nouveau", meta={"tmdb": "movie/5", "actors": [{"name": "Penélope Cruz"}], "person_ids": {"Penélope Cruz": CRUZ}})], [])
        self.assertEqual([m["tmdb"] for m in self.person()[1]["missing"]], [1, 6])          # le film 5 est arrivé, le 1 est parti

    def test_a_show_with_the_same_number_does_not_count_as_owned(self):
        self.db.replace_library("lib", [item("s1", "show", title="Série", meta={"tmdb": 5, "actors": ["Penélope Cruz"]}),
                                        item("m1", title="Volver", meta={"tmdb": "movie/1", "actors": [{"name": "Penélope Cruz"}], "person_ids": {"Penélope Cruz": CRUZ}})], [])
        self.assertIn(5, [m["tmdb"] for m in self.person()[1]["missing"]])

    def test_a_film_with_a_bare_number_tag_is_owned_too(self):
        self.db.replace_library("lib", [item("m5", title="Cinq", meta={"tmdb": "5", "actors": [{"name": "Penélope Cruz"}], "person_ids": {"Penélope Cruz": CRUZ}})], [])
        self.assertEqual([m["tmdb"] for m in self.person()[1]["missing"]], [1, 6])

    def telefilms(self, *infos):
        """Une bibliothèque de type « séries » (« Téléfilms ») qui range un item par fichier, chacun avec l'étiquette TMDB `infos[n]` (ce que le scan garde de ses tags)."""
        lib = config.Library("tele", "Téléfilms", "shows", self.root / "tele")
        lib.path.mkdir()
        self.hub.cfg.libraries.append(lib)
        self.db.replace_library("tele", [item(f"a{n}", "show", lib="tele", title=f"Téléfilm {n}") for n, _ in enumerate(infos)],
                                [base.entry(f"e{n}", f"a{n}", lib="tele", meta={"info": info}) for n, info in enumerate(infos)])

    def test_a_film_kept_in_a_library_of_series_counts_as_owned(self):
        self.telefilms({"tmdb": "movie/5"})
        self.assertEqual([m["tmdb"] for m in self.person()[1]["missing"]], [6])

    def test_a_tag_that_is_not_a_film_does_not(self):
        self.telefilms({"tmdb": "tv/5"}, {"tmdb": "5"}, {"tmdb": 5}, {"title": "sans étiquette"}, {"tmdb": "movie/abc"})
        self.assertEqual([m["tmdb"] for m in self.person()[1]["missing"]], [5, 6])

    def test_such_a_film_in_a_library_the_account_cannot_see_is_missing_for_it(self):
        self.telefilms({"tmdb": "movie/5"})
        bob = self.bob()
        self.assertEqual([m["tmdb"] for m in self.person(headers=bob)[1]["missing"]], [4, 5, 6])
        self.assertEqual([m["tmdb"] for m in self.person(headers=self.root_cookie)[1]["missing"]], [6])

    def test_an_unreadable_entry_of_a_library_of_series_is_skipped(self):
        self.telefilms({"tmdb": "movie/5"}, {"tmdb": "movie/6"})
        self.db.execute("UPDATE entries SET meta='movie/6 mais pas du json' WHERE id='e1'")
        self.db.execute("INSERT INTO entries(id,item_id,lib,rel,kind,title,summary,meta) VALUES('e9','a0','tele','x','video','','','\"movie/6\"')")          # du JSON, mais pas un objet
        self.assertEqual([m["tmdb"] for m in self.person()[1]["missing"]], [6])

    def test_the_key_is_absent_when_nothing_is_missing(self):
        self.db.kv_set(f"personinfo:{CRUZ}", {"at": 1, "films": [self.film(1)]})
        self.assertNotIn("missing", self.person()[1])
        self.db.kv_set(f"personinfo:{CRUZ}", {"at": 1, "films": []})
        self.assertNotIn("missing", self.person()[1])

    def test_the_key_is_absent_when_the_person_was_never_enriched(self):
        self.db.kv_delete(f"personinfo:{CRUZ}")
        self.assertNotIn("missing", self.person()[1])

    def test_a_person_without_a_single_id_has_no_missing_films(self):
        self.assertNotIn("missing", self.person("Seule Ici")[1])

    def test_namesakes_have_no_missing_films_whatever_is_kept(self):
        self.db.replace_library("lib", [item("m9", title="Homonyme", meta={"actors": [{"name": "Penélope Cruz"}], "person_ids": {"Penélope Cruz": 1}})], [])
        self.db.kv_set("personinfo:1", {"at": 1, "films": [self.film(77)]})
        body = self.person()[1]
        self.assertNotIn("missing", body)
        self.assertIs(body["enrichable"], False)

    def test_an_unknown_name_has_neither(self):
        body = self.person("Personne Du Tout")[1]
        self.assertEqual(body, {"name": "Personne Du Tout", "roles": {"director": 0, "actor": 0}, "results": [], "enrichable": False})

    def test_a_name_only_a_hidden_library_knows_is_unknown_and_shows_nothing_of_its_films(self):
        self.db.replace_library("prive", [item("p1", lib="prive", title="Secret", meta={"actors": [{"name": "Seule Ailleurs"}], "person_ids": {"Seule Ailleurs": 4242}})], [])
        self.db.kv_set("personinfo:4242", {"at": 1, "films": [self.film(88)]})
        body = self.person("Seule Ailleurs", self.bob())[1]
        self.assertEqual((body["results"], body["enrichable"], "missing" in body), ([], False, False))


class TestEnrichable(PersonCase):
    def test_one_id_and_a_key_make_the_button_useful(self):
        self.assertIs(self.person()[1]["enrichable"], True)

    def test_without_a_tmdb_key_it_is_not(self):
        self.hub.cfg.tmdb_key = ""
        self.assertIs(self.person()[1]["enrichable"], False)

    def test_a_person_without_an_id_is_not_enrichable(self):
        self.assertIs(self.person("Seule Ici")[1]["enrichable"], False)

    def test_two_ids_for_one_name_are_not(self):
        self.db.replace_library("lib", [item("m9", title="Homonyme", meta={"actors": [{"name": "Penélope Cruz"}], "person_ids": {"Penélope Cruz": 1}})], [])
        self.assertIs(self.person()[1]["enrichable"], False)

    def test_an_id_that_only_a_hidden_library_knows_is_not_the_account_s(self):
        """Dans « Films », Penélope Cruz n'a pas d'identifiant ; « Privé » (que bob ne voit pas) lui en donne un : seul l'administrateur le voit."""
        self.db.replace_library("lib", [item("m1", title="Volver", meta={"tmdb": "movie/1", "actors": [{"name": "Penélope Cruz"}]})], [])
        bob = self.bob()
        self.assertEqual((self.person(headers=bob)[1]["enrichable"], "missing" in self.person(headers=bob)[1]), (False, False))
        admin = self.person(headers=self.root_cookie)[1]
        self.assertEqual((admin["enrichable"], [m["tmdb"] for m in admin["missing"]]), (True, [5, 6]))

    def test_a_namesake_in_a_hidden_library_does_not_stop_the_account_either(self):
        self.db.replace_library("prive", [item("p1", lib="prive", meta={"tmdb": "movie/4", "actors": [{"name": "Penélope Cruz"}], "person_ids": {"Penélope Cruz": 1}})], [])
        bob = self.bob()
        self.assertIs(self.person(headers=bob)[1]["enrichable"], True)
        self.assertIs(self.person(headers=self.root_cookie)[1]["enrichable"], False)          # l'administrateur voit les deux : homonymes

    def test_the_answer_does_not_depend_on_what_was_kept(self):
        self.db.kv_delete(f"personinfo:{CRUZ}")
        self.assertIs(self.person()[1]["enrichable"], True)


class TestEnrichPersonRoute(PersonCase):
    def setUp(self):
        super().setUp()
        self.queue = self.hub.jobs.enrich_person
        self.queue.return_value.public.return_value = {"id": 7, "label": "Films de Penélope Cruz"}

    def enrich(self, name="Penélope Cruz", headers=None, **body):
        reply = call(self.hub, "POST", "/api/enrich/person", {"name": name, **body}, headers=headers)[1]
        return reply.status, reply.json()

    def test_it_starts_the_task_for_the_id_of_the_name(self):
        self.assertEqual(self.enrich(), (202, {"job": {"id": 7, "label": "Films de Penélope Cruz"}}))
        self.queue.assert_called_once_with("Penélope Cruz", CRUZ, False)

    def test_the_name_is_found_whatever_its_accents_and_case_and_is_shown_as_written_in_the_library(self):
        self.enrich("PENELOPE  cruz")
        self.queue.assert_called_once_with("Penélope Cruz", CRUZ, False)

    def test_force_is_passed_on(self):
        self.enrich(force=True)
        self.queue.assert_called_once_with("Penélope Cruz", CRUZ, True)

    def test_the_administrator_may_and_an_ordinary_account_may_not(self):
        bob = self.bob()
        self.assertEqual(self.enrich(headers=bob), (403, {"error": "réservé à l'administrateur"}))
        self.queue.assert_not_called()
        self.assertEqual(self.enrich(headers=self.root_cookie)[0], 202)

    def test_without_a_tmdb_key_the_error_says_so(self):
        self.hub.cfg.tmdb_key = ""
        status, body = self.enrich()
        self.assertEqual((status, "TMDB_KEY" in body["error"]), (400, True))
        self.queue.assert_not_called()

    def test_a_name_with_two_ids_is_refused_with_a_clear_message(self):
        self.db.replace_library("lib", [item("m9", title="Homonyme", meta={"actors": [{"name": "Penélope Cruz"}], "person_ids": {"Penélope Cruz": 1}})], [])
        status, body = self.enrich()
        self.assertEqual(status, 400)
        self.assertIn("plusieurs personnes", body["error"])
        self.queue.assert_not_called()

    def test_a_name_without_any_id_is_refused_with_a_clear_message(self):
        status, body = self.enrich("Seule Ici")
        self.assertEqual(status, 400)
        self.assertIn("pas d'identifiant TMDB", body["error"])
        self.queue.assert_not_called()

    def test_an_unknown_name_is_a_404_and_a_bad_body_a_400(self):
        self.assertEqual(self.enrich("Personne Du Tout")[0], 404)
        for name in ("", "   ", None, 5, ["x"], "x" * 201):
            self.assertEqual(self.enrich(name)[0], 400, name)
        self.queue.assert_not_called()

    def test_the_route_needs_a_post(self):
        self.assertEqual(call(self.hub, "GET", "/api/enrich/person?name=Penelope")[1].status, 404)
