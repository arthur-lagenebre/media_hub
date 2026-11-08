"""Ce que le serveur montre, sans serveur : fiches, progression et cartes « Reprendre », sagas regroupées, tri, pagination, réglages de lecture, fichiers du site."""

from __future__ import annotations

import json
import shutil
import tempfile
import types
import unittest
from pathlib import Path

from tests import fixtures
from hub import config

fixtures.REPO  # noqa: B018  (rend `libraries` importable)
config.use_media_library(fixtures.REPO)

from libraries.common.text import fold, initial  # noqa: E402

from hub import urls  # noqa: E402
from hub.db import Database, make_id  # noqa: E402
from hub.server import app  # noqa: E402
from hub.server.app import HttpError  # noqa: E402


def item(ident, kind="movie", lib="lib", title=None, meta=None, **over):
    title = title or ident
    return {"id": ident, "lib": lib, "type": kind, "title": title, "sort": title.casefold(), "letter": title[:1].upper(), "year": "", "summary": "", "genres": "[]", "grp": "",
            "meta": json.dumps(meta or {}), "added": 0, "count": 1, **over}


def entry(ident, item_id, kind="video", lib="lib", number=None, season=None, meta=None, **over):
    return {"id": ident, "item_id": item_id, "lib": lib, "rel": f"{item_id}/{ident}", "kind": kind, "season": season, "disc": None, "number": number, "title": f"Titre {ident}",
            "summary": "", "duration": 100.0, "size": 1, "mtime": 1, "meta": json.dumps(meta or {}), "probe": None, **over}


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = Database(self.tmp / "hub.db")
        self.hub = types.SimpleNamespace(db=self.db)

    def fill(self, items, entries, lib="lib"):
        self.db.replace_library(lib, items, entries)

    def progress(self, profile, entry_id, position, duration, done, updated):
        self.db.save_progress(profile, entry_id, position, duration, done)
        self.db.execute("UPDATE progress SET updated=? WHERE profile=? AND entry_id=?", (updated, profile, entry_id))


class TestSummary(unittest.TestCase):
    def test_the_common_fields_of_a_thumbnail(self):
        row = item("m1", title="Alien", year="1979", grp="Saga", genres='["SF"]', count=2, added=5)
        self.assertEqual(app.summary(row), {"id": "m1", "type": "movie", "title": "Alien", "year": "1979", "grp": "Saga", "n": 2, "letter": "A", "added": 5, "genres": ["SF"],
                                            "cover": urls.item_cover("m1")})

    def test_the_seen_count_is_there_even_when_zero_but_only_when_given(self):
        self.assertNotIn("seen", app.summary(item("m1")))
        self.assertEqual(app.summary(item("m1"), 0)["seen"], 0)
        self.assertEqual(app.summary(item("m1"), 3)["seen"], 3)

    def test_an_album_gives_its_duration_and_its_artist_when_it_has_them(self):
        out = app.summary(item("a1", "album", meta={"duration": 3600, "artist_id": "x1"}))
        self.assertEqual((out["duration"], out["artist_id"]), (3600, "x1"))
        out = app.summary(item("a2", "album", meta={}))
        self.assertNotIn("duration", out)
        self.assertNotIn("artist_id", out)
        out = app.summary(item("m1", "movie", meta={"duration": 3600, "artist_id": "x1"}))
        self.assertNotIn("duration", out)
        self.assertNotIn("artist_id", out)

    def test_a_series_counts_the_episodes_it_lacks(self):
        missing = [{"numbers": [1, 2]}, {"numbers": [5]}]
        self.assertEqual(app.summary(item("s1", "series", meta={"missing": missing}))["missing"], 3)
        self.assertNotIn("missing", app.summary(item("s1", "series", meta={"missing": []})))
        self.assertNotIn("missing", app.summary(item("s1", "movie", meta={"missing": missing})))


class TestEntryJson(unittest.TestCase):
    def test_a_video_has_an_image_and_its_date(self):
        row = entry("e1", "i1", number=3, season=2, meta={"format": "mkv", "label": "1080p", "sub": "VF", "info": {"date": "2020-01-02"}})
        out = app.entry_json(row)
        self.assertEqual((out["id"], out["kind"], out["season"], out["number"], out["duration"], out["format"], out["label"], out["sub"], out["date"]),
                         ("e1", "video", 2, 3, 100.0, "mkv", "1080p", "VF", "2020-01-02"))
        self.assertEqual((out["file"], out["image"]), (urls.entry_file("e1"), urls.entry_image("e1")))
        self.assertNotIn("artist", out)
        self.assertNotIn("progress", out)

    def test_a_video_without_date_has_none(self):
        self.assertNotIn("date", app.entry_json(entry("e1", "i1")))
        self.assertNotIn("date", app.entry_json(entry("e1", "i1", kind="book", meta={"info": {"date": "2020"}})))

    def test_a_track_has_no_image_of_its_own_but_an_artist(self):
        out = app.entry_json(entry("e1", "i1", kind="audio", meta={"info": {"artist": "Daft"}}))
        self.assertNotIn("image", out)
        self.assertEqual(out["artist"], "Daft")
        self.assertEqual(app.entry_json(entry("e2", "i1", kind="audio"))["artist"], "")

    def test_a_track_gives_its_artists_when_the_file_names_them(self):
        artists = [{"name": "BABYMETAL", "id": "a"}, {"name": "Poppy", "id": "b"}]
        out = app.entry_json(entry("e1", "i1", kind="audio", meta={"info": {"artist": "BABYMETAL feat. Poppy", "artists": artists}}))
        self.assertEqual((out["artist"], out["artists"]), ("BABYMETAL feat. Poppy", artists))

    def test_a_track_without_artists_in_its_file_gives_no_list(self):
        self.assertNotIn("artists", app.entry_json(entry("e1", "i1", kind="audio", meta={"info": {"artist": "Daft", "artists": []}})))
        self.assertNotIn("artists", app.entry_json(entry("e2", "i1", kind="audio", meta={"info": {"artist": "Daft"}})))

    def test_a_book_gives_its_pages_when_known(self):
        self.assertEqual(app.entry_json(entry("e1", "i1", kind="book", meta={"info": {"PageCount": 180}}))["pages"], 180)
        self.assertNotIn("pages", app.entry_json(entry("e1", "i1", kind="book")))
        self.assertNotIn("pages", app.entry_json(entry("e1", "i1", kind="video", meta={"info": {"PageCount": 180}})))

    def test_the_progress_is_given_with_a_real_boolean(self):
        out = app.entry_json(entry("e1", "i1"), {"position": 5, "duration": 10, "done": 1, "updated": 7})
        self.assertEqual(out["progress"], {"position": 5, "duration": 10, "done": True, "updated": 7})
        self.assertIs(app.entry_json(entry("e1", "i1"), {"position": 0, "duration": 0, "done": 0, "updated": 0})["progress"]["done"], False)


class TestSeen(DbCase):
    def setUp(self):
        super().setUp()
        self.fill([item("i1"), item("i3")], [entry("e1", "i1"), entry("e2", "i1"), entry("e4", "i3", kind="audio")])
        self.fill([item("i2", lib="b")], [entry("e3", "i2", lib="b")], lib="b")
        for entry_id, done in (("e1", True), ("e2", False), ("e3", True), ("e4", True)):
            self.db.save_progress("p", entry_id, 5, 10, done)

    def test_the_files_seen_and_started_are_counted_by_item_without_the_music(self):
        self.assertEqual(app.seen_counts(self.hub, "p"), {"i1": (1, 2), "i2": (1, 1)})

    def test_a_library_can_be_asked_alone_and_each_account_has_its_own(self):
        self.assertEqual(app.seen_counts(self.hub, "p", "lib"), {"i1": (1, 2)})
        self.assertEqual(app.seen_counts(self.hub, "p", "b"), {"i2": (1, 1)})
        self.assertEqual(app.seen_counts(self.hub, "q"), {})

    def test_nothing_seen_but_started_counts_zero_seen(self):
        self.db.save_progress("q", "e1", 5, 10, False)
        self.assertEqual(app.seen_counts(self.hub, "q"), {"i1": (0, 1)})

    def test_with_seen_tells_how_many_are_seen_and_how_many_are_started(self):
        row = item("i1")
        self.assertNotIn("seen", app.with_seen(row, {}))
        self.assertNotIn("started", app.with_seen(row, {"i1": (3, 0)}))
        self.assertEqual({k: app.with_seen(row, {"i1": (1, 3)})[k] for k in ("seen", "started")}, {"seen": 1, "started": 2})
        self.assertEqual({k: app.with_seen(row, {"i1": (0, 2)})[k] for k in ("seen", "started")}, {"seen": 0, "started": 2})


class TestNextEntry(DbCase):
    def test_the_next_file_follows_the_order_of_the_episodes(self):
        self.fill([item("s1", "show")], [entry("e3", "s1", season=1, number=3), entry("e1", "s1", season=1, number=1), entry("e2", "s1", season=1, number=2)])
        get = lambda ident: self.db.one("SELECT * FROM entries WHERE id=?", (ident,))
        self.assertEqual(app.next_entry(self.hub, get("e1"))["id"], "e2")
        self.assertEqual(app.next_entry(self.hub, get("e2"))["id"], "e3")
        self.assertIsNone(app.next_entry(self.hub, get("e3")))
        self.assertIsNone(app.next_entry(self.hub, {"id": "inconnu", "item_id": "s1"}))


class ProgressCase(DbCase):
    def setUp(self):
        super().setUp()
        episodes = [entry(f"e{n}", "s1", season=1, number=n) for n in (1, 2, 3)]
        self.fill([item("s1", "show"), item("m1"), item("b1", "books")], episodes + [entry("f1", "m1"), entry("k1", "b1", kind="book", number=1), entry("k2", "b1", kind="book", number=2)])

    def cards(self, profile="p"):
        return app.continue_row(self.hub, profile)


class TestContinue(ProgressCase):
    def test_a_film_started_is_offered_where_it_stopped(self):
        self.progress("p", "f1", 30, 200, False, 1000)
        card, = self.cards()
        self.assertEqual((card["id"], card["position"], card["total"], card["resume"], card["updated"], card["item"]["id"]), ("f1", 30, 200, True, 1000, "m1"))

    def test_the_duration_of_the_file_stands_in_when_the_player_did_not_give_one(self):
        self.progress("p", "f1", 30, 0, False, 1000)
        self.assertEqual(self.cards()[0]["total"], 100.0)

    def test_a_position_at_the_very_beginning_is_not_offered(self):
        self.progress("p", "f1", app.RESUME_MIN - 1, 200, False, 1000)
        self.assertEqual(self.cards(), [])
        self.progress("p", "f1", app.RESUME_MIN, 200, False, 1000)
        self.assertEqual(len(self.cards()), 1)

    def test_a_book_is_offered_from_its_first_page(self):
        self.progress("p", "k1", 0, 10, False, 1000)
        self.assertEqual(self.cards(), [])
        self.progress("p", "k1", 1, 10, False, 1000)
        self.assertEqual(self.cards()[0]["id"], "k1")

    def test_a_finished_film_offers_nothing(self):
        self.progress("p", "f1", 200, 200, True, 1000)
        self.assertEqual(self.cards(), [])

    def test_a_finished_episode_offers_the_next_one_which_has_no_progress_yet(self):
        self.progress("p", "e1", 100, 100, True, 1000)
        card, = self.cards()
        self.assertEqual((card["id"], card["position"], card["total"], card["resume"], card["updated"]), ("e2", 0, 100.0, False, 1000))

    def test_the_next_episode_comes_back_where_it_was_left(self):
        self.progress("p", "e1", 100, 100, True, 1000)
        self.progress("p", "e2", 12, 90, False, 900)
        card, = self.cards()
        self.assertEqual((card["id"], card["position"], card["total"]), ("e2", 12, 90))

    def test_a_finished_last_episode_or_a_next_one_already_seen_offers_nothing(self):
        self.progress("p", "e3", 100, 100, True, 1000)
        self.assertEqual(self.cards(), [])
        self.progress("p", "e1", 100, 100, True, 1000)
        self.progress("p", "e2", 100, 100, True, 900)
        self.assertEqual(self.cards(), [])                 # l'épisode 1 est le dernier regardé : son suivant est vu, l'élément n'est pas proposé deux fois

    def test_each_item_appears_once_with_its_latest_file_and_the_latest_item_first(self):
        self.progress("p", "e1", 50, 100, False, 100)
        self.progress("p", "e2", 50, 100, False, 300)
        self.progress("p", "f1", 50, 100, False, 200)
        self.assertEqual([c["id"] for c in self.cards()], ["e2", "f1"])

    def test_each_account_sees_its_own(self):
        self.progress("p", "f1", 50, 100, False, 100)
        self.assertEqual(self.cards("q"), [])

    def test_a_dismissed_item_returns_when_it_is_watched_again(self):
        self.progress("p", "f1", 50, 100, False, 1000)
        self.db.kv_set(app.dismiss_key("p", "m1"), 1000)
        self.assertEqual(self.cards(), [])
        self.progress("p", "f1", 60, 100, False, 1001)
        self.assertEqual(len(self.cards()), 1)

    def test_without_a_refusal_a_progress_is_offered_whatever_its_date(self):
        for updated in (0, 1):
            self.progress("p", "f1", 50, 100, False, updated)
            self.assertEqual(len(self.cards()), 1, updated)

    def test_progress_on_a_file_whose_item_is_gone_is_ignored(self):
        self.progress("p", "f1", 50, 100, False, 1000)
        self.db.execute("DELETE FROM items WHERE id='m1'")
        self.assertEqual(self.cards(), [])

    def test_no_more_than_twenty_four_cards(self):
        items = [item(f"x{n}") for n in range(25)]
        self.fill(items, [entry(f"y{n}", f"x{n}") for n in range(25)], lib="beaucoup")
        for n in range(25):
            self.progress("p", f"y{n}", 50, 100, False, 100 + n)
        cards = self.cards()
        self.assertEqual((len(cards), cards[0]["id"]), (24, "y24"))
        self.db.execute("DELETE FROM progress WHERE entry_id='y0'")
        self.assertEqual(len(self.cards()), 24)
        for n in range(1, 24):
            self.db.execute("DELETE FROM progress WHERE entry_id=?", (f"y{n}",))
        self.assertEqual(len(self.cards()), 1)


class TestCards(ProgressCase):
    def test_the_card_of_a_file_is_its_json_with_its_item_and_where_it_stands(self):
        entry_row = self.db.one("SELECT * FROM entries WHERE id='f1'")
        card = app.progress_card(entry_row, self.db.one("SELECT * FROM items WHERE id='m1'"), 10, 0, True, 5)
        self.assertEqual((card["id"], card["position"], card["total"], card["resume"], card["updated"], card["item"]["title"], card["file"]), ("f1", 10, 100.0, True, 5, "m1", urls.entry_file("f1")))
        self.assertEqual(app.progress_card(entry_row, self.db.one("SELECT * FROM items WHERE id='m1'"), 10, 50, True, 5)["total"], 50)


class TestTaggedAndCollapsed(unittest.TestCase):
    def film(self, ident, grp, year="2000", genres=(), added=0, lib="lib", **over):
        return item(ident, title=f"Film {ident}", grp=grp, year=year, genres=json.dumps(list(genres)), added=added, lib=lib, **over)

    def test_only_sagas_of_the_tag_with_two_films_or_more_and_not_replaced_by_the_file_are_kept(self):
        rows = [self.film("a", "Alpha", "2002"), self.film("b", "Alpha", "2001"), self.film("c", "Seul"), self.film("d", ""), self.film("e", "Fichier"), self.film("f", "Fichier"),
                item("g", "show", grp="Alpha"), self.film("h", "Alpha", "2003")]
        found = app.tagged_groups(rows, {"Fichier": []})
        self.assertEqual(list(found), ["Alpha"])
        self.assertEqual([r["id"] for r in found["Alpha"]], ["b", "a", "h"])                  # par date de sortie

    def test_the_films_of_a_saga_become_one_thumbnail_and_the_others_stay_alone(self):
        rows = [self.film("a", "Alpha", "2002", ["SF", "Action"], added=3), self.film("b", "Alpha", "2001", ["Action", "Épopée"], added=9), self.film("c", "", "1999")]
        out = app.collapse_sagas(rows, {"a": (1, 0), "b": (1, 0)}, {})
        self.assertEqual([r["id"] for r in out if r["type"] != "saga"], ["c"])
        saga, = [r for r in out if r["type"] == "saga"]
        self.assertEqual((saga["id"], saga["lib"], saga["title"], saga["sort"], saga["letter"]), (make_id("lib", "saga:Alpha"), "lib", "Alpha", fold("Alpha"), initial("Alpha")))
        self.assertEqual((saga["year"], saga["years"], saga["added"], saga["count"], saga["grp"], saga["summary"], saga["meta"]), ("2001", ["2001", "2002"], 9, 2, "", "", "{}"))
        self.assertEqual((json.loads(saga["genres"]), [m["id"] for m in saga["members"]], saga["all_seen"]), (["Action", "Épopée", "SF"], ["b", "a"], True))
        self.assertIn("Épopée", saga["genres"])                      # les accents restent des accents dans le texte JSON

    def test_a_saga_is_not_all_seen_while_a_film_is_not(self):
        rows = [self.film("a", "Alpha"), self.film("b", "Alpha")]
        saga, = app.collapse_sagas(rows, {"a": (1, 0)}, {})
        self.assertFalse(saga["all_seen"])

    def test_films_without_year_give_a_saga_without_years(self):
        rows = [self.film("a", "Alpha", year=""), self.film("b", "Alpha", year="")]
        saga, = app.collapse_sagas(rows, {}, {})
        self.assertEqual((saga["year"], saga["years"]), ("", []))
        rows = [self.film("a", "Alpha", year=""), self.film("b", "Alpha", year="2005")]
        saga, = app.collapse_sagas(rows, {}, {})
        self.assertEqual((saga["year"], saga["years"]), ("2005", ["2005", "2005"]))

    def test_a_saga_of_the_file_gathers_films_whatever_their_tag(self):
        rows = [self.film("a", ""), self.film("b", ""), self.film("c", "")]
        custom = {"Ma saga": [(rows[1], 1), (rows[0], 2)]}
        out = app.collapse_sagas(rows, {}, custom)
        self.assertEqual([r["id"] for r in out if r["type"] != "saga"], ["c"])
        saga, = [r for r in out if r["type"] == "saga"]
        self.assertEqual([m["id"] for m in saga["members"]], ["b", "a"])

    def test_a_saga_of_the_file_with_a_single_film_stays_a_film_unless_it_replaces_a_tagged_saga(self):
        rows = [self.film("a", ""), self.film("b", "Alpha"), self.film("c", "Alpha")]
        self.assertEqual({r["type"] for r in app.collapse_sagas(rows, {}, {"Seule": [(rows[0], 1)]})}, {"movie", "saga"})
        out = app.collapse_sagas(rows, {}, {"Seule": [(rows[0], 1)]})
        self.assertEqual(sorted(r["id"] for r in out if r["type"] == "movie"), ["a"])


class TestLibraryOrder(unittest.TestCase):
    rows = [{"id": "x", "sort": "b", "added": 1, "year": "2001", "grp": "Zed", "meta": "{}"}, {"id": "y", "sort": "a", "added": 2, "year": "", "grp": "Abe", "meta": "{}"}]

    def order(self, kind, **q):
        sort, key, reverse = app.library_order(kind, q, None)
        return sort, [r["id"] for r in sorted(self.rows, key=key, reverse=reverse)]

    def test_films_follow_the_sort_asked(self):
        self.assertEqual(self.order("movies"), ("title", ["y", "x"]))
        self.assertEqual(self.order("movies", sort="added"), ("added", ["y", "x"]))
        self.assertEqual(self.order("movies", sort="year"), ("year", ["x", "y"]))
        self.assertEqual(self.order("movies", sort="year-asc"), ("year-asc", ["x", "y"]))

    def test_an_unknown_sort_is_the_title_one(self):
        self.assertEqual(self.order("movies", sort="n'importe quoi")[1], ["y", "x"])

    def test_music_and_books_are_sorted_by_artist_or_collection(self):
        self.assertEqual(self.order("music", sort="added"), ("title", ["y", "x"]))
        self.assertEqual(self.order("books"), ("title", ["y", "x"]))
        self.assertEqual(self.order("books", sort="added"), ("added", ["y", "x"]))

    def test_the_order_of_a_saga_follows_the_ranks_when_there_are_some(self):
        sort, key, reverse = app.library_order("movies", {"sort": "part"}, {"x": 1, "y": 2})
        self.assertEqual((sort, reverse, sorted(self.rows, key=key)[0]["id"]), ("part", False, "x"))
        sort, key, _ = app.library_order("movies", {"sort": "added"}, {"x": 9, "y": 1})
        self.assertEqual(sorted(self.rows, key=key, reverse=True)[0]["id"], "y")                  # « added » : le plus récent d'abord, quels que soient les rangs
        sort, key, _ = app.library_order("movies", {"sort": "part"}, None)
        self.assertEqual(key(self.rows[0]), app.release_key(self.rows[0]))


class TestRatingOrder(unittest.TestCase):
    """« Mieux notés » : la note TMDB montrée, puis le nombre de votes, puis le titre ; ce qui n'a pas de note montrée va à la fin, par titre."""

    def film(self, ident, sort, vote=None, votes=0):
        rating = {"tmdb": 1, "vote": vote, "votes": votes, "at": 0} if vote is not None or votes else None
        return {"id": ident, "sort": sort, "added": 0, "year": "", "grp": "", "meta": json.dumps({"tmdb_rating": rating} if rating else {})}

    def order(self, rows):
        sort, key, reverse = app.library_order("movies", {"sort": "rating"}, None)
        self.assertEqual(sort, "rating")
        return [r["id"] for r in sorted(rows, key=key, reverse=reverse)]

    def test_the_best_rated_come_first_then_the_most_voted_then_by_title(self):
        rows = [self.film("moyen", "m", 6.5, 900), self.film("bon-b", "b", 8.4, 100), self.film("bon-a", "a", 8.4, 100), self.film("bon-vote", "z", 8.4, 5000)]
        self.assertEqual(self.order(rows), ["bon-vote", "bon-a", "bon-b", "moyen"])

    def test_what_has_no_shown_rating_goes_last_by_title(self):
        rows = [self.film("sans", "b"), self.film("peu", "a", 9.9, app.MIN_VOTES - 1), self.film("note", "z", 3.0, 900), self.film("ancien", "c", None, 0)]
        rows[3]["meta"] = json.dumps({"tmdb_rating": 7.8})                  # la forme d'avant : pas une note montrée
        self.assertEqual(self.order(rows), ["note", "peu", "sans", "ancien"])          # 3.0 sur 900 votes passe devant 9.9 sur trop peu de votes

    def test_a_saga_is_rated_by_its_best_film(self):
        saga = {"id": "s", "sort": "s", "added": 0, "year": "", "grp": "", "meta": "{}",
                "members": [self.film("t1", "a", 8.0, 900), self.film("t2", "b", 8.6, 1200), self.film("t3", "c", 6.1, 900), self.film("sans", "d")]}     # Terminator : le 2
        rows = [saga, self.film("seul", "z", 8.5, 100), self.film("egal", "y", 8.6, 500)]
        self.assertEqual(self.order(rows), ["s", "egal", "seul"])                    # 8,6 sur 1 200 votes (ceux du 2), devant 8,6 sur 500
        self.assertEqual(app.row_rating(saga), (8.6, 1200))
        unrated = {**saga, "id": "vide", "members": [self.film("c", "c")]}
        self.assertEqual(self.order([unrated, self.film("bas", "y", 6.9, 100)]), ["bas", "vide"])


class TestPageBounds(unittest.TestCase):
    def test_the_defaults(self):
        self.assertEqual(app.page_bounds({}), (0, 240))

    def test_the_numbers_asked_are_kept_within_the_limits(self):
        self.assertEqual(app.page_bounds({"offset": "10", "limit": "5"}), (10, 5))
        self.assertEqual(app.page_bounds({"offset": "-4", "limit": "0"}), (0, 1))
        self.assertEqual(app.page_bounds({"limit": "1"}), (0, 1))
        self.assertEqual(app.page_bounds({"limit": "600"}), (0, 600))
        self.assertEqual(app.page_bounds({"limit": "601"}), (0, 600))
        self.assertEqual(app.page_bounds({"offset": "0"}), (0, 240))

    def test_what_is_not_a_number_gives_the_defaults(self):
        self.assertEqual(app.page_bounds({"offset": "x", "limit": "5"}), (0, 240))
        self.assertEqual(app.page_bounds({"offset": "5", "limit": "y"}), (0, 240))


class TestLibraryItem(unittest.TestCase):
    def test_a_saga_is_shown_as_a_saga_and_the_rest_as_a_thumbnail(self):
        saga = {"id": "s", "lib": "lib", "type": "saga", "title": "Saga", "year": "", "years": [], "count": 2, "letter": "S", "added": 0, "genres": "[]", "members": []}
        self.assertEqual(app.library_item(saga, {})["type"], "saga")
        self.assertIn("covers", app.library_item(saga, {}))
        self.assertNotIn("covers", app.library_item(item("m1"), {}))
        self.assertEqual(app.library_item(item("m1"), {"m1": (1, 2)})["seen"], 1)


class TestPathOf(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.lib = types.SimpleNamespace(path=self.root / "Musique")
        self.hub = types.SimpleNamespace(cfg=types.SimpleNamespace(library=lambda ident: self.lib if ident == "musique" else None))

    def path_of(self, rel, lib="musique"):
        return app.Hub.path_of(self.hub, {"lib": lib, "rel": rel})

    def refused(self, rel, status, message):
        with self.assertRaises(HttpError) as caught:
            self.path_of(rel)
        self.assertEqual((caught.exception.status, caught.exception.message), (status, message), rel)

    def test_a_file_of_the_library_is_found_under_its_folder(self):
        lib, path = self.path_of("Daft/01.flac")
        self.assertEqual((lib, path), (self.lib, self.root / "Musique" / "Daft" / "01.flac"))
        self.assertEqual(self.path_of("Daft/../Air/02.flac")[1], self.root / "Musique" / "Air" / "02.flac")
        self.assertEqual(self.path_of(".")[1], self.root / "Musique")

    def test_an_unknown_library_is_a_404(self):
        with self.assertRaises(HttpError) as caught:
            self.path_of("a.flac", lib="ailleurs")
        self.assertEqual((caught.exception.status, caught.exception.message), (404, "bibliothèque inconnue"))

    def test_a_path_that_leaves_the_library_is_refused_even_for_a_folder_with_the_same_beginning(self):
        self.refused("../secret.txt", 400, "chemin refusé")
        self.refused("../Musique2/a.flac", 400, "chemin refusé")
        self.refused("Daft/../../x", 400, "chemin refusé")


class TestLibraryExtras(unittest.TestCase):
    def test_nothing_is_added_when_there_is_nothing_to_say(self):
        out = {}
        app.library_extras(out, "movies", {}, [], False, [])
        self.assertEqual(out, {})

    def test_the_sections_come_for_books_only(self):
        out = {}
        app.library_extras(out, "books", {"Manga": 2}, [], False, [])
        self.assertEqual(out, {"sections": [["Manga", 2]]})
        out = {}
        app.library_extras(out, "movies", {"Manga": 2}, [], False, [])
        self.assertEqual(out, {})

    def test_the_missing_films_are_given_as_they_are(self):
        out = {}
        app.library_extras(out, "movies", {}, [{"tmdb": 1}], False, [])
        self.assertEqual(out, {"missing": [{"tmdb": 1}]})

    def test_a_film_in_two_sagas_counts_once_in_the_total(self):
        a, b, c = ({"id": i, "type": "movie"} for i in "abc")
        chosen = [{"id": "s1", "type": "saga", "members": [a, b]}, {"id": "s2", "type": "saga", "members": [b, c]}, {"id": "d", "type": "movie"}]
        out = {}
        app.library_extras(out, "movies", {}, [], True, chosen)
        self.assertEqual((out["sagas"], out["films"]), (2, 4))


class TestResumeTarget(unittest.TestCase):
    def rows(self):
        return [{"id": n} for n in "abc"]

    def test_the_file_started_most_recently_comes_first(self):
        progress = {"a": {"updated": 5, "done": 0, "position": 10}, "b": {"updated": 9, "done": 0, "position": 10}, "c": {"updated": 99, "done": 1, "position": 10}}
        self.assertEqual(app.resume_target(self.rows(), progress), "b")

    def test_a_file_at_the_very_start_does_not_count_as_started(self):
        progress = {"a": {"updated": 5, "done": 0, "position": 0}, "b": {"updated": 9, "done": 1, "position": 50}}
        self.assertEqual(app.resume_target(self.rows(), progress), "a")
        progress = {"a": {"updated": 99, "done": 0, "position": 0}, "b": {"updated": 1, "done": 0, "position": 0.5}}
        self.assertEqual(app.resume_target(self.rows(), progress), "b")                  # un peu entamé passe avant un pas commencé, même plus récent

    def test_otherwise_the_first_that_is_not_seen(self):
        progress = {"a": {"updated": 1, "done": 1, "position": 5}}
        self.assertEqual(app.resume_target(self.rows(), progress), "b")
        self.assertEqual(app.resume_target(self.rows(), {}), "a")

    def test_when_everything_is_seen_the_first(self):
        progress = {n: {"updated": 1, "done": 1, "position": 5} for n in "abc"}
        self.assertEqual(app.resume_target(self.rows(), progress), "a")

    def test_no_file_gives_nothing(self):
        self.assertIsNone(app.resume_target([], {}))


class TestPlaying(DbCase):
    def test_the_settings_of_the_player(self):
        self.assertEqual(app.play_settings({}), ({}, None, 0, 0))
        self.assertEqual(app.play_settings({"caps": {"channels": 6}, "audio": "2", "max_kbps": "3000", "max_height": 720}), ({"channels": 6}, 2, 3000, 720))
        self.assertEqual(app.play_settings({"caps": None, "audio": 0, "max_kbps": None, "max_height": None}), ({}, 0, 0, 0))

    def test_a_setting_that_is_not_a_number_is_refused(self):
        for body in ({"audio": "x"}, {"max_kbps": "x"}, {"max_height": [1]}, {"audio": {}}):
            with self.assertRaises(HttpError) as caught:
                app.play_settings(body)
            self.assertEqual((caught.exception.status, caught.exception.message), (400, "réglages invalides"), body)

    def test_a_file_that_plays_as_it_is_is_served_as_it_is(self):
        self.assertEqual(app.play_url({"id": "e1"}, {"mode": "direct"}, {}, 0, 0), "/media/e1/file")

    def test_a_converted_file_is_asked_with_its_settings(self):
        decision = {"mode": "transcode", "copy_audio": False, "audio": None}
        self.assertEqual(app.play_url({"id": "e1"}, decision, {}, 3000, 720), "/media/e1/stream?m=transcode&ca=0&k=3000&h=720&c=2")
        decision = {"mode": "remux", "copy_audio": True, "audio": 0}
        self.assertEqual(app.play_url({"id": "e1"}, decision, {"channels": 6}, 0, 0), "/media/e1/stream?m=remux&ca=1&k=0&h=0&c=6&a=0")

    def test_the_channels_asked_never_go_over_six(self):
        decision = {"mode": "remux", "copy_audio": True, "audio": 1}
        self.assertTrue(app.play_url({"id": "e1"}, decision, {"channels": 8}, 0, 0).endswith("&c=6&a=1"))
        self.assertTrue(app.play_url({"id": "e1"}, decision, {"channels": 0}, 0, 0).endswith("&c=2&a=1"))
        self.assertTrue(app.play_url({"id": "e1"}, decision, {"channels": 1}, 0, 0).endswith("&c=1&a=1"))

    def test_a_4k_to_convert_is_asked_in_1080p_unless_a_height_was_chosen(self):
        converted = {"mode": "transcode"}
        self.assertEqual(app.default_height(converted, {"height": 2160}, 0), 1080)
        self.assertEqual(app.default_height(converted, {"height": 2160}, 720), 720)
        self.assertEqual(app.default_height(converted, {"height": 1081}, 0), 1080)
        self.assertEqual(app.default_height(converted, {"height": 1080}, 0), 0)
        self.assertEqual(app.default_height({"mode": "remux"}, {"height": 2160}, 0), 0)
        self.assertEqual(app.default_height(converted, {}, 0), 0)

    def test_the_position_to_resume_from(self):
        self.db.replace_library("lib", [item("m1")], [entry("e1", "m1"), entry("e2", "m1"), entry("e3", "m1"), entry("e4", "m1")])
        self.db.save_progress("p", "e1", app.RESUME_MIN, 100, False)
        self.db.save_progress("p", "e2", app.RESUME_MIN - 0.5, 100, False)
        self.db.save_progress("p", "e3", 50, 100, True)
        get = lambda ident: {"id": ident}
        self.assertEqual(app.resume_position(self.hub, "p", get("e1")), app.RESUME_MIN)
        for ident in ("e2", "e3", "e4"):
            self.assertEqual(app.resume_position(self.hub, "p", get(ident)), 0, ident)
        self.assertEqual(app.resume_position(self.hub, "q", get("e1")), 0)

    def test_the_title_of_the_show_of_an_episode(self):
        self.db.replace_library("lib", [item("s1", "show", title="Dark"), item("m1", title="Film")], [])
        self.assertEqual(app.show_title(self.hub, {"item_id": "s1"}), "Dark")
        self.assertEqual(app.show_title(self.hub, {"item_id": "m1"}), "")
        self.assertEqual(app.show_title(self.hub, {"item_id": "absent"}), "")

    def test_the_next_file_is_summarized_when_it_is_a_video(self):
        following = {"id": "e2", "title": "Suite", "season": 1, "number": 2, "kind": "video", "duration": 5}
        self.assertEqual(app.next_summary(following), {"id": "e2", "title": "Suite", "season": 1, "number": 2})
        self.assertIsNone(app.next_summary({**following, "kind": "audio"}))
        self.assertIsNone(app.next_summary(None))

    def test_a_subtitle_is_labelled_by_its_language_its_title_and_whether_it_is_forced(self):
        self.assertEqual(app.sub_label({"lang": "fre", "title": "", "index": 0}), "Français")
        self.assertEqual(app.sub_label({"lang": "fre", "title": "SDH", "index": 0, "forced": True}), "Français · SDH · Forcé")
        self.assertEqual(app.sub_label({"lang": "", "title": "", "index": 3}), "Piste 4")
        self.assertEqual(app.sub_label({"lang": "", "title": "Commentaires", "index": 0}), "Piste 1 · Commentaires")


class TestSiteFiles(unittest.TestCase):
    def setUp(self):
        self.web = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.web, True)
        (self.web / "index.html").write_text("<html></html>", encoding="utf-8")
        (self.web / "app.js").write_text("1", encoding="utf-8")
        (self.web / "sous").mkdir()
        (self.web / "sous" / "a.css").write_text("a", encoding="utf-8")
        self.secret = self.web.parent / (self.web.name + "-secret")
        self.secret.mkdir()
        self.addCleanup(shutil.rmtree, self.secret, True)
        (self.secret / "x.js").write_text("secret", encoding="utf-8")
        self.handler = types.SimpleNamespace(hub=types.SimpleNamespace(cfg=types.SimpleNamespace(web=self.web)))

    def target(self, path):
        return app.Handler.static_target(self.handler, path)

    def test_the_page_asked_is_the_file_of_the_site(self):
        self.assertEqual(self.target("/app.js"), (self.web / "app.js").resolve())
        self.assertEqual(self.target("/sous/a.css"), (self.web / "sous" / "a.css").resolve())
        self.assertEqual(self.target("/"), (self.web / "index.html").resolve())
        self.assertEqual(self.target(""), (self.web / "index.html").resolve())

    def test_a_route_of_the_application_is_the_home_page(self):
        for path in ("/bibliotheque/films", "/item/abc", "/a.b/c"):
            self.assertEqual(self.target(path), self.web.resolve() / "index.html", path)

    def test_a_file_that_does_not_exist_is_a_404(self):
        for path in ("/absent.js", "/sous/absent.css", "/dossier.v2/absent.png"):
            with self.assertRaises(HttpError) as caught:
                self.target(path)
            self.assertEqual(caught.exception.status, 404, path)

    def test_nothing_outside_the_site_is_served(self):
        for path in ("/../" + self.secret.name + "/x.js", "/sous/../../" + self.secret.name + "/x.js", "/../"):
            with self.assertRaises(HttpError) as caught:
                self.target(path)
            self.assertEqual(caught.exception.status, 404, path)


class TestScrub(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.films, self.data, self.repo = self.root / "Films", self.root / "Films" / "data", self.root / "repo"
        for folder in (self.films, self.data, self.repo):
            folder.mkdir(parents=True, exist_ok=True)

    def scrub(self, text, repo=True, libs=None):
        cfg = types.SimpleNamespace(libraries=[types.SimpleNamespace(path=p) for p in (libs if libs is not None else [self.films])], data=self.data, repo=self.repo if repo else None)
        return app.Hub.scrub(types.SimpleNamespace(cfg=cfg), text)

    def test_the_folders_of_the_server_are_hidden_in_every_spelling(self):
        for spelling in (str(self.films), self.films.as_posix(), str(self.repo), self.repo.as_posix(), str(self.data)):
            self.assertEqual(self.scrub(f"erreur dans {spelling}/x.mkv"), "erreur dans …/x.mkv", spelling)

    def test_the_longest_folder_goes_first(self):
        self.assertEqual(self.scrub(f"{self.data}/hub.db et {self.films}/a"), "…/hub.db et …/a")

    def test_a_message_without_a_path_is_kept_and_a_non_text_is_written_out(self):
        self.assertEqual(self.scrub("fichier illisible"), "fichier illisible")
        self.assertEqual(self.scrub(ValueError("code 3")), "code 3")
        self.assertEqual(self.scrub(f"ici {self.repo}", repo=False), f"ici {self.repo}")

    def test_a_very_short_folder_is_not_a_reason_to_cut_every_message(self):
        self.assertEqual(self.scrub("a/b/c", libs=[Path("/a")]), "a/b/c")
        self.assertEqual(self.scrub("sur /ab/c", libs=[Path("/ab")]), "sur /ab/c")


if __name__ == "__main__":
    unittest.main()
