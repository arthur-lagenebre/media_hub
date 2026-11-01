"""Chercher la collection TMDB d'une saga : ce que dit le journal, ce qui est compté en échec, ce qui arrête tout."""

from __future__ import annotations

import json
import unittest

from tests import fixtures
from hub import config

fixtures.REPO  # noqa: B018  (rend `libraries` importable)
config.use_media_library(fixtures.REPO)

from libraries.video.tmdb import TmdbAuthError, TmdbError  # noqa: E402

from hub.enrich import SagaPass  # noqa: E402


class FakeTmdb:
    def __init__(self, movie=None, parts=None, error=None):
        self.movie, self.parts, self.error = movie or {}, parts or [], error

    def get(self, endpoint):
        if self.error:
            raise self.error
        return self.movie

    def collection(self, collection_id):
        return {"parts": self.parts}


def film(tmdb):
    return {"meta": json.dumps({"tmdb": f"movie/{tmdb}" if tmdb else ""})}


class TestCollectionOf(unittest.TestCase):
    def run_pass(self, tmdb, members):
        log = []
        run = SagaPass(tmdb, None, {"films": {}, "collections": {}}, {}, log.append, False)
        return run, log, run.collection_of("Ma saga", members)

    def test_the_collection_and_its_films(self):
        parts = [{"id": 1}, {"id": 2}]
        run, log, found = self.run_pass(FakeTmdb({"belongs_to_collection": {"id": 9000}}, parts), [film(0), film(49521)])
        self.assertEqual(found, (9000, parts))                                  # le premier film qui a un identifiant TMDB désigne la collection
        self.assertEqual((log, run.counts["failed"]), ([], 0))

    def test_no_film_with_a_tmdb_id_is_noted_but_is_not_a_failure(self):
        run, log, found = self.run_pass(FakeTmdb(), [film(0), film(0)])
        self.assertIsNone(found)
        self.assertEqual((log, run.counts["failed"]), (["  Ma saga : aucun film n'a d'identifiant TMDB"], 0))

    def test_a_film_in_no_collection_is_a_failure(self):
        for movie in ({}, {"belongs_to_collection": None}, {"belongs_to_collection": {}}):
            run, log, found = self.run_pass(FakeTmdb(movie), [film(1)])
            self.assertIsNone(found)
            self.assertEqual((log, run.counts["failed"]), (["  Ma saga : TMDB ne rattache ce film à aucune collection"], 1))

    def test_a_tmdb_error_is_a_failure_and_a_refused_key_stops_everything(self):
        run, log, found = self.run_pass(FakeTmdb(error=TmdbError("introuvable sur TMDB (HTTP 404)")), [film(1)])
        self.assertIsNone(found)
        self.assertEqual((log, run.counts["failed"]), (["  Ma saga : introuvable sur TMDB (HTTP 404)"], 1))
        with self.assertRaises(TmdbAuthError):
            self.run_pass(FakeTmdb(error=TmdbAuthError("clé refusée")), [film(1)])

    def test_a_collection_without_films_is_an_empty_list(self):
        self.assertEqual(self.run_pass(FakeTmdb({"belongs_to_collection": {"id": 5}}, None), [film(1)])[2], (5, []))


if __name__ == "__main__":
    unittest.main()
