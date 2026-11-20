"""Les petites fonctions du serveur, appelées une à une : tri, plages d'octets, progression, libellés, types MIME, corps des requêtes."""

from __future__ import annotations

import http.client
import io
import json
import re
import shutil
import socket
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests import fixtures
from hub import config

fixtures.REPO  # noqa: B018  (rend `libraries` importable)
config.use_media_library(fixtures.REPO)

from libraries.books import shelf  # noqa: E402

from hub import enrich, urls  # noqa: E402
from hub.db import Database  # noqa: E402
from hub.server import app  # noqa: E402
from hub.server.app import HttpError  # noqa: E402


class TestOrderKey(unittest.TestCase):
    def key(self, **over):
        return app.order_key({"season": None, "disc": None, "number": None, "rel": "A/Piste.flac", **over})

    def test_a_file_without_season_or_number_goes_first_by_season_and_last_by_number(self):
        self.assertEqual(self.key(), (-1, 0, 1e9, "a/piste.flac", "A/Piste.flac"))

    def test_the_numbers_are_kept_whatever_they_are(self):
        self.assertEqual(self.key(season=0, disc=2, number=0), (0, 2, 0, "a/piste.flac", "A/Piste.flac"))
        self.assertEqual(self.key(season=3, disc=0, number=7), (3, 0, 7, "a/piste.flac", "A/Piste.flac"))

    def test_the_tracks_of_an_album_go_by_disc_then_number_then_name(self):
        rows = [self.key(disc=2, number=1), self.key(disc=1, number=2), self.key(disc=1, number=1), self.key(disc=1, number=None)]
        self.assertEqual([(r[1], r[2]) for r in sorted(rows)], [(1, 1), (1, 2), (1, 1e9), (2, 1)])


class TestRangeBounds(unittest.TestCase):
    def test_without_an_understandable_header_the_whole_file_is_sent(self):
        for header in (None, "", "   ", "bytes=-", "n'importe quoi", "bytes=1-2,5-6", "items=0-5"):
            self.assertEqual(app.range_bounds(header, 100), (0, 99, 200), header)

    def test_a_part_is_sent_with_206(self):
        self.assertEqual(app.range_bounds("bytes=10-19", 100), (10, 19, 206))
        self.assertEqual(app.range_bounds(" bytes=10-19 ", 100), (10, 19, 206))
        self.assertEqual(app.range_bounds("bytes=10-10", 100), (10, 10, 206))
        self.assertEqual(app.range_bounds("bytes=0-", 100), (0, 99, 206))
        self.assertEqual(app.range_bounds("bytes=99-", 100), (99, 99, 206))

    def test_an_open_range_goes_to_the_end_and_a_suffix_counts_from_the_end(self):
        self.assertEqual(app.range_bounds("bytes=10-", 100), (10, 99, 206))
        self.assertEqual(app.range_bounds("bytes=-5", 100), (95, 99, 206))
        self.assertEqual(app.range_bounds("bytes=-100", 100), (0, 99, 206))
        self.assertEqual(app.range_bounds("bytes=-500", 100), (0, 99, 206))

    def test_the_end_is_cut_at_the_end_of_the_file(self):
        self.assertEqual(app.range_bounds("bytes=50-1000", 100), (50, 99, 206))
        self.assertEqual(app.range_bounds("bytes=50-99", 100), (50, 99, 206))

    def test_a_part_outside_the_file_is_refused(self):
        for header, size in (("bytes=100-", 100), ("bytes=100-200", 100), ("bytes=20-10", 100), ("bytes=-0", 100), ("bytes=0-", 0), ("bytes=-5", 0)):
            self.assertIsNone(app.range_bounds(header, size), (header, size))


class TestIsFinished(unittest.TestCase):
    def test_nothing_is_finished_without_a_duration(self):
        for kind in ("video", "book", "audio"):
            self.assertFalse(app.is_finished(kind, 10, 0), kind)
            self.assertFalse(app.is_finished(kind, 10, -5), kind)

    def test_a_very_short_duration_is_still_a_duration(self):
        self.assertTrue(app.is_finished("video", 0.5, 0.5))
        self.assertTrue(app.is_finished("book", 1, 1))
        self.assertFalse(app.is_finished("video", 0.4, 0.5))

    def test_a_book_is_finished_a_second_before_the_last_page(self):
        self.assertTrue(app.is_finished("book", 99, 100))
        self.assertTrue(app.is_finished("book", 100, 100))
        self.assertFalse(app.is_finished("book", 98.9, 100))

    def test_a_film_is_finished_from_ninety_two_percent(self):
        self.assertTrue(app.is_finished("video", 1840, 2000))                 # pile 92 %
        self.assertTrue(app.is_finished("video", 2000, 2000))
        self.assertFalse(app.is_finished("video", 1839, 2000))
        self.assertTrue(app.is_finished("video", 92, 100))
        self.assertFalse(app.is_finished("video", 91, 100))

    def test_being_close_to_the_end_never_counts_for_more_than_the_share(self):
        for duration in (30, 100, 600, 1125, 1126, 3000, 9000):              # 8 % de la durée ou 90 s, le plus petit : toujours dans les 8 %
            for position in range(0, duration + 1, max(1, duration // 97)):
                near_the_end = duration - position <= min(90, duration * 0.08)
                self.assertFalse(near_the_end and not app.is_finished("video", position, duration), (position, duration))


class TestWatchState(unittest.TestCase):
    """« À voir » : rien d'entamé (comme le rayon « Pas encore vu ») ; « En cours » : entamé, pas tout vu ; « Terminé » : tous les fichiers vus. `seen` : {élément: (fichiers vus, fichiers entamés)}, vus compris."""

    def row(self, count, ident="f", **over):
        return {"id": ident, "type": "movie", "count": count, **over}

    def test_an_item_without_progress_is_to_watch(self):
        self.assertEqual(app.watch_state(self.row(3), {}), "todo")
        self.assertEqual(app.watch_state(self.row(3), {"autre": (3, 3)}), "todo")

    def test_an_item_with_something_started_but_not_all_seen_is_in_progress(self):
        self.assertEqual(app.watch_state(self.row(1), {"f": (0, 1)}), "progress")            # un film entamé
        self.assertEqual(app.watch_state(self.row(10), {"f": (3, 3)}), "progress")           # une série : 3 épisodes vus sur 10
        self.assertEqual(app.watch_state(self.row(10), {"f": (3, 5)}), "progress")           # 3 vus, 2 entamés
        self.assertEqual(app.watch_state(self.row(10), {"f": (0, 1)}), "progress")           # un épisode entamé, aucun vu

    def test_an_item_is_done_when_every_file_is_seen(self):
        self.assertEqual(app.watch_state(self.row(3), {"f": (3, 3)}), "done")
        self.assertEqual(app.watch_state(self.row(3), {"f": (4, 4)}), "done")                # plus de fichiers vus que dans la base : fini quand même
        self.assertEqual(app.watch_state(self.row(1), {"f": (1, 1)}), "done")

    def test_an_item_without_count_needs_one_seen_file(self):
        self.assertEqual(app.watch_state(self.row(0), {}), "todo")
        self.assertEqual(app.watch_state(self.row(0), {"f": (1, 1)}), "done")
        self.assertEqual(app.watch_state(self.row(0), {"f": (0, 5)}), "progress")

    def saga(self, *films):
        return {"id": "s", "type": "saga", "count": len(films), "members": [self.row(1, name) for name, _ in films]}, {name: counts for name, counts in films if counts}

    def test_a_saga_counts_its_films_one_by_one(self):
        for films, state in (((("a", (1, 1)), ("b", (1, 1))), "done"),                        # tous vus
                             ((("a", None), ("b", None)), "todo"),                            # aucun entamé
                             ((("a", (1, 1)), ("b", None)), "progress"),                      # un vu, un pas touché
                             ((("a", (0, 1)), ("b", None)), "progress"),                      # un entamé
                             ((("a", (0, 1)), ("b", (1, 1))), "progress")):
            with self.subTest(films=films):
                row, seen = self.saga(*films)
                self.assertEqual(app.watch_state(row, seen), state)

    def test_what_the_saga_card_holds_for_itself_does_not_count(self):
        row, _ = self.saga(("a", None), ("b", None))
        self.assertEqual(app.watch_state(row, {"s": (2, 2)}), "todo")                          # le décompte de la vignette elle-même n'existe pas : seuls les films comptent

    def filtered(self, query, rows, seen):
        tests = app.library_filters(query, "movies", None, {}, {}, seen)
        return [r["id"] for r in rows if all(test(r) for test in tests)]

    def test_the_library_filters_pick_one_state_the_old_unseen_means_to_watch_and_an_unknown_one_is_refused(self):
        rows = [self.row(2, "a"), self.row(2, "b"), self.row(2, "c")]
        seen = {"b": (1, 1), "c": (2, 2)}
        self.assertEqual(self.filtered({"filter": "todo"}, rows, seen), ["a"])
        self.assertEqual(self.filtered({"filter": "progress"}, rows, seen), ["b"])
        self.assertEqual(self.filtered({"filter": "done"}, rows, seen), ["c"])
        self.assertEqual(self.filtered({"filter": "unseen"}, rows, seen), ["a"])                # ancien nom, pour les favoris
        self.assertEqual(self.filtered({}, rows, seen), ["a", "b", "c"])
        self.assertEqual(self.filtered({"filter": ""}, rows, seen), ["a", "b", "c"])            # vide : pas de filtre
        with self.assertRaisesRegex(app.HttpError, "filtre inconnu : autre") as refused:       # une valeur inconnue est refusée, elle ne filtre plus « rien » en silence
            self.filtered({"filter": "autre"}, rows, seen)
        self.assertEqual(refused.exception.status, 400)


class TestShownRating(unittest.TestCase):
    def test_a_rating_shows_from_enough_votes(self):
        rating = {"tmdb": 1, "vote": 8.4, "votes": app.MIN_VOTES, "at": 0}
        self.assertIs(app.shown_rating(rating), rating)

    def test_too_few_votes_no_vote_or_an_older_shape_shows_nothing(self):
        for rating in (None, 8.4, "8.4", {}, {"vote": 8.4, "votes": app.MIN_VOTES - 1}, {"vote": None, "votes": 900}, {"vote": 8.4}):
            with self.subTest(rating=rating):
                self.assertIsNone(app.shown_rating(rating))

    def test_a_vote_that_is_not_a_finite_non_zero_number_shows_nothing(self):
        for vote in (True, False, "8.4", [8.4], float("nan"), float("inf"), float("-inf"), 0, 0.0):
            with self.subTest(vote=vote):
                self.assertIsNone(app.shown_rating({"vote": vote, "votes": 900}))
        for vote in (8, 0.1):
            with self.subTest(vote=vote):
                self.assertIsNotNone(app.shown_rating({"vote": vote, "votes": 900}))

    def test_a_card_carries_the_rating_and_its_votes_only_when_shown(self):
        def card(rating):
            return app.summary({"id": "f", "type": "movie", "title": "F", "year": "", "grp": "", "count": 1, "letter": "F", "added": 0, "genres": "[]",
                                "meta": json.dumps({"tmdb_rating": rating})})
        self.assertEqual({k: v for k, v in card({"tmdb": 1, "vote": 8.4, "votes": 900}).items() if k.startswith("tmdb")}, {"tmdb_rating": 8.4, "tmdb_votes": 900})
        self.assertNotIn("tmdb_rating", card(8.4))


class TestSeriesCard(unittest.TestCase):
    def card(self, meta, kind="series"):
        return app.summary({"id": "s", "type": kind, "title": "S", "year": "", "grp": "", "count": 3, "letter": "S", "added": 0, "genres": "[]", "meta": json.dumps(meta)})

    def test_a_series_with_a_stack_shows_its_own_cover_then_the_covers_of_its_next_volumes(self):
        out = self.card({"stack": ["e2", "e3"]})
        self.assertEqual(out["cover_urls"], [urls.item_cover("s"), urls.entry_image("e2"), urls.entry_image("e3")])

    def test_no_more_than_the_maximum_is_shown(self):
        out = self.card({"stack": ["e2", "e3", "e4", "e5"]})
        self.assertEqual(len(out["cover_urls"]), app.STACK_MAX)

    def test_the_page_stacks_as_many_covers_as_the_server_sends(self):
        script = (Path(__file__).resolve().parents[1] / "hub" / "web" / "js" / "views.js").read_text(encoding="utf-8")
        found = re.search(r"var STACK_MAX = (\d+);", script)
        self.assertIsNotNone(found, "views.js doit déclarer STACK_MAX")
        self.assertEqual(int(found.group(1)), app.STACK_MAX)

    def test_a_series_of_one_volume_or_scanned_before_the_stack_existed_has_no_field(self):
        for meta in ({}, {"stack": []}):
            with self.subTest(meta=meta):
                self.assertNotIn("cover_urls", self.card(meta))

    def test_only_a_book_series_stacks(self):
        self.assertNotIn("cover_urls", self.card({"stack": ["e2"]}, kind="show"))


class TestSagaJson(unittest.TestCase):
    def saga(self, n=5):
        members = [{"id": f"f{i}", "count": 1} for i in range(n)]
        return {"id": "s", "lib": "films", "title": "Saga", "year": "2001", "years": ["2001", "2005"], "count": n, "letter": "S", "added": 7, "genres": '["Action"]', "members": members}

    def test_the_covers_are_those_of_the_first_three_films(self):
        out = app.saga_json(self.saga(), {})
        self.assertEqual(out["covers"], ["f0", "f1", "f2"])
        self.assertEqual(out["cover_urls"], [urls.item_cover(i) for i in ("f0", "f1", "f2")])
        self.assertEqual((out["type"], out["n"], out["genres"], out["years"], out["grp"]), ("saga", 5, ["Action"], ["2001", "2005"], ""))

    def test_nothing_is_said_of_a_saga_nobody_started(self):
        out = app.saga_json(self.saga(), {})
        self.assertNotIn("seen", out)
        self.assertNotIn("started", out)

    def test_the_films_seen_and_the_films_started_are_counted_apart(self):
        seen = {"f0": (1, 0), "f1": (1, 40), "f2": (0, 40), "f3": (0, 0), "f4": (0, 30)}                 # (fichiers vus, position)
        out = app.saga_json(self.saga(), seen)
        self.assertEqual((out["seen"], out["started"]), (2, 2))                  # un film vu n'est pas « commencé » même s'il garde une position
        self.assertNotIn("started", app.saga_json(self.saga(), {"f0": (1, 40), "f1": (0, 0)}))


class TestReleaseKey(unittest.TestCase):
    def key(self, meta=None, year="", sort="titre"):
        return app.release_key({"meta": json.dumps(meta or {}), "year": year, "sort": sort, "id": "x"})

    def test_the_date_of_the_sheet_wins_then_the_year_then_the_end_of_time(self):
        self.assertEqual(self.key({"date": "2001-05-04"}, "1999"), ("2001-05-04", True, 0, "titre", "x"))
        self.assertEqual(self.key({}, "1999"), ("1999", True, 0, "titre", "x"))
        self.assertEqual(self.key(), ("9999", True, 0, "titre", "x"))

    def test_the_number_only_separates_films_of_the_same_day(self):
        self.assertEqual(self.key({"part": 3}), ("9999", False, 3, "titre", "x"))
        self.assertEqual(self.key({"part": 0}), ("9999", False, 0, "titre", "x"))
        rows = [self.key({"date": "2001", "part": 2}, sort="b"), self.key({"date": "2001"}, sort="a"), self.key({"date": "2001", "part": 1}, sort="c")]
        self.assertEqual([r[3] for r in sorted(rows)], ["c", "b", "a"])


class TestTiesAreBrokenByIdentifier(unittest.TestCase):
    """Deux éléments que le tri ne distingue pas (même titre, même année, même date d'ajout) gardent le même ordre, quel que soit celui dans lequel la base les rend."""

    def rows(self):
        base = {"sort": "alien", "title": "Alien", "year": "1979", "added": 100, "grp": "Scott", "meta": "{}"}
        return [dict(base, id=ident) for ident in ("c", "a", "b")]

    def assertStable(self, key, reverse=False):
        rows = self.rows()
        orders = {tuple(r["id"] for r in sorted(order, key=key, reverse=reverse)) for order in (rows, rows[::-1], rows[1:] + rows[:1])}
        self.assertEqual(len(orders), 1, orders)

    def test_every_library_sort_breaks_ties_by_identifier(self):
        for name, (key, reverse) in app.SORTS.items():
            with self.subTest(sort=name):
                self.assertStable(key, reverse)
        for kind, key in app.TITLE_SORTS.items():
            with self.subTest(kind=kind):
                self.assertStable(key)

    def test_the_order_of_a_saga_breaks_ties_by_identifier(self):
        self.assertStable(app.release_key)


class TestSectionsJson(unittest.TestCase):
    def test_the_shelf_order_then_the_others_by_name_then_the_loose_ones(self):
        order = list(shelf.SECTION_ORDER)
        counts = {shelf.LOOSE_SECTION: 1, "Zoologie": 2, order[2]: 3, "alpha": 4, order[0]: 5, "Éclair": 6}
        self.assertEqual(app.sections_json(counts), [[order[0], 5], [order[2], 3], ["alpha", 4], ["Éclair", 6], ["Zoologie", 2], [shelf.LOOSE_SECTION, 1]])

    def test_no_section_gives_an_empty_list(self):
        self.assertEqual(app.sections_json({}), [])


class TestPlayWarning(unittest.TestCase):
    def warning(self, mode="transcode", height=2160, codec="hevc"):
        return app.play_warning({"mode": mode}, {"height": height, "codec": codec})

    def test_a_4k_that_must_be_converted_is_flagged(self):
        for codec in ("hevc", "av1"):
            self.assertIn("4K", self.warning(codec=codec))
        self.assertIn("4K", self.warning(height=2000))

    def test_anything_else_is_not(self):
        self.assertEqual(self.warning(height=1999), "")
        self.assertEqual(self.warning(mode="direct"), "")
        self.assertEqual(self.warning(mode="remux"), "")
        self.assertEqual(self.warning(codec="h264"), "")
        self.assertEqual(app.play_warning({"mode": "transcode"}, {}), "")
        self.assertEqual(app.play_warning({"mode": "transcode"}, {"height": 2160}), "")
        self.assertEqual(app.play_warning({"mode": "transcode"}, {"codec": "hevc"}), "")                 # hauteur inconnue : pas du 4K


class TestTrackLabel(unittest.TestCase):
    def label(self, **over):
        return app.track_label({"lang": "fre", "title": "", "index": 0, **over})

    def test_the_language_the_title_and_the_codec_with_its_channels(self):
        self.assertEqual(self.label(codec="ac3", channels=6), "Français (AC-3 5.1)")
        self.assertEqual(self.label(title="Commentaire", codec="eac3", channels=2), "Français · Commentaire (E-AC-3 stéréo)")
        self.assertEqual(self.label(lang="eng", codec="truehd", channels=8), "English (TrueHD 7.1)")

    def test_every_codec_has_its_name(self):
        names = {"dts": "DTS", "aac": "AAC", "flac": "FLAC", "opus": "Opus"}
        for codec, name in names.items():
            self.assertEqual(self.label(codec=codec), f"Français ({name})")

    def test_an_unknown_codec_is_written_in_capitals_and_an_unknown_layout_is_left_out(self):
        self.assertEqual(self.label(codec="vorbis", channels=3), "Français (VORBIS)")
        self.assertEqual(self.label(channels=1), "Français (mono)")
        self.assertEqual(self.label(), "Français")

    def test_a_track_without_language_is_numbered_from_one_unless_it_has_a_title(self):
        self.assertEqual(self.label(lang="", index=2), "Piste 3")
        self.assertEqual(self.label(lang="", index=0, title="Version longue"), "Version longue")
        self.assertEqual(self.label(lang="", index=1, codec="dts", channels=6), "Piste 2 (DTS 5.1)")


class TestEpubMime(unittest.TestCase):
    GOOD, BAD = b'<?xml version="1.0"?><html/>', b"<html><p>pas ferme</html>"

    def test_a_well_formed_page_is_served_as_xhtml(self):
        for mime in ("application/xhtml+xml", "text/html"):
            self.assertEqual(app.epub_mime(self.GOOD, mime), "application/xhtml+xml; charset=utf-8", mime)

    def test_a_badly_formed_page_is_served_as_html(self):
        for mime in ("application/xhtml+xml", "text/html"):
            self.assertEqual(app.epub_mime(self.BAD, mime), "text/html; charset=utf-8", mime)

    def test_an_svg_stays_an_svg(self):
        self.assertEqual(app.epub_mime(self.GOOD, "image/svg+xml"), "image/svg+xml; charset=utf-8")
        self.assertEqual(app.epub_mime(self.BAD, "image/svg+xml"), "image/svg+xml; charset=utf-8")

    def test_a_file_that_begins_like_xml_is_treated_as_a_page_whatever_its_name_says(self):
        self.assertEqual(app.epub_mime(self.GOOD, "application/octet-stream"), "application/xhtml+xml; charset=utf-8")
        self.assertEqual(app.epub_mime(b"<?xml mais pas du xml", "application/octet-stream"), "text/html; charset=utf-8")
        self.assertEqual(app.epub_mime(b"<?xm", "application/octet-stream"), "application/octet-stream")
        self.assertEqual(app.epub_mime(self.GOOD, "image/svg+xml"), "image/svg+xml; charset=utf-8")

    def test_the_encoding_is_told_for_text_only(self):
        for mime in ("text/css", "text/plain", "application/x-dtbncx+xml", "application/smil+xml"):
            self.assertEqual(app.epub_mime(b"corps", mime), mime + "; charset=utf-8", mime)
        for mime in ("image/png", "application/javascript", "font/otf", "application/octet-stream"):
            self.assertEqual(app.epub_mime(b"corps", mime), mime, mime)


class FakeRequest(app.Handler):
    """Un gestionnaire sans connexion : les en-têtes et le corps donnés, rien d'autre."""

    def __init__(self, body=b"", length=None):
        self.headers = {} if length is None and not body else {"Content-Length": str(len(body) if length is None else length)}
        self.rfile = io.BytesIO(body)
        self.close_connection = False
        self.body_taken = False


class TestContentLength(unittest.TestCase):
    def length(self, value):
        request = FakeRequest()
        request.headers = {} if value is None else {"Content-Length": value}
        return request, request.content_length()

    def test_the_announced_size_is_read(self):
        self.assertEqual(self.length("12")[1], 12)
        self.assertEqual(self.length("0")[1], 0)
        self.assertEqual(self.length("")[1], 0)
        self.assertEqual(self.length(None)[1], 0)

    def test_an_unreadable_or_negative_size_is_refused_and_the_connection_cut(self):
        for value in ("abc", "-1", "-50", "1.5"):
            request = FakeRequest()
            request.headers = {"Content-Length": value}
            with self.assertRaises(HttpError) as caught:
                request.content_length()
            self.assertEqual(caught.exception.status, 400, value)
            self.assertTrue(request.close_connection, value)

    def test_a_good_size_leaves_the_connection_open(self):
        self.assertFalse(self.length("0")[0].close_connection)


class TestLoginCredentials(unittest.TestCase):
    def read(self, raw):
        request = FakeRequest(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
        return request, app.login_credentials(request)

    def test_a_form_gives_the_name_and_the_password(self):
        self.assertEqual(self.read("user=alice&password=s%C3%A9cret")[1], ("alice", "sécret"))
        self.assertEqual(self.read("user=alice")[1], ("alice", ""))
        self.assertEqual(self.read("autre=1")[1], ("", ""))

    def test_a_json_body_does_too_even_when_the_password_has_an_equals_sign(self):
        self.assertEqual(self.read('{"user": "alice", "password": "a=b"}')[1], ("alice", "a=b"))
        self.assertEqual(self.read('   {"user": "alice", "password": "a=b"}')[1], ("alice", "a=b"))

    def test_a_json_body_without_what_is_needed_gives_nothing(self):
        self.assertEqual(self.read('{"password": "x"}')[1], (None, "x"))
        self.assertEqual(self.read("[1, 2]")[1], (None, ""))
        self.assertEqual(self.read("n'importe quoi")[1], (None, ""))
        self.assertEqual(self.read("")[1], (None, ""))

    def test_at_most_four_kilobytes_are_read_and_a_longer_body_cuts_the_connection(self):
        request, found = self.read("user=alice&password=" + "x" * 5000)
        self.assertEqual((len(found[1]), request.rfile.tell(), request.close_connection, request.body_taken), (4096 - len("user=alice&password="), 4096, True, True))
        request, found = self.read("user=alice&password=" + "x" * (4096 - len("user=alice&password=")))
        self.assertEqual((len(found[1]), request.close_connection), (4096 - len("user=alice&password="), False))


def inbox_request(length="10", consumed=0, command="POST", path="/api/inbox", **more):
    """Une requête sans connexion, comme `dispatch` la voit : `length` est le `Content-Length` annoncé (None : aucun), `consumed` ce que la route en a déjà lu."""
    request = FakeRequest()
    request.command, request.path, request.body_consumed = command, path, consumed
    request.headers = http.client.HTTPMessage()
    for key, value in ({} if length is None else {"Content-Length": length}).items() | more.items():
        request.headers[key] = value
    return request


class TestInboxUnread(unittest.TestCase):
    def test_what_the_client_may_still_send_is_the_announced_size_less_what_was_read(self):
        self.assertEqual(inbox_request("10").inbox_unread(), 10)
        self.assertEqual(inbox_request("10", consumed=3).inbox_unread(), 7)
        self.assertEqual(inbox_request("10", path="/api/inbox?name=a.mkv").inbox_unread(), 10)
        self.assertEqual(inbox_request(" 10 ").inbox_unread(), 10)

    def test_never_less_than_nothing(self):
        self.assertEqual(inbox_request("10", consumed=10).inbox_unread(), 0)
        self.assertEqual(inbox_request("10", consumed=25).inbox_unread(), 0)

    def test_only_an_upload_to_the_inbox_has_a_body_to_wait_for(self):
        for command, path in (("GET", "/api/inbox"), ("POST", "/api/progress"), ("DELETE", "/api/inbox"), ("GET", "/api/other"), ("POST", "/api/inbox/alice/a.mkv")):
            self.assertEqual(inbox_request("10", command=command, path=path).inbox_unread(), 0, (command, path))

    def test_a_size_that_is_not_announced_properly_leaves_nothing_to_wait_for(self):
        self.assertEqual(inbox_request(None).inbox_unread(), 0)
        for length in ("abc", "-5", "1_0", "+10", "", "1.5"):
            self.assertEqual(inbox_request(length).inbox_unread(), 0, length)
        self.assertEqual(inbox_request("10", **{"Transfer-Encoding": "chunked"}).inbox_unread(), 0)
        twice = inbox_request("10")
        twice.headers["Content-Length"] = "10"
        self.assertEqual(twice.inbox_unread(), 0)


class TestLetTheReplyArrive(unittest.TestCase):
    def request(self, unread, chunks=()):
        request = inbox_request(str(unread))
        request.connection, request.wfile = mock.Mock(), mock.Mock()
        request.rfile = mock.Mock(read1=mock.Mock(side_effect=list(chunks) + [b""]))
        return request

    def test_with_nothing_left_to_read_the_connection_is_left_alone(self):
        request = self.request(0)
        request.let_the_reply_arrive()
        request.connection.shutdown.assert_not_called()
        request.rfile.read1.assert_not_called()

    def test_what_is_left_is_read_and_thrown_away_once_the_writing_side_is_closed(self):
        request = self.request(10, [b"abcd", b"efghij"])
        request.let_the_reply_arrive()
        request.wfile.flush.assert_called_once_with()
        request.connection.shutdown.assert_called_once_with(socket.SHUT_WR)
        self.assertEqual([call.args for call in request.rfile.read1.call_args_list], [(10,), (6,)])

    def test_the_body_is_read_up_to_the_announced_size_and_no_further(self):
        request = self.request(4, [b"abcd", b"zz"])
        request.let_the_reply_arrive()
        self.assertEqual(request.rfile.read1.call_count, 1)

    def test_the_last_byte_is_read_too(self):
        one = self.request(1, [b"a"])
        one.let_the_reply_arrive()
        one.connection.shutdown.assert_called_once_with(socket.SHUT_WR)
        self.assertEqual(one.rfile.read1.call_count, 1)
        five = self.request(5, [b"abcd", b"e"])
        five.let_the_reply_arrive()
        self.assertEqual([call.args for call in five.rfile.read1.call_args_list], [(5,), (1,)])

    def test_a_last_second_is_enough_to_read(self):
        request = self.request(10, [b"abcd", b"efghij"])
        with mock.patch.object(app.time, "monotonic", side_effect=[100.0, 100.0 + app.INBOX_DRAIN_SECONDS - 1, 100.0 + app.INBOX_DRAIN_SECONDS - 1]):
            request.let_the_reply_arrive()
        self.assertEqual(request.rfile.read1.call_count, 2)
        request.connection.settimeout.assert_called_with(1.0)

    def test_a_client_that_goes_on_sending_is_cut_at_the_byte_limit(self):
        request = self.request(app.INBOX_DRAIN_BYTES * 2, [b"x" * app.INBOX_DRAIN_BYTES])
        request.let_the_reply_arrive()
        self.assertEqual(request.rfile.read1.call_count, 1)

    def test_nothing_more_is_read_once_the_time_is_up(self):
        request = self.request(10, [b"abcd"])
        with mock.patch.object(app.time, "monotonic", side_effect=[100.0, 100.0 + app.INBOX_DRAIN_SECONDS]):
            request.let_the_reply_arrive()
        request.connection.shutdown.assert_called_once_with(socket.SHUT_WR)
        request.rfile.read1.assert_not_called()

    def test_a_client_that_left_is_not_an_error(self):
        request = self.request(10)
        request.connection.shutdown.side_effect = OSError("parti")
        request.let_the_reply_arrive()


class TestFailure(unittest.TestCase):
    """`_fail` : la réponse d'erreur, puis ce qui reste du corps de la requête."""

    def failing(self, command="POST", taken=False, close=False, send=None):
        request = inbox_request("10", command=command)
        request.hub = types.SimpleNamespace(scrub=lambda message: message)
        request.body_taken, request.close_connection = taken, close
        request.send_json, request.drop_body, request.let_the_reply_arrive = mock.Mock(side_effect=send), mock.Mock(), mock.Mock()
        request._fail(403, "refusé")
        return request

    def test_the_error_is_sent_as_json_with_its_status(self):
        self.failing().send_json.assert_called_once_with({"error": "refusé"}, 403)

    def test_a_body_that_was_not_read_is_thrown_away_whatever_the_answer(self):
        self.failing().drop_body.assert_called_once_with()
        self.failing("DELETE").drop_body.assert_called_once_with()
        self.failing("GET").drop_body.assert_not_called()
        self.failing(taken=True).drop_body.assert_not_called()

    def test_the_client_gets_time_to_read_the_reply_only_when_the_connection_is_cut(self):
        self.failing(close=True).let_the_reply_arrive.assert_called_once_with()
        self.failing(close=False).let_the_reply_arrive.assert_not_called()

    def test_an_answer_that_cannot_be_sent_cuts_the_connection_without_waiting_for_anyone(self):
        request = self.failing(send=OSError("parti"))
        self.assertTrue(request.close_connection)
        request.let_the_reply_arrive.assert_not_called()


class TestDispatchBookkeeping(unittest.TestCase):
    def test_each_request_starts_with_nothing_read_of_its_body(self):
        request = inbox_request("10", consumed=99)
        request.authorized, request._fail = mock.Mock(return_value=False), mock.Mock()
        request.dispatch()
        self.assertEqual((request.body_consumed, request.body_taken), (0, False))
        self.assertEqual(request.inbox_unread(), 10)


class TestStrictBody(unittest.TestCase):
    def test_a_refused_body_was_read_all_the_same(self):
        for body in (b"[1]", b"{", b""):
            request = FakeRequest(body)
            with self.assertRaises(HttpError) as caught:
                app.strict_body_dict(request)
            self.assertEqual(caught.exception.status, 400, body)
            self.assertTrue(request.body_taken, body)


class TestGrantsOfTheAccount(unittest.TestCase):
    def request(self, role):
        request = FakeRequest()
        request.hub, request.user, request.granted = types.SimpleNamespace(db="base"), {"id": "u1", "role": role}, None
        return request

    def test_the_rights_of_an_account_are_read_once_per_request(self):
        request = self.request("user")
        with mock.patch.object(app.accounts, "grants", return_value={"lib": (True, False, False)}) as grants:
            self.assertTrue(request.granted_to_account("lib", "view"))
            self.assertFalse(request.granted_to_account("lib", "play"))
        grants.assert_called_once_with("base", "u1")

    def test_an_administrator_has_no_rights_to_read(self):
        request = self.request("admin")
        with mock.patch.object(app.accounts, "grants") as grants:
            self.assertTrue(request.granted_to_account("lib", "view"))
        grants.assert_not_called()


class TestArtistLinks(unittest.TestCase):
    def refused(self, call, *args):
        with self.assertRaises(HttpError) as caught:
            call(*args)
        self.assertEqual(caught.exception.status, 400, args)
        return caught.exception.message

    def test_a_web_address_is_kept_trimmed(self):
        self.assertEqual(app.artist_link_url("  https://exemple.fr/a?b=1#c  "), "https://exemple.fr/a?b=1#c")
        self.assertEqual(app.artist_link_url("http://exemple.fr/a!b"), "http://exemple.fr/a!b")

    def test_an_address_may_have_up_to_the_limit_and_not_one_more(self):
        base = "https://exemple.fr/"
        self.assertEqual(app.artist_link_url(base + "x" * (app.LINK_URL_MAX - len(base))), base + "x" * (app.LINK_URL_MAX - len(base)))
        self.assertIn("trop longue", self.refused(app.artist_link_url, base + "x" * (app.LINK_URL_MAX - len(base) + 1)))

    def test_what_is_not_a_web_address_is_refused(self):
        for value in (None, 5, "", "   ", "javascript:alert(1)", "data:text/html,x", "ftp://exemple.fr/", "https://", "exemple.fr", "https://exemple.fr/a b", "https://exemple.fr/a\x00b", "https://exemple.fr/a\x01b", "https://exemple.fr/a\x1bb", "https://exemple.fr/a\nb", "http://[::1"):
            with self.subTest(value=value):
                self.refused(app.artist_link_url, value)

    def test_a_label_is_tidied_or_made_from_the_site_and_has_a_limit(self):
        self.assertEqual(app.artist_link_label("  Mon   site  ", "https://exemple.fr/"), "Mon site")
        self.assertEqual(app.artist_link_label(None, "https://www.exemple.fr/a"), app.artistinfo.host_label("https://www.exemple.fr/a"))
        self.assertEqual(app.artist_link_label("   ", "https://www.exemple.fr/a"), app.artistinfo.host_label("https://www.exemple.fr/a"))
        self.assertEqual(app.artist_link_label("x" * app.LINK_LABEL_MAX, "https://exemple.fr/"), "x" * app.LINK_LABEL_MAX)
        self.assertIn("trop long", self.refused(app.artist_link_label, "x" * (app.LINK_LABEL_MAX + 1), "https://exemple.fr/"))
        self.refused(app.artist_link_label, 5, "https://exemple.fr/")

    def test_what_is_kept_for_an_artist_is_read_with_care(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        db = Database(root / "hub.db")
        hub = types.SimpleNamespace(db=db)
        self.assertEqual(app.artist_link_edits(hub, "mb1"), {"removed": [], "added": []})
        db.kv_set(enrich.links_key("mb1"), {"removed": ["https://a.fr/", 5, None], "added": [{"label": "A", "url": "https://b.fr/"}, {"url": "https://c.fr/"}, {"label": "D", "url": 5}, {"label": "E"}, "https://f.fr/", 7, None]})
        self.assertEqual(app.artist_link_edits(hub, "mb1"), {"removed": ["https://a.fr/"], "added": [{"label": "A", "url": "https://b.fr/"}, {"label": "", "url": "https://c.fr/"}]})
        db.kv_set(enrich.links_key("mb1"), ["abîmé"])
        self.assertEqual(app.artist_link_edits(hub, "mb1"), {"removed": [], "added": []})


class TestArtistSheet(unittest.TestCase):
    def test_the_country_is_in_french_for_a_french_page_and_as_musicbrainz_says_it_otherwise(self):
        info = {"type": "Group", "area": "Sweden", "begin": "1998", "end": "2016"}
        self.assertEqual(app.artist_sheet(info)["facts"], "Groupe · Suède · 1998 – 2016")
        self.assertEqual(app.artist_sheet(info, "", "fr")["facts"], "Groupe · Suède · 1998 – 2016")
        self.assertEqual(app.artist_sheet(info, "", "en")["facts"], "Groupe · Sweden · 1998 – 2016")


class TestSideLabel(unittest.TestCase):
    def test_the_words_of_the_hub_follow_the_language_of_the_page_and_nothing_else_does(self):
        self.assertEqual(app.side_label("Français · Forcé · SDH"), "Français · Forcé · SDH")
        self.assertEqual(app.side_label("Français · Forcé · SDH", "fr"), "Français · Forcé · SDH")
        self.assertEqual(app.side_label("Français · Forcé · SDH", "en"), "Français · Forced · SDH")
        self.assertEqual(app.side_label("Sous-titres", "en"), "Subtitles")


class TestFirstChunk(unittest.TestCase):
    def process(self, data):
        return types.SimpleNamespace(stdout=types.SimpleNamespace(read1=lambda n: data), wait=mock.Mock())

    def test_the_first_bytes_are_returned(self):
        self.assertEqual(app.first_chunk(self.process(b"abc"), []), b"abc")

    def test_no_byte_is_a_500_that_gives_the_last_line_ffmpeg_wrote(self):
        for errors, said in ((["premiere\nderniere\n"], "derniere"), (["  une seule  \n"], "une seule"), ([], "erreur inconnue"), (["  \n"], "erreur inconnue"), ([""], "erreur inconnue")):
            proc = self.process(b"")
            with mock.patch.object(app.time, "sleep") as sleep, self.assertRaises(HttpError) as caught:
                app.first_chunk(proc, errors)
            self.assertEqual((caught.exception.status, caught.exception.message), (500, "ffmpeg n'a rien produit : " + said), errors)
            proc.wait.assert_called_once_with(timeout=5)
            sleep.assert_called_once_with(0.05)


class TestArtistTracks(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.db = Database(root / "hub.db")
        self.hub = types.SimpleNamespace(db=self.db)

    def album(self, ident, title, genres):
        return {"id": ident, "lib": "musique", "type": "album", "title": title, "sort": title.casefold(), "letter": title[:1], "year": "", "summary": "", "genres": json.dumps(genres),
                "grp": "Daft", "meta": "{}", "added": 0, "count": 2}

    def track(self, ident, item_id, disc, number, artist=None):
        meta = {"info": {"artist": artist}} if artist else {}
        return {"id": ident, "item_id": item_id, "lib": "musique", "rel": f"{item_id}/{ident}.flac", "kind": "audio", "season": None, "disc": disc, "number": number, "title": f"Piste {ident}",
                "summary": "", "duration": 60.0, "size": 1, "mtime": 1, "meta": json.dumps(meta), "probe": None}

    def test_the_tracks_of_each_album_in_order_with_their_artist_and_the_genres_counted_by_album(self):
        albums = [self.album("a1", "Disco", ["Électro", "Rock"]), self.album("a2", "Homework", ["Électro"])]
        self.db.replace_library("musique", albums, [self.track("t2", "a1", 1, 2), self.track("t1", "a1", 1, 1, artist="Invité"), self.track("u1", "a2", 1, 1)])
        tracks, genres = app.artist_tracks(self.hub, [dict(r) for r in self.db.query("SELECT * FROM items ORDER BY sort")], "Daft Punk")
        self.assertEqual([(t["id"], t["album"], t["item"], t["artist"], t["duration"]) for t in tracks],
                         [("t1", "Disco", "a1", "Invité", 60.0), ("t2", "Disco", "a1", "Daft Punk", 60.0), ("u1", "Homework", "a2", "Daft Punk", 60.0)])
        self.assertEqual(tracks[0]["cover"], urls.item_cover("a1"))
        self.assertEqual(tracks[0]["file"], urls.entry_file("t1"))
        self.assertEqual(genres, {"Électro": 2, "Rock": 1})


if __name__ == "__main__":
    unittest.main()


class TestHandlerTransfer(unittest.TestCase):
    """`Handler.transfer` : un téléchargement ou une lecture sans erreur laisse sa ligne d'activité, même sans octet (ffmpeg et la sauvegarde le refusent eux-mêmes)."""

    def test_a_transfer_leaves_its_line_even_empty_unless_told_otherwise(self):
        seen = []
        activity = types.SimpleNamespace(transfer=lambda *args, **kwargs: seen.append(args[8]))
        h = types.SimpleNamespace(hub=types.SimpleNamespace(activity=activity), user={"id": "u1", "name": "alice"},
                                  client_address=("192.168.1.5", 4000), headers=http.client.HTTPMessage())
        app.Handler.transfer(h, "download", "ref", "Titre")
        app.Handler.transfer(h, "backup", "backup", "Archive", leave_empty=False)
        self.assertEqual(seen, [True, False])
