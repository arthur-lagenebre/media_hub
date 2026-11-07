"""Les petites fonctions du serveur, appelées une à une : tri, plages d'octets, progression, libellés, types MIME, corps des requêtes."""

from __future__ import annotations

import io
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

from libraries.books import shelf  # noqa: E402

from hub import urls  # noqa: E402
from hub.db import Database  # noqa: E402
from hub.server import app  # noqa: E402
from hub.server.app import HttpError  # noqa: E402


class TestOrderKey(unittest.TestCase):
    def key(self, **over):
        return app.order_key({"season": None, "disc": None, "number": None, "rel": "A/Piste.flac", **over})

    def test_a_file_without_season_or_number_goes_first_by_season_and_last_by_number(self):
        self.assertEqual(self.key(), (-1, 0, 1e9, "a/piste.flac"))

    def test_the_numbers_are_kept_whatever_they_are(self):
        self.assertEqual(self.key(season=0, disc=2, number=0), (0, 2, 0, "a/piste.flac"))
        self.assertEqual(self.key(season=3, disc=0, number=7), (3, 0, 7, "a/piste.flac"))

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


class TestIsUnseen(unittest.TestCase):
    def row(self, count, **over):
        return {"id": "f", "type": "movie", "count": count, **over}

    def test_a_film_is_unseen_until_all_its_files_are_seen(self):
        self.assertTrue(app.is_unseen(self.row(3), {"f": (2, 0)}))
        self.assertFalse(app.is_unseen(self.row(3), {"f": (3, 0)}))
        self.assertFalse(app.is_unseen(self.row(3), {"f": (4, 0)}))
        self.assertTrue(app.is_unseen(self.row(3), {}))

    def test_an_item_without_count_needs_one_seen_file(self):
        self.assertTrue(app.is_unseen(self.row(0), {}))
        self.assertFalse(app.is_unseen(self.row(0), {"f": (1, 0)}))
        self.assertTrue(app.is_unseen(self.row(0), {"f": (0, 5)}))

    def test_a_saga_is_unseen_until_all_its_films_are(self):
        self.assertTrue(app.is_unseen({"id": "s", "type": "saga", "count": 2, "all_seen": False}, {"s": (9, 0)}))
        self.assertFalse(app.is_unseen({"id": "s", "type": "saga", "count": 2, "all_seen": True}, {}))


class TestShownRating(unittest.TestCase):
    def test_a_rating_shows_from_enough_votes(self):
        rating = {"tmdb": 1, "vote": 8.4, "votes": app.MIN_VOTES, "at": 0}
        self.assertIs(app.shown_rating(rating), rating)

    def test_too_few_votes_no_vote_or_an_older_shape_shows_nothing(self):
        for rating in (None, 8.4, "8.4", {}, {"vote": 8.4, "votes": app.MIN_VOTES - 1}, {"vote": None, "votes": 900}, {"vote": 8.4}):
            with self.subTest(rating=rating):
                self.assertIsNone(app.shown_rating(rating))

    def test_a_card_carries_the_rating_and_its_votes_only_when_shown(self):
        def card(rating):
            return app.summary({"id": "f", "type": "movie", "title": "F", "year": "", "grp": "", "count": 1, "letter": "F", "added": 0, "genres": "[]",
                                "meta": json.dumps({"tmdb_rating": rating})})
        self.assertEqual({k: v for k, v in card({"tmdb": 1, "vote": 8.4, "votes": 900}).items() if k.startswith("tmdb")}, {"tmdb_rating": 8.4, "tmdb_votes": 900})
        self.assertNotIn("tmdb_rating", card(8.4))


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
        return app.release_key({"meta": json.dumps(meta or {}), "year": year, "sort": sort})

    def test_the_date_of_the_sheet_wins_then_the_year_then_the_end_of_time(self):
        self.assertEqual(self.key({"date": "2001-05-04"}, "1999"), ("2001-05-04", True, 0, "titre"))
        self.assertEqual(self.key({}, "1999"), ("1999", True, 0, "titre"))
        self.assertEqual(self.key(), ("9999", True, 0, "titre"))

    def test_the_number_only_separates_films_of_the_same_day(self):
        self.assertEqual(self.key({"part": 3}), ("9999", False, 3, "titre"))
        self.assertEqual(self.key({"part": 0}), ("9999", False, 0, "titre"))
        rows = [self.key({"date": "2001", "part": 2}, sort="b"), self.key({"date": "2001"}, sort="a"), self.key({"date": "2001", "part": 1}, sort="c")]
        self.assertEqual([r[3] for r in sorted(rows)], ["c", "b", "a"])


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
