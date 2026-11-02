"""L'enrichissement, sans réseau : ce qu'on retient d'une série, d'une collection de saga, la photo d'un artiste et le journal d'une passe."""

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

from hub import enrich  # noqa: E402
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
        self.db.kv_set(enrich.show_key(self.lib, self.show), {"season_summaries": {}})
        self.assertTrue(enrich.show_is_done(self.db, self.lib, self.show, False))

    def test_a_sheet_from_before_the_seasons_is_done_again(self):
        self.db.kv_set(enrich.show_key(self.lib, self.show), {"summary": "x"})
        self.assertFalse(enrich.show_is_done(self.db, self.lib, self.show, False))

    def test_force_does_everything_again(self):
        self.db.kv_set(enrich.show_key(self.lib, self.show), {"season_summaries": {"1": "x"}})
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


class TestPhotoBounds(unittest.TestCase):
    def fetch(self, data):
        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n):
                return data[:n]

        with mock.patch.object(enrich, "urlopen", lambda request, timeout=30: Reply()), mock.patch.object(enrich, "PHOTO_MAX", 40):
            return enrich.fetch_photo("https://upload.wikimedia.org/a.jpg")

    def test_a_photo_of_exactly_the_maximum_size_is_kept_and_a_byte_more_is_refused(self):
        exact = JPEG + b"x" * (40 - len(JPEG))
        self.assertEqual(self.fetch(exact), exact)
        with self.assertRaises(OSError):
            self.fetch(exact + b"x")


class TestPhotoWidth(unittest.TestCase):
    def urls(self, width):
        page = {"thumbnail": {"source": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/D.jpg/320px-D.jpg"},
                "originalimage": {"source": "https://upload.wikimedia.org/wikipedia/commons/a/ab/D.jpg", "width": width}}
        mb = types.SimpleNamespace(external=lambda url: page)
        return enrich.photo_urls(mb, {"lang": "fr", "title": "D"})

    def test_an_original_as_wide_as_the_header_needs_no_bigger_version(self):
        self.assertEqual(len(self.urls(enrich.PHOTO_WIDTH)), 2)
        self.assertEqual(len(self.urls(enrich.PHOTO_WIDTH + 1)), 3)

    def test_only_the_width_of_the_thumbnail_changes_even_when_the_file_name_looks_like_one(self):
        page = {"thumbnail": {"source": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/120px-Daft.jpg/320px-120px-Daft.jpg"},
                "originalimage": {"source": "https://upload.wikimedia.org/wikipedia/commons/a/ab/120px-Daft.jpg", "width": 5000}}
        found = enrich.photo_urls(types.SimpleNamespace(external=lambda url: page), {"lang": "fr", "title": "D"})
        self.assertEqual(found[0], f"https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/120px-Daft.jpg/{enrich.PHOTO_WIDTH}px-120px-Daft.jpg")


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

    def test_the_journal_gives_the_facts_of_the_sheet_and_notes_a_photo(self):
        (self.tmp / "artists").mkdir()
        (self.tmp / "artists" / "a1.jpg").write_bytes(JPEG)
        counts, log = self.run_pass({"type": "Group", "area": "France", "begin": "1993", "end": "2021"})
        self.assertEqual((counts, log), ({"enriched": 1, "failed": 0}, ["  Daft : Groupe · France · 1993 – 2021 · photo"]))

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
        self.db.kv_set(enrich.show_key(self.lib, done), {"season_summaries": {}})
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
        info = {"photo": "", "sheet": enrich.SHEET_VERSION, **info}
        return {"fetched": (date.today() - timedelta(days=days)).isoformat(), "info": info}

    def test_a_recent_sheet_with_its_photo_and_the_current_version_is_fresh(self):
        self.assertTrue(enrich.sheet_is_fresh(self.sheet()))

    def test_anything_less_is_done_again(self):
        old = self.sheet()
        del old["info"]["photo"]
        for kept in (None, {}, self.sheet(days=400), old, self.sheet(sheet=enrich.SHEET_VERSION - 1), self.sheet(sheet=None)):
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
        sheet = {"fetched": date.today().isoformat(), "info": {"photo": "", "sheet": enrich.SHEET_VERSION}}
        self.db.kv_set(enrich.artist_key("a-id"), sheet)
        self.assertEqual(self.run_pass()[2], ["z-id"])
        self.db.kv_set(enrich.artist_key("a-id"), sheet)
        self.assertEqual(self.run_pass(force=True)[2], ["z-id", "a-id"])


if __name__ == "__main__":
    unittest.main()
