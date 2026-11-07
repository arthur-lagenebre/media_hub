"""L'enrichissement, sans réseau : ce qu'on retient d'une série, d'une collection de saga, les photos d'artistes déjà posées et le journal d'une passe."""

from __future__ import annotations

import json
import shutil
import tempfile
import types
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests import fixtures
from hub import config

fixtures.REPO  # noqa: B018  (rend `libraries` importable)
config.use_media_library(fixtures.REPO)

from hub import enrich, scan  # noqa: E402
from hub.db import Database  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 20


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = Database(self.tmp / "hub.db")


def item(ident, lib, kind, title="Titre", meta=None, grp=""):
    return {"id": ident, "lib": lib, "type": kind, "title": title, "sort": title.casefold(), "letter": title[:1], "year": "", "summary": "", "genres": "[]", "grp": grp,
            "meta": json.dumps(meta or {}), "added": 0, "count": 1}


class TestShowIsDone(TempCase):
    lib = types.SimpleNamespace(id="series")
    show = {"id": "s1"}

    def test_a_show_is_done_once_it_has_its_seasons(self):
        self.assertFalse(enrich.show_is_done(self.db, self.lib, self.show, False))
        self.db.kv_set(enrich.show_key(self.lib, self.show), {"season_summaries": {}, "doubts": []})
        self.assertTrue(enrich.show_is_done(self.db, self.lib, self.show, False))

    def test_a_sheet_from_before_the_seasons_is_done_again(self):
        self.db.kv_set(enrich.show_key(self.lib, self.show), {"summary": "x"})
        self.assertFalse(enrich.show_is_done(self.db, self.lib, self.show, False))

    def test_a_sheet_from_before_the_doubts_is_done_again(self):
        self.db.kv_set(enrich.show_key(self.lib, self.show), {"season_summaries": {"1": "x"}})
        self.assertFalse(enrich.show_is_done(self.db, self.lib, self.show, False))

    def test_force_does_everything_again(self):
        self.db.kv_set(enrich.show_key(self.lib, self.show), {"season_summaries": {"1": "x"}, "doubts": []})
        self.assertFalse(enrich.show_is_done(self.db, self.lib, self.show, True))


class TestShowFacts(unittest.TestCase):
    def test_what_tmdb_gives_is_kept(self):
        show = {"overview": "Un voyage", "genres": [{"name": "Drame"}, {"name": ""}, {}, {"name": "Mystère"}], "first_air_date": "2017-12-01", "name": "Dark",
                "number_of_seasons": 3, "number_of_episodes": 26, "status": "Ended"}
        self.assertEqual(enrich.show_facts(show, "70523"), {"summary": "Un voyage", "genres": ["Drame", "Mystère"], "year": "2017", "tmdb": 70523, "tmdb_name": "Dark",
                                                            "seasons_total": 3, "episodes_total": 26, "status": "Ended"})

    def test_what_tmdb_does_not_give_is_empty(self):
        for show in ({}, {"overview": None, "first_air_date": None, "name": None, "status": None}):
            self.assertEqual(enrich.show_facts(show, 1), {"summary": "", "genres": [], "year": "", "tmdb": 1, "tmdb_name": "", "seasons_total": None, "episodes_total": None, "status": ""})


class TestCastOrder(unittest.TestCase):
    def test_someone_without_a_count_comes_after_someone_with_one_even_if_listed_first(self):
        class Tmdb:
            def aggregate_credits(self, show_id):
                return {"cast": [{"name": "Sans compte", "order": 0}, {"name": "Un épisode", "total_episode_count": 1, "order": 1}]}

        self.assertEqual([p["name"] for p in enrich.cast(Tmdb(), 1)], ["Un épisode", "Sans compte"])


class FakeCollections:
    def __init__(self, parts):
        self.parts = parts

    def get(self, endpoint):
        return {"belongs_to_collection": {"id": 9000}}

    def collection(self, collection_id):
        return {"parts": self.parts}


def film(ident, date_):
    return {"id": ident, "title": f"Film {ident}", "release_date": date_}


def tagged(tmdb):
    return {"meta": json.dumps({"tmdb": f"movie/{tmdb}"})}


class TestRefreshReport(unittest.TestCase):
    def refresh(self, parts, owned):
        log = []
        run = enrich.SagaPass(FakeCollections(parts), None, {"films": {}, "collections": {}}, owned, log.append, False)
        run.refresh("Saga", [tagged(1)])
        return run, log

    def test_the_journal_counts_the_films_the_ones_missing_and_those_not_out_yet(self):
        run, log = self.refresh([film(1, "2000-01-01"), film(2, "2001-01-01"), film(3, "2999-01-01"), film(4, "")], {1: {}})
        self.assertEqual(log, ["  Saga : 4 film(s), 1 absent(s), 2 pas encore sorti(s)"])
        self.assertEqual(run.counts, {"sagas": 1, "films": 4, "failed": 0})

    def test_without_a_film_to_come_the_journal_says_nothing_of_it(self):
        _, log = self.refresh([film(1, "2000-01-01"), film(2, "2001-01-01")], {})
        self.assertEqual(log, ["  Saga : 2 film(s), 2 absent(s)"])

    def test_the_collection_is_kept_with_its_films_in_release_order(self):
        run, _ = self.refresh([film(5, "2005-01-01"), film(2, "2001-01-01"), {"title": "sans identifiant"}, film(3, "2001-01-01")], {})
        collection = run.info["collections"]["Saga"]
        self.assertEqual((collection["id"], collection["films"], collection["at"]), (9000, [2, 3, 5], run.now))
        self.assertEqual(sorted(run.info["films"]), ["2", "3", "5"])


class TestWanted(TempCase):
    def wanted(self, owned, known, force=False):
        path = self.tmp / "sagas.json"
        path.write_text(json.dumps({"sagas": [{"name": "A", "films": [1, 2, 3]}, {"name": "B", "films": [3, 4]}]}), encoding="utf-8")
        info = {"films": {str(i): {} for i in known}, "collections": {}}
        return enrich.SagaPass(None, None, info, owned, print, force).wanted(path)

    def test_the_films_listed_but_neither_owned_nor_described_are_wanted_once(self):
        self.assertEqual(self.wanted({1: {}}, [2]), [3, 4])

    def test_force_asks_again_for_the_ones_described_but_still_not_for_those_owned(self):
        self.assertEqual(self.wanted({1: {}}, [2], force=True), [2, 3, 4])

    def test_a_film_described_but_owned_is_not_wanted(self):
        self.assertEqual(self.wanted({3: {}, 4: {}}, [1]), [2])


class TestOtherPhotos(unittest.TestCase):
    legacy = Path("artists")

    def test_every_extension_of_the_three_names_and_the_folder_of_the_artist(self):
        names = [p.name for p in enrich.other_photos(self.legacy, "a1", "mb1", Path("home"))]
        self.assertEqual(names, [f"{stem}{ext}" for stem in ("a1", "custom-a1", "mb1") for ext in enrich.PHOTO_EXTS] + ["artist.webp"])

    def test_a_name_that_could_be_a_path_is_never_a_candidate(self):
        for mbid in ("../x", "a/b", ""):
            names = [p.name for p in enrich.other_photos(self.legacy, "a1", mbid, None)]
            self.assertEqual(len(names), 2 * len(enrich.PHOTO_EXTS), mbid)


class TestMusicBrainzClient(unittest.TestCase):
    def test_the_cache_is_read_in_its_own_folder(self):
        cfg = types.SimpleNamespace(data=Path("donnees"))
        with mock.patch.object(enrich, "MusicBrainz") as client:
            enrich.musicbrainz(cfg)
        kwargs = client.call_args.kwargs
        self.assertEqual(kwargs["user_agent"], enrich.MB_USER_AGENT)
        self.assertTrue(kwargs["cache"].read)
        self.assertEqual(kwargs["cache"].folder, Path("donnees") / "musicbrainz-cache")


class TestArtistJournal(TempCase):
    def run_pass(self, info):
        lib = types.SimpleNamespace(id="musique", path=self.tmp)
        cfg = types.SimpleNamespace(data=self.tmp)
        self.db.replace_library("musique", [item("alb", "musique", "album", grp="Daft", meta={"artist_mbid": "mb1", "artist_id": "a1"})], [])
        log = []
        with mock.patch.object(enrich, "musicbrainz"), mock.patch.object(enrich, "fetch_artist", lambda mb, mbid: dict(info)):
            counts = enrich.enrich_artists(cfg, self.db, lib, log.append)
        return counts, log

    def test_the_journal_gives_the_facts_of_the_sheet(self):
        counts, log = self.run_pass({"type": "Group", "area": "France", "begin": "1993", "end": "2021"})
        self.assertEqual((counts, log), ({"enriched": 1, "failed": 0}, ["  Daft : Groupe · France · 1993 – 2021"]))

    def test_an_empty_sheet_is_said_to_be_empty(self):
        _, log = self.run_pass({})
        self.assertEqual(log, ["  Daft : fiche vide"])


class TestMainRole(unittest.TestCase):
    def test_the_role_with_the_most_episodes_wins_and_one_without_a_count_comes_last(self):
        self.assertEqual(enrich.main_role({"roles": [{"character": "Sans compte"}, {"character": "Un épisode", "episode_count": 1}]}), "Un épisode")
        self.assertEqual(enrich.main_role({"roles": [{"character": "Un", "episode_count": 1}, {"character": "Dix", "episode_count": 10}]}), "Dix")

    def test_no_role_or_no_character_gives_nothing(self):
        for person in ({}, {"roles": []}, {"roles": [{"episode_count": 3}]}, {"roles": [{"character": None}]}):
            self.assertEqual(enrich.main_role(person), "", person)


class TestTmdbClient(TempCase):
    def client(self, read=None, data=None):
        cfg = types.SimpleNamespace(tmdb_key="cle", data=data or self.tmp / "nouveau" / "donnees")
        extra = {} if read is None else {"read": read}
        with mock.patch.object(enrich, "Tmdb") as tmdb, mock.patch.object(enrich.tmdb_cache, "Cache") as cache:
            result = enrich.tmdb_client(cfg, **extra)
        return cfg, result, tmdb, cache

    def test_the_cache_is_read_by_default_and_not_when_asked(self):
        _, _, _, cache = self.client()
        self.assertEqual(cache.call_args.kwargs, {"folder": self.tmp / "nouveau" / "donnees" / "tmdb-cache", "read": True})
        self.assertIs(self.client(read=False)[3].call_args.kwargs["read"], False)

    def test_the_folder_of_the_posters_is_created_with_its_parents_and_again_without_error(self):
        cfg, (_, posters), _, _ = self.client()
        self.assertEqual((posters, posters.is_dir()), (cfg.data / "tmdb", True))
        self.client(data=cfg.data)

    def test_without_a_key_it_is_an_error(self):
        with self.assertRaisesRegex(RuntimeError, "Pas de clé TMDB"):
            enrich.tmdb_client(types.SimpleNamespace(tmdb_key="", data=self.tmp))


class FakeShow:
    """Un TMDB qui connaît une série, et note les affiches qu'on lui demande."""

    def __init__(self, poster="/p.jpg"):
        self.poster, self.saved = poster, []

    def series(self, show_id):
        return {"name": "Dark", "overview": "Résumé", "first_air_date": "2017-12-01", "poster_path": self.poster, "genres": []}

    def save_image(self, path, size, dest):
        self.saved.append((path, size, dest))

    def season(self, show_id, number):
        return {}

    def aggregate_credits(self, show_id):
        return {"cast": []}


class TestEnrichShow(TempCase):
    def run_show(self, meta, tmdb=None):
        lib = types.SimpleNamespace(id="series", path=self.tmp / "lib")
        self.db.replace_library("series", [item("s1", "series", "show", meta=meta)], [])
        row = self.db.one("SELECT * FROM items WHERE id=?", ("s1",))
        tmdb = tmdb or FakeShow()
        with mock.patch.object(enrich.lookup, "resolve_show_id", return_value=42) as resolve:
            found = enrich.enrich_show(tmdb, self.tmp / "posters", self.db, lib, row)
        return found, resolve, tmdb, lib

    def test_a_pinned_identifier_is_given_to_the_search_and_none_otherwise(self):
        _, resolve, tmdb, lib = self.run_show({"folder": "Dark", "tmdb_pinned": 123})
        self.assertEqual(resolve.call_args.args, (tmdb, lib.path / "Dark", 123))
        self.assertIsNone(self.run_show({"folder": "Dark"})[1].call_args.args[2])

    def test_the_poster_is_saved_in_the_folder_of_the_posters_under_the_identifier_of_the_show(self):
        (_, data), _, tmdb, _ = self.run_show({})
        self.assertEqual(tmdb.saved, [("/p.jpg", "w500", self.tmp / "posters" / "42.jpg")])
        self.assertEqual(data["poster_file"], "42.jpg")

    def test_a_show_without_a_poster_keeps_none(self):
        (_, data), _, tmdb, _ = self.run_show({}, FakeShow(poster=None))
        self.assertEqual(tmdb.saved, [])
        self.assertNotIn("poster_file", data)

    def test_the_sheet_keeps_the_rating_of_the_show(self):
        (_, data), _, _, _ = self.run_show({})
        self.assertEqual({k: data["tmdb_rating"][k] for k in ("tmdb", "vote", "votes")}, {"tmdb": 42, "vote": None, "votes": 0})       # FakeShow : personne n'a voté
        self.assertEqual(self.db.kv_get(enrich.show_key(types.SimpleNamespace(id="series"), {"id": "s1"}))["tmdb_rating"], data["tmdb_rating"])


class TestRatingOf(unittest.TestCase):
    def test_the_average_is_rounded_and_the_votes_are_kept(self):
        self.assertEqual(enrich.rating_of({"vote_average": 7.8467, "vote_count": 1234}, "27205", now=100), {"tmdb": 27205, "vote": 7.8, "votes": 1234, "at": 100})

    def test_without_votes_there_is_no_average(self):
        for sheet in ({}, {"vote_average": 0, "vote_count": 0}, {"vote_average": 6.0, "vote_count": 0}, {"vote_average": None, "vote_count": None}):
            with self.subTest(sheet=sheet):
                self.assertEqual(enrich.rating_of(sheet, 1, now=5), {"tmdb": 1, "vote": None, "votes": 0, "at": 5})


class TestRatingIsFresh(unittest.TestCase):
    now = 10 * enrich.RATING_STALE

    def rating(self, age=0, tmdb=27205):
        return {"tmdb": tmdb, "vote": 7.8, "votes": 100, "at": self.now - age}

    def test_a_recent_rating_of_the_same_film_is_fresh(self):
        self.assertTrue(enrich.rating_is_fresh(self.rating(age=enrich.RATING_STALE - 1), 27205, self.now))

    def test_an_old_rating_a_rating_of_another_film_or_a_forced_pass_ask_again(self):
        self.assertFalse(enrich.rating_is_fresh(self.rating(age=enrich.RATING_STALE), 27205, self.now))
        self.assertFalse(enrich.rating_is_fresh(self.rating(tmdb=1), 27205, self.now))              # le film a été réétiqueté
        self.assertFalse(enrich.rating_is_fresh(self.rating(), 27205, self.now, force=True))

    def test_no_rating_or_one_of_an_older_shape_is_not_fresh(self):
        for rating in (None, 7.8, {}, {"vote": 7.8}):
            with self.subTest(rating=rating):
                self.assertFalse(enrich.rating_is_fresh(rating, 27205, self.now))


class FakeRatings:
    """Un TMDB qui note ce qu'on lui demande et répond d'après `sheets` ({adresse: fiche, ou exception})."""

    def __init__(self, sheets):
        self.sheets, self.calls = sheets, []

    def get(self, endpoint):
        self.calls.append(endpoint)
        found = self.sheets.get(endpoint, enrich.TmdbError("introuvable sur TMDB (HTTP 404)"))
        if isinstance(found, Exception):
            raise found
        return found


class TestEnrichRatings(TempCase):
    def setUp(self):
        super().setUp()
        self.cfg = types.SimpleNamespace(tmdb_key="cle", data=self.tmp)
        self.films = types.SimpleNamespace(id="films", kind="movies")
        fresh = {"tmdb": 3, "vote": 6.0, "votes": 50, "at": 2 ** 40}          # dans le futur : toujours frais
        self.db.replace_library("films", [
            item("a", "films", "movie", title="A", meta={"tmdb": "movie/1"}),
            item("b", "films", "movie", title="B", meta={"tmdb": ""}),                                   # pas d'identifiant : rien à demander
            item("c", "films", "movie", title="C", meta={"tmdb": "movie/2", "tmdb_rating": {**fresh, "tmdb": 2, "at": 0}}),     # vieille note
            item("d", "films", "movie", title="D", meta={"tmdb": "movie/3", "tmdb_rating": fresh}),
            item("e", "films", "movie", title="E", meta={"tmdb": "movie/4"}),
        ], [])

    def run_pass(self, tmdb, lib=None, **options):
        log = []
        with mock.patch.object(enrich, "tmdb_client", return_value=(tmdb, self.tmp)) as client:
            counts = enrich.enrich_ratings(self.cfg, self.db, lib or self.films, log.append, **options)
        return counts, log, client

    def meta(self, ident):
        return json.loads(self.db.one("SELECT meta FROM items WHERE id=?", (ident,))["meta"])

    def test_the_films_without_a_fresh_rating_are_asked_and_their_rating_kept(self):
        tmdb = FakeRatings({"movie/1": {"vote_average": 8.36, "vote_count": 900}, "movie/2": {"vote_average": 5.0, "vote_count": 30}})
        counts, log, _ = self.run_pass(tmdb)
        self.assertEqual(tmdb.calls, ["movie/1", "movie/2", "movie/4"])
        self.assertEqual(counts, {"rated": 2, "failed": 1})
        self.assertEqual(log, ["  E : pas de note (introuvable sur TMDB (HTTP 404))", "  Notes TMDB : 2 demandée(s), 1 en échec"])
        rating = self.meta("a")["tmdb_rating"]
        self.assertEqual({k: rating[k] for k in ("tmdb", "vote", "votes")}, {"tmdb": 1, "vote": 8.4, "votes": 900})
        self.assertEqual(self.db.kv_get(enrich.show_key(self.films, {"id": "a"})), {"tmdb_rating": rating})        # gardée pour le scan
        self.assertEqual(self.meta("a")["tmdb"], "movie/1")                                                       # le reste de l'élément ne bouge pas
        self.assertNotIn("tmdb_rating", self.meta("e"))

    def test_force_asks_every_film_with_an_identifier(self):
        tmdb = FakeRatings({})
        self.run_pass(tmdb, force=True)
        self.assertEqual(tmdb.calls, ["movie/1", "movie/2", "movie/3", "movie/4"])

    def test_a_forced_pass_does_not_ask_again_what_this_task_just_rated(self):
        started = 2 ** 41                                           # après la note de « d » (2 ** 40) : « d » est redemandée, pas « a »
        self.db.update_item_meta("a", meta={"tmdb_rating": {"tmdb": 1, "vote": 7.0, "votes": 40, "at": started}})
        tmdb = FakeRatings({})
        self.run_pass(tmdb, force=True, since=started)              # « a » : notée par la même tâche (fiche de la série)
        self.assertEqual(tmdb.calls, ["movie/2", "movie/3", "movie/4"])

    def test_a_sheet_chosen_while_the_pass_asks_tmdb_keeps_its_own_rating(self):
        db = self.db

        class ChosenMeanwhile(FakeRatings):
            def get(self, endpoint):
                if endpoint == "movie/1":
                    db.update_item_meta("a", meta={"tmdb": "movie/9"})          # « Changer la fiche TMDB » pendant la requête
                return super().get(endpoint)
        counts, log, _ = self.run_pass(ChosenMeanwhile({"movie/1": {"vote_average": 8.0, "vote_count": 900}}))
        self.assertNotIn("tmdb_rating", self.meta("a"))
        self.assertIsNone(self.db.kv_get(enrich.show_key(self.films, {"id": "a"})))
        self.assertEqual(counts["rated"], 0)
        self.assertIn("  A : fiche TMDB changée pendant la passe, note laissée", log)

    def test_tmdb_is_asked_and_not_its_cache(self):
        self.assertEqual(self.run_pass(FakeRatings({}))[2].call_args.kwargs, {"read": False})

    def test_a_show_is_asked_by_its_tv_address_and_its_sheet_keeps_the_rest(self):
        shows = types.SimpleNamespace(id="series", kind="shows")
        self.db.replace_library("series", [item("s1", "series", "show", title="Dark", meta={"tmdb": 70523})], [])
        self.db.kv_set(enrich.show_key(shows, {"id": "s1"}), {"summary": "Un voyage", "tmdb": 70523})
        tmdb = FakeRatings({"tv/70523": {"vote_average": 8.4, "vote_count": 6000}})
        self.assertEqual(self.run_pass(tmdb, shows)[0], {"rated": 1, "failed": 0})
        sheet = self.db.kv_get(enrich.show_key(shows, {"id": "s1"}))
        self.assertEqual((sheet["summary"], sheet["tmdb_rating"]["vote"]), ("Un voyage", 8.4))

    def test_an_authentication_error_stops_the_pass(self):
        with self.assertRaisesRegex(RuntimeError, "TMDB : clé refusée"):
            self.run_pass(FakeRatings({"movie/1": enrich.TmdbAuthError("clé refusée")}))


class TestChosenSheet(TempCase):
    """La fiche TMDB choisie à la main, et les doutes de la recherche."""

    def setUp(self):
        super().setUp()
        self.lib = types.SimpleNamespace(id="series", path=self.tmp / "lib")
        self.db.replace_library("series", [item("s1", "series", "show", meta={"folder": "Hercule", "tmdb_pinned": "99"})], [])
        self.row = self.db.one("SELECT * FROM items WHERE id=?", ("s1",))
        self.tmdb = FakeShow()

    def enrich(self, resolve=42, show_id=4615):
        with mock.patch.object(enrich.lookup, "resolve_show_id", **({"side_effect": resolve} if callable(resolve) else {"return_value": resolve})) as patched:
            found = enrich.choose_show(self.tmdb, self.tmp / "posters", self.db, self.lib, self.row, show_id)
        return found, patched

    def test_the_choice_made_in_the_hub_comes_before_the_folder_name(self):
        _, resolve = self.enrich(show_id=4615)
        self.assertEqual(resolve.call_args.args[2], 4615)
        self.assertEqual(self.db.kv_get(enrich.pin_key(self.row)), 4615)

    def test_without_a_choice_the_folder_name_decides_as_before(self):
        with mock.patch.object(enrich.lookup, "resolve_show_id", return_value=42) as resolve:
            enrich.enrich_show(self.tmdb, self.tmp / "posters", self.db, self.lib, self.row)
        self.assertEqual(resolve.call_args.args[2], "99")

    def test_the_choice_is_not_kept_when_tmdb_refuses_it(self):
        self.enrich(show_id=4615)
        self.tmdb.series = mock.Mock(side_effect=enrich.TmdbError("introuvable"))
        with self.assertRaises(enrich.TmdbError):
            self.enrich(show_id=777)
        self.assertEqual(self.db.kv_get(enrich.pin_key(self.row)), 4615)          # celui d'avant

    def test_a_choice_that_tmdb_refuses_leaves_no_choice_when_there_was_none(self):
        self.tmdb.series = mock.Mock(side_effect=enrich.TmdbError("introuvable"))
        with self.assertRaises(enrich.TmdbError):
            self.enrich(show_id=777)
        self.assertIsNone(self.db.kv_get(enrich.pin_key(self.row)))

    def test_the_choice_survives_what_a_scan_does_with_the_enrichment(self):
        self.enrich(show_id=4615)
        self.assertNotIn(enrich.pin_key(self.row), scan.enriched_for(self.db, "series").keys())
        self.assertEqual(scan.enriched_for(self.db, "series")["s1"]["tmdb"], 42)

    def test_the_doubts_of_the_search_are_kept_without_the_advice_for_the_command_line(self):
        def search(*args):
            print("recherche : 'Hercule' -> Hercule (1998)")
            print("/!\\ titre partagé avec Hercule (1995) -> vérifie l'année, ou force --tmdb-id")
            print("/!\\ titre trouvé éloigné de la recherche -> à vérifier")
            return 42
        (_, data), _ = self.enrich(resolve=search)
        self.assertEqual(data["doubts"], ["titre partagé avec Hercule (1995)", "titre trouvé éloigné de la recherche"])
        self.assertEqual(json.loads(self.db.one("SELECT meta FROM items WHERE id='s1'")["meta"])["doubts"], data["doubts"])

    def test_a_doubt_that_is_lifted_disappears_from_the_sheet(self):
        self.enrich(resolve=lambda *a: print("/!\\ titre partagé avec X") or 42)
        self.enrich(show_id=4615)
        self.assertEqual(json.loads(self.db.one("SELECT meta FROM items WHERE id='s1'")["meta"])["doubts"], [])

    def test_the_journal_of_a_pass_tells_each_doubt(self):
        log = []
        found = ({"name": "Hercule"}, {"year": "1998", "doubts": ["titre partagé avec Hercule (1995)"]})
        cfg = types.SimpleNamespace(tmdb_key="cle", data=self.tmp)
        with mock.patch.object(enrich, "tmdb_client", return_value=(None, self.tmp)), mock.patch.object(enrich, "enrich_show", return_value=found):
            enrich.enrich_shows(cfg, self.db, self.lib, log.append)
        self.assertEqual(len(log), 2)
        self.assertIn("titre partagé avec Hercule (1995)", log[1])


class TestEnrichShows(TempCase):
    def setUp(self):
        super().setUp()
        self.lib = types.SimpleNamespace(id="series", path=self.tmp / "lib")
        self.cfg = types.SimpleNamespace(tmdb_key="cle", data=self.tmp)
        self.db.replace_library("series", [item(name.lower(), "series", "show", title=name) for name in ("A", "B", "C", "D")], [])

    def run_pass(self, outcomes, **options):
        log, seen = [], []

        def enrich_show(tmdb, posters, db, lib, row):
            seen.append(row["title"])
            outcome = outcomes[row["title"]]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with mock.patch.object(enrich, "tmdb_client", return_value=(None, self.tmp)), mock.patch.object(enrich, "enrich_show", enrich_show):
            counts = enrich.enrich_shows(self.cfg, self.db, self.lib, log.append, **options)
        return counts, log, seen

    def test_the_counts_and_the_journal_tell_what_happened_to_each_show(self):
        found = ({"name": "Dark"}, {"year": "2017"})
        counts, log, _ = self.run_pass({"A": found, "B": None, "C": enrich.TmdbError("boum"), "D": OSError("disque plein")})
        self.assertEqual(counts, {"enriched": 1, "failed": 3})
        self.assertEqual(log, ["  A -> Dark (2017)", "  B : série introuvable sur TMDB", "  C : boum", "  D : disque plein"])

    def test_a_show_already_done_is_skipped_unless_forced(self):
        done = self.db.one("SELECT * FROM items WHERE id=?", ("a",))
        self.db.kv_set(enrich.show_key(self.lib, done), {"season_summaries": {}, "doubts": []})
        found = ({"name": "Nom"}, {"year": ""})
        outcomes = {name: found for name in "ABCD"}
        self.assertEqual(self.run_pass(outcomes)[2], ["B", "C", "D"])
        self.assertEqual(self.run_pass(outcomes, force=True)[2], ["A", "B", "C", "D"])

    def test_an_authentication_error_stops_the_pass(self):
        with self.assertRaisesRegex(RuntimeError, "TMDB : clé refusée"):
            self.run_pass({"A": enrich.TmdbAuthError("clé refusée")})


class TestSagaPoster(TempCase):
    def poster(self, force=False, present=False):
        posters = self.tmp / "posters"
        posters.mkdir(exist_ok=True)
        saved = []

        class Tmdb:
            def save_image(self, path, size, dest):
                saved.append((path, size, dest.name))
                dest.write_bytes(b"jpeg")

        if present:
            (posters / "film-5.jpg").write_bytes(b"ancienne")
        run = enrich.SagaPass(Tmdb(), posters, {"films": {}, "collections": {}}, {}, print, force)
        return run.poster({"id": 5, "poster_path": "/x.jpg"}, "Film 5"), saved, (posters / "film-5.jpg").read_bytes()

    def test_a_missing_poster_is_downloaded_and_a_present_one_is_kept(self):
        self.assertEqual(self.poster(), ("film-5.jpg", [("/x.jpg", "w342", "film-5.jpg.tmp")], b"jpeg"))
        self.assertEqual(self.poster(present=True), ("film-5.jpg", [], b"ancienne"))

    def test_force_downloads_it_again(self):
        self.assertEqual(self.poster(force=True, present=True), ("film-5.jpg", [("/x.jpg", "w342", "film-5.jpg.tmp")], b"jpeg"))


class TestRefreshAge(unittest.TestCase):
    def refresh(self, known, force=False, now=10 * enrich.missing.STALE):
        run = enrich.SagaPass(None, None, {"films": {}, "collections": {"Saga": known}}, {}, print, force)
        run.now = now
        with mock.patch.object(run, "collection_of", return_value=None) as asked, mock.patch.object(run, "describe_missing") as only_new:
            run.refresh("Saga", [])
        return asked.called, only_new.called

    def test_a_collection_known_for_less_than_a_month_only_describes_the_new_films(self):
        now = 10 * enrich.missing.STALE
        self.assertEqual(self.refresh({"films": [], "at": now - enrich.missing.STALE + 1}), (False, True))

    def test_a_collection_known_for_exactly_a_month_is_asked_again(self):
        now = 10 * enrich.missing.STALE
        self.assertEqual(self.refresh({"films": [], "at": now - enrich.missing.STALE}), (True, False))

    def test_a_collection_without_a_date_is_as_old_as_the_clock(self):
        self.assertEqual(self.refresh({"films": []}), (True, False))
        self.assertEqual(self.refresh({"films": []}, now=enrich.missing.STALE), (True, False))
        self.assertEqual(self.refresh({"films": []}, now=enrich.missing.STALE - 1), (False, True))

    def test_force_asks_again_whatever_the_age(self):
        self.assertEqual(self.refresh({"films": [], "at": 10 * enrich.missing.STALE}, force=True), (True, False))


class TestEnrichSagas(TempCase):
    def read_asked(self, **options):
        lib = types.SimpleNamespace(id="films", path=self.tmp)
        with mock.patch.object(enrich, "tmdb_client", return_value=(object(), self.tmp)) as client:
            enrich.enrich_sagas(types.SimpleNamespace(data=self.tmp), self.db, lib, lambda line: None, **options)
        return client.call_args.kwargs["read"]

    def test_the_cache_of_tmdb_is_read_unless_forced(self):
        self.assertIs(self.read_asked(), True)
        self.assertIs(self.read_asked(force=True), False)


class TestWantedLinks(unittest.TestCase):
    def links(self, kind, url):
        return enrich.wanted_links({"relations": [{"type": kind, "url": {"resource": url}}]})

    def test_a_wanted_type_on_a_web_address_is_kept_with_its_label(self):
        self.assertEqual(self.links("youtube", "https://www.youtube.com/c/x"), [{"label": "YouTube", "url": "https://www.youtube.com/c/x"}])
        self.assertEqual(self.links("official homepage", "http://exemple.fr"), [{"label": "Site officiel", "url": "http://exemple.fr"}])

    def test_the_type_the_scheme_and_the_host_must_each_be_right(self):
        for kind, url in (("wikipedia", "https://youtube.com/x"), ("youtube", "ftp://youtube.com/x"), ("youtube", "https://"), ("youtube", "javascript:alert(1)"),
                          ("youtube", "")):
            self.assertEqual(self.links(kind, url), [], (kind, url))


class TestSheetIsFresh(unittest.TestCase):
    def sheet(self, days=0, **info):
        info = {"sheet": enrich.SHEET_VERSION, **info}
        return {"fetched": (date.today() - timedelta(days=days)).isoformat(), "info": info}

    def test_a_recent_sheet_of_the_current_version_is_fresh(self):
        self.assertTrue(enrich.sheet_is_fresh(self.sheet()))

    def test_a_sheet_kept_with_a_photo_key_by_an_older_version_is_still_fresh(self):
        self.assertTrue(enrich.sheet_is_fresh(self.sheet(photo="a1.webp")))

    def test_anything_less_is_done_again(self):
        for kept in (None, {}, self.sheet(days=400), self.sheet(sheet=enrich.SHEET_VERSION - 1), self.sheet(sheet=None)):
            self.assertFalse(enrich.sheet_is_fresh(kept), kept)


class TestArtistPass(TempCase):
    def setUp(self):
        super().setUp()
        self.lib = types.SimpleNamespace(id="musique", path=self.tmp)
        self.cfg = types.SimpleNamespace(data=self.tmp)
        self.db.replace_library("musique", [item("alb1", "musique", "album", grp="Zeta", meta={"artist_mbid": "a-id", "artist_id": "a1"}),
                                            item("alb2", "musique", "album", grp="alpha", meta={"artist_mbid": "z-id", "artist_id": "a2"})], [])

    def run_pass(self, fail=(), **options):
        log, asked = [], []

        def fetch(mb, mbid):
            asked.append(mbid)
            if mbid in fail:
                raise enrich.MusicBrainzError("hors ligne")
            return {}

        with mock.patch.object(enrich, "musicbrainz"), mock.patch.object(enrich, "fetch_artist", fetch):
            counts = enrich.enrich_artists(self.cfg, self.db, self.lib, log.append, **options)
        return counts, log, asked

    def test_the_artists_are_taken_in_the_order_of_their_names_not_of_their_identifiers(self):
        _, log, asked = self.run_pass()
        self.assertEqual((asked, [line.split(" :")[0] for line in log]), (["z-id", "a-id"], ["  alpha", "  Zeta"]))

    def test_a_sheet_that_cannot_be_fetched_is_counted_and_said(self):
        counts, log, _ = self.run_pass(fail=("a-id",))
        self.assertEqual(counts, {"enriched": 1, "failed": 1})
        self.assertIn("  Zeta : hors ligne", log)

    def test_a_fresh_sheet_is_not_fetched_again_unless_forced(self):
        sheet = {"fetched": date.today().isoformat(), "info": {"sheet": enrich.SHEET_VERSION}}
        self.db.kv_set(enrich.artist_key("a-id"), sheet)
        self.assertEqual(self.run_pass()[2], ["z-id"])
        self.db.kv_set(enrich.artist_key("a-id"), sheet)
        self.assertEqual(self.run_pass(force=True)[2], ["z-id", "a-id"])


if __name__ == "__main__":
    unittest.main()
