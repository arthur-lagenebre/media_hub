"""missing.tagged : les rangs et les manquants d'une saga de l'étiquette, d'après la collection TMDB."""

from __future__ import annotations

import json
import unittest

from hub import missing


def row(ident, tmdb):
    return {"id": ident, "meta": json.dumps({"tmdb": f"movie/{tmdb}"})}


def info(collection, known=()):
    return {"collections": {"Saga": {"films": collection}}, "films": {str(i): {"title": f"Film {i}", "date": "2000-01-01"} for i in known}}


class TestTagged(unittest.TestCase):
    def test_the_films_we_have_take_their_place_in_the_collection_and_the_others_are_missing(self):
        ranks, absent = missing.tagged("Saga", [row("a", 1), row("c", 3)], info([1, 2, 3], known=[2]), set())
        self.assertEqual(ranks, {"a": 1, "c": 3})
        self.assertEqual([(m["tmdb"], m["rank"]) for m in absent], [(2, 2)])

    def test_two_versions_of_a_film_share_its_place_and_do_not_stop_the_numbering(self):
        members = [row("a", 1), row("c1", 3), row("c2", 3)]
        ranks, absent = missing.tagged("Saga", members, info([1, 2, 3], known=[2]), set())
        self.assertEqual(ranks, {"a": 1, "c1": 3, "c2": 3})
        self.assertEqual([m["tmdb"] for m in absent], [2])

    def test_a_film_outside_the_collection_still_makes_the_name_untrusted(self):
        self.assertEqual(missing.tagged("Saga", [row("a", 1), row("z", 99)], info([1, 2]), set()), (None, []))


if __name__ == "__main__":
    unittest.main()
