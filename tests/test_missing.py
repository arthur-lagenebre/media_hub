"""Les films qui manquent : `tagged` (les rangs et les manquants d'une saga de l'étiquette, d'après la collection TMDB), et ce que `missing` lit et dit des films (`load`, `released`, `film_entry`,
`film_of_entry`, `owned_films`, `of_person`, `listed`)."""

from __future__ import annotations

import json
import shutil
import tempfile
import types
import unittest
from datetime import date
from pathlib import Path

from hub import missing
from hub.db import Database
from tests.test_server_views import entry, item


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

    def test_a_missing_film_is_ignorable_and_one_asked_to_hide_is_left_out(self):
        _, absent = missing.tagged("Saga", [row("a", 1)], info([1, 2, 3], known=[2, 3]), set())
        self.assertEqual([(m["tmdb"], m["ignorable"]) for m in absent], [(2, True), (3, True)])
        _, absent = missing.tagged("Saga", [row("a", 1)], info([1, 2, 3], known=[2, 3]), {2})
        self.assertEqual([m["tmdb"] for m in absent], [3])

    def test_a_film_announced_keeps_its_place_but_is_not_shown_and_an_unknown_saga_gives_nothing(self):
        data = info([1, 2, 3], known=[2, 3])
        data["films"]["2"]["date"] = "9999-01-01"
        ranks, absent = missing.tagged("Saga", [row("a", 1), row("c", 3)], data, set())
        self.assertEqual((ranks, absent), ({"a": 1, "c": 3}, []))
        self.assertEqual(missing.tagged("Autre", [row("a", 1)], data, set()), (None, []))


class DbCase(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        self.db = Database(tmp / "hub.db")
        self.addCleanup(self.db.connection().close)


class TestLoad(DbCase):
    def test_nothing_kept_gives_empty_films_and_collections(self):
        self.assertEqual(missing.load(self.db), {"films": {}, "collections": {}})

    def test_what_is_kept_comes_back_and_a_missing_half_is_empty(self):
        kept = {"films": {"7": {"title": "Sept"}}, "collections": {"Saga": {"id": 1, "films": [7], "at": 5}}}
        self.db.kv_set(missing.KEY, kept)
        self.assertEqual(missing.load(self.db), kept)
        self.db.kv_set(missing.KEY, {"films": {"7": {"title": "Sept"}}})
        self.assertEqual(missing.load(self.db), {"films": {"7": {"title": "Sept"}}, "collections": {}})
        self.db.kv_set(missing.KEY, {"films": None, "collections": {"Saga": {}}})
        self.assertEqual(missing.load(self.db), {"films": {}, "collections": {"Saga": {}}})

    def test_save_keeps_it_under_the_key(self):
        missing.save(self.db, {"films": {"1": {}}, "collections": {}})
        self.assertEqual(self.db.kv_get(missing.KEY), {"films": {"1": {}}, "collections": {}})


class TestReleased(unittest.TestCase):
    def test_a_past_date_and_the_day_itself_are_released_a_later_one_is_not(self):
        self.assertTrue(missing.released({"date": "2020-01-01"}, now="2020-01-02"))
        self.assertTrue(missing.released({"date": "2020-01-02"}, now="2020-01-02"))
        self.assertFalse(missing.released({"date": "2020-01-03"}, now="2020-01-02"))

    def test_no_date_is_never_released_even_far_in_the_future(self):
        for film in ({}, {"date": ""}, {"date": None}):
            self.assertFalse(missing.released(film, now="9999-12-31"), film)

    def test_only_the_day_of_the_date_counts(self):
        self.assertTrue(missing.released({"date": "2020-01-02T23:59:59"}, now="2020-01-02"))

    def test_without_a_given_day_it_is_today(self):
        today = date.today()
        self.assertTrue(missing.released({"date": today.isoformat()}))
        self.assertFalse(missing.released({"date": today.replace(year=today.year + 1, day=1).isoformat()}))
        self.assertTrue(missing.released({"date": "1999-01-01"}))
        self.assertEqual(missing.today(), today.isoformat())


class TestFilmEntry(unittest.TestCase):
    def test_a_described_film_is_given_as_the_api_does(self):
        self.assertEqual(missing.film_entry(7, 3, {"title": "Sept", "year": "1999", "poster": "film-7.jpg"}),
                         {"tmdb": 7, "title": "Sept", "year": "1999", "rank": 3, "cover": "/img/film/7"})

    def test_a_film_without_title_year_or_poster_gets_empty_strings(self):
        self.assertEqual(missing.film_entry(7, 1, {}), {"tmdb": 7, "title": "", "year": "", "rank": 1, "cover": ""})
        self.assertEqual(missing.film_entry(7, 1, {"title": None, "year": None, "poster": ""}), {"tmdb": 7, "title": "", "year": "", "rank": 1, "cover": ""})

    def test_entry_reads_the_film_of_the_info_by_its_identifier(self):
        data = {"films": {"7": {"title": "Sept", "year": "1999", "poster": ""}}}
        self.assertEqual(missing.entry(7, 2, data), {"tmdb": 7, "title": "Sept", "year": "1999", "rank": 2, "cover": ""})


class TestFilmOfEntry(unittest.TestCase):
    def tag(self, tmdb):
        return json.dumps({"info": {"tmdb": tmdb}})

    def test_a_movie_tag_gives_its_number(self):
        self.assertEqual(missing.film_of_entry(self.tag("movie/27205")), 27205)

    def test_any_other_tag_gives_none(self):
        for tag in ("tv/27205", "27205", 27205, None, ["movie/1"], True, "movie/", "movie/abc"):
            self.assertIsNone(missing.film_of_entry(self.tag(tag)), tag)

    def test_a_meta_that_says_nothing_usable_gives_none(self):
        for text in ("pas du json", None, "[]", "5", json.dumps({}), json.dumps({"info": None}), json.dumps({"info": "movie/1"}), json.dumps({"info": ["movie/1"]})):
            self.assertIsNone(missing.film_of_entry(text), text)


def lib(lib_id, kind):
    return types.SimpleNamespace(id=lib_id, kind=kind)


class TestOwnedFilms(DbCase):
    def setUp(self):
        super().setUp()
        self.db.replace_library("films", [item("m1", lib="films", meta={"tmdb": "movie/1"}), item("m2", lib="films", meta={"tmdb": 2}), item("m3", lib="films", title="sans étiquette")], [])
        self.db.replace_library("autres", [item("a1", lib="autres", meta={"tmdb": "movie/5"})], [])
        self.db.replace_library("series", [item("s1", "show", lib="series", meta={"tmdb": 8}), item("s2", "show", lib="series")], [
            entry("e1", "s1", lib="series", meta={"info": {"tmdb": "movie/9"}}), entry("e2", "s1", lib="series", meta={"info": {"tmdb": "tv/8"}}),
            entry("e3", "s2", lib="series", meta={"info": {"tmdb": "movie/10"}}), entry("e4", "s2", lib="series", meta={"info": {"tmdb": 11}})])
        self.db.replace_library("musique", [item("album", "album", lib="musique", meta={"tmdb": "movie/12"})], [])

    def test_the_films_of_the_movie_libraries_and_the_movie_files_of_the_show_libraries(self):
        libs = [lib("films", "movies"), lib("autres", "movies"), lib("series", "shows"), lib("musique", "music")]
        self.assertEqual(missing.owned_films(self.db, libs), {1, 2, 5, 9, 10})

    def test_only_the_libraries_given_count(self):
        self.assertEqual(missing.owned_films(self.db, [lib("films", "movies")]), {1, 2})
        self.assertEqual(missing.owned_films(self.db, [lib("series", "shows")]), {9, 10})
        self.assertEqual(missing.owned_films(self.db, [lib("films", "movies"), lib("series", "shows")]), {1, 2, 9, 10})
        self.assertEqual(missing.owned_films(self.db, [lib("musique", "music")]), set())
        self.assertEqual(missing.owned_films(self.db, []), set())


class TestOfPerson(unittest.TestCase):
    FILMS = [{"tmdb": 3, "title": "Trois", "year": "2012", "role": "actor", "poster": "film-3.jpg"},
             {"tmdb": 2, "title": "Deux", "year": "2006", "role": "director", "poster": ""},
             {"tmdb": 1, "title": "Un", "year": "1999", "role": "actor", "poster": "film-1.jpg"}]

    def test_the_films_we_do_not_own_are_ranked_from_one_in_their_order(self):
        self.assertEqual(missing.of_person(self.FILMS, {2}), [
            {"tmdb": 3, "title": "Trois", "year": "2012", "rank": 1, "cover": "/img/film/3", "role": "actor"},
            {"tmdb": 1, "title": "Un", "year": "1999", "rank": 2, "cover": "/img/film/1", "role": "actor"}])
        self.assertEqual([f["rank"] for f in missing.of_person(self.FILMS, set())], [1, 2, 3])


class TestListed(unittest.TestCase):
    INFO = {"films": {"1": {"title": "Un", "year": "1999", "date": "1999-01-01", "poster": "film-1.jpg"}, "2": {"title": "Deux", "year": "2006", "date": "2006-01-01", "poster": ""},
                      "3": {"title": "Trois", "year": "", "date": "", "poster": ""}, "4": {"title": "Quatre", "year": "9999", "date": "9999-01-01", "poster": ""}}}

    def test_the_films_of_the_list_we_do_not_own_and_that_are_described_keep_their_place_in_the_list(self):
        listed = missing.listed([1, 9, 2, 3], {2}, self.INFO)
        self.assertEqual([(f["tmdb"], f["rank"]) for f in listed], [(1, 1), (3, 4)])        # 9 n'est pas décrit : sauté, mais compté dans le rang
        self.assertEqual(listed[0], {"tmdb": 1, "title": "Un", "year": "1999", "rank": 1, "cover": "/img/film/1"})

    def test_a_film_not_yet_released_or_without_a_date_is_marked_coming_and_the_others_are_not(self):
        listed = {f["tmdb"]: f for f in missing.listed([1, 3, 4], set(), self.INFO)}
        self.assertNotIn("coming", listed[1])
        self.assertIs(listed[3]["coming"], True)
        self.assertIs(listed[4]["coming"], True)

    def test_nothing_listed_or_everything_owned_gives_nothing(self):
        self.assertEqual(missing.listed([], set(), self.INFO), [])
        self.assertEqual(missing.listed([1, 2], {1, 2}, self.INFO), [])


if __name__ == "__main__":
    unittest.main()
