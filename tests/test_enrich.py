"""L'enrichissement, sans réseau : ce qu'on retient d'une série, d'une collection de saga, la photo d'un artiste et le journal d'une passe."""

from __future__ import annotations

import json
import shutil
import tempfile
import types
import unittest
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


if __name__ == "__main__":
    unittest.main()
