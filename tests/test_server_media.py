"""Les routes qui servent des fichiers : plan de lecture, téléchargement, conversion à la volée, sous-titres, livres, films des sagas."""

from __future__ import annotations

import json
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from tests import fixtures
from tests.rig import MiniHub, call
from tests.test_server_views import entry, item
from hub import accounts, config
from hub.server import app
from hub.server.app import HttpError

ME = accounts.FIRST_ID
PASSWORD = "un-mot-de-passe-ok"


class MediaCase(unittest.TestCase):
    kind = "movies"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "web").mkdir()
        self.lib = config.Library("lib", "Bibliothèque", self.kind, self.root / "lib")
        self.lib.path.mkdir()
        self.hub = MiniHub(self.root, [self.lib])
        self.db = self.hub.db

    def fill(self, items, entries):
        self.db.replace_library("lib", items, entries)

    def get(self, target, **kw):
        return call(self.hub, "GET", target, **kw)[1]

    def post(self, target, body=None, **kw):
        return call(self.hub, "POST", target, {} if body is None else body, **kw)[1]


PROBE = {"duration": 0, "audio": [{"index": 1, "lang": "fre", "title": "", "commentary": False, "codec": "ac3", "channels": 6},
                                  {"index": 2, "lang": "eng", "title": "Commentaire", "commentary": True, "codec": "aac", "channels": 2}],
         "subs": [{"index": 3, "lang": "fre", "title": "", "forced": True, "default": True, "text": True}, {"index": 4, "lang": "eng", "title": "", "forced": False, "default": False, "text": False}],
         "video": {"codec": "h264", "width": 1920, "height": 1080, "hdr": False}}


class TestPlay(MediaCase):
    def setUp(self):
        super().setUp()
        self.fill([item("sh", "show", title="Dark"), item("mv", title="Film")],
                  [entry("e1", "sh", season=1, number=1, duration=100.0), entry("e2", "sh", season=1, number=2, duration=100.0), entry("f1", "mv", duration=120.0),
                   entry("k1", "mv", kind="audio")])
        self.probe = json.loads(json.dumps(PROBE))
        self.decision = {"mode": "direct", "why": "lisible tel quel", "audio": 2, "copy_audio": True}
        for patcher in (mock.patch.object(app, "probe_of", lambda hub, e: self.probe), mock.patch.object(app.prober, "plan", lambda *a: self.decision),
                        mock.patch.object(app.subtitles, "side_files", lambda path: self.sides)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sides = []

    def play(self, ident="e1", **body):
        return self.post(f"/api/play/{ident}", body)

    def test_a_file_that_plays_as_it_is(self):
        out = self.play().json()
        self.assertEqual((out["entry"], out["item"], out["title"], out["show"], out["season"], out["number"]), ("e1", "sh", "Titre e1", "Dark", 1, 1))
        self.assertEqual((out["mode"], out["why"], out["url"], out["seekable"], out["warning"]), ("direct", "lisible tel quel", "/media/e1/file", True, ""))
        self.assertEqual(out["video"], {"codec": "h264", "width": 1920, "height": 1080, "hdr": False})
        self.assertEqual(out["next"], {"id": "e2", "title": "Titre e2", "season": 1, "number": 2})

    def test_the_duration_is_the_one_of_the_probe_or_else_the_one_of_the_file(self):
        self.assertEqual(self.play().json()["duration"], 100.0)
        self.probe["duration"] = 90.5
        self.assertEqual(self.play().json()["duration"], 90.5)

    def test_the_audio_tracks_say_which_one_is_chosen(self):
        audio = self.play().json()["audio"]
        self.assertEqual([(t["index"], t["selected"], t["commentary"], t["lang"]) for t in audio], [(1, False, False, "fre"), (2, True, True, "eng")])
        self.assertEqual(audio[0]["label"], "Français (AC-3 5.1)")
        self.decision["audio"] = 1
        self.assertEqual([t["selected"] for t in self.play().json()["audio"]], [True, False])

    def test_the_subtitles_have_an_address_when_they_are_text_and_a_side_file_is_never_the_default(self):
        self.sides = [{"index": 0, "label": "Français", "lang": "fre", "forced": False, "path": Path("x.srt")}]
        subs = self.play().json()["subs"]
        self.assertEqual([(s["id"], s["where"], s["default"], s["forced"], s["text"], s["url"]) for s in subs],
                         [("e3", "mkv", True, True, True, "/api/subs/e1/e3.vtt"), ("e4", "mkv", False, False, False, ""), ("s0", "fichier", False, False, True, "/api/subs/e1/s0.vtt")])
        self.assertEqual(subs[0]["label"], "Français · Forcé")

    def test_a_converted_file_cannot_be_sought_and_a_4k_is_flagged(self):
        self.decision = {"mode": "transcode", "why": "codec", "audio": 1, "copy_audio": False}
        self.probe["video"] = {"codec": "hevc", "width": 3840, "height": 2160, "hdr": True}
        out = self.play(max_kbps=4000).json()
        self.assertEqual((out["seekable"], out["mode"]), (False, "transcode"))
        self.assertEqual(out["url"], "/media/e1/stream?m=transcode&ca=0&k=4000&h=1080&c=2&a=1")
        self.assertIn("4K", out["warning"])

    def test_a_file_without_video_stream_has_no_video_summary(self):
        self.probe["video"] = {}
        self.assertIsNone(self.play().json()["video"])

    def test_the_resume_position_and_the_absence_of_a_next_file(self):
        self.db.save_progress(ME, "f1", 60, 120, False)
        out = self.play("f1").json()
        self.assertEqual((out["resume"], out["next"], out["show"]), (60, None, ""))

    def test_only_a_video_can_be_played(self):
        reply = self.play("k1")
        self.assertEqual((reply.status, reply.json()), (400, {"error": "ce fichier n'est pas une vidéo"}))
        self.assertEqual(self.play("nope").status, 404)
        self.assertEqual(self.play("e1", max_kbps="x").status, 400)


class TestMediaFile(MediaCase):
    def setUp(self):
        super().setUp()
        (self.lib.path / "Film (2020).mkv").write_bytes(b"0123456789")
        self.fill([item("mv")], [dict(entry("f1", "mv"), rel="Film (2020).mkv")])

    def test_the_file_is_served_with_ranges_and_a_cache_of_one_hour(self):
        reply = self.get("/media/f1/file")
        self.assertEqual((reply.status, reply.body, reply.headers["cache-control"]), (200, b"0123456789", "private, max-age=3600"))
        self.assertNotIn("content-disposition", reply.headers)
        part = self.get("/media/f1/file", headers={"Range": "bytes=2-4"})
        self.assertEqual((part.status, part.body), (206, b"234"))

    def test_a_download_names_the_file(self):
        reply = self.get("/media/f1/file?download=1")
        self.assertEqual(reply.headers["content-disposition"], "attachment; filename*=UTF-8''" + quote("Film (2020).mkv"))
        self.assertNotIn("content-disposition", self.get("/media/f1/file?download=").headers)

    def test_an_unknown_file_or_a_vanished_one_is_a_404(self):
        self.assertEqual(self.get("/media/nope/file").status, 404)
        (self.lib.path / "Film (2020).mkv").unlink()
        self.assertEqual(self.get("/media/f1/file").status, 404)


class TestStreamCommand(unittest.TestCase):
    def command(self, query, audio=True):
        hub = types.SimpleNamespace(cfg=types.SimpleNamespace(ffmpeg="ff"))
        probe = {"audio": [{"index": 1}] if audio else []}
        with mock.patch.object(app.prober, "ffmpeg_command", return_value=["ff"]) as built:
            app.stream_command(hub, Path("f.mkv"), probe, query)
        args, kwargs = built.call_args
        return args[3], kwargs

    def test_the_defaults_copy_the_picture_and_the_sound(self):
        decision, kwargs = self.command({})
        self.assertEqual(decision, {"mode": "remux", "audio": None, "copy_video": True, "copy_audio": True})
        self.assertEqual(kwargs, {"start": 0.0, "max_kbps": 0, "max_height": 0, "channels": 2})

    def test_a_conversion_copies_nothing(self):
        decision, _ = self.command({"m": "transcode", "ca": "1"})
        self.assertEqual((decision["mode"], decision["copy_video"], decision["copy_audio"]), ("transcode", False, False))

    def test_the_sound_is_copied_only_when_asked_with_a_one(self):
        self.assertTrue(self.command({"ca": "1"})[0]["copy_audio"])
        for value in ("0", "2"):
            self.assertFalse(self.command({"ca": value})[0]["copy_audio"], value)

    def test_the_audio_track_asked_counts_only_when_the_file_has_audio(self):
        self.assertEqual(self.command({"a": "2"})[0]["audio"], 2)
        self.assertEqual(self.command({"a": "0"})[0]["audio"], 0)
        self.assertIsNone(self.command({"a": "2"}, audio=False)[0]["audio"])

    def test_the_numbers_asked_are_kept_within_the_limits(self):
        _, kwargs = self.command({"start": "12.5", "k": "3000", "h": "720", "c": "3"})
        self.assertEqual(kwargs, {"start": 12.5, "max_kbps": 3000, "max_height": 720, "channels": 3})
        self.assertEqual(self.command({"start": "-5"})[1]["start"], 0.0)
        self.assertEqual([self.command({"c": value})[1]["channels"] for value in ("0", "1", "6", "9")], [1, 1, 6, 6])

    def test_a_number_that_is_not_one_is_refused(self):
        for key in ("a", "k", "h", "c", "ca", "start"):
            with self.assertRaises(HttpError) as caught:
                self.command({key: "x"})
            self.assertEqual((caught.exception.status, caught.exception.message), (400, f"paramètre {key} invalide"), key)

    def test_query_number_defaults_to_zero(self):
        self.assertEqual(app.query_number({}, "k"), 0)
        self.assertEqual(app.query_number({"k": "7"}, "k"), 7)
        self.assertEqual(app.query_number({}, "k", int, 5), 5)
        self.assertEqual(app.query_number({"s": "1.5"}, "s", float, 0.0), 1.5)


class TestSubtitles(MediaCase):
    def setUp(self):
        super().setUp()
        (self.lib.path / "f.mkv").write_bytes(b"x")
        self.fill([item("mv")], [dict(entry("f1", "mv"), rel="f.mkv", mtime=77)])
        self.side = self.lib.path / "f.fr.srt"
        self.side.write_text("1", encoding="utf-8")
        self.sides = [{"index": 0, "label": "Français", "lang": "fre", "forced": False, "path": self.side}]
        patchers = (mock.patch.object(app.subtitles, "side_files", lambda path: self.sides), mock.patch.object(app.subtitles, "ffmpeg_vtt", return_value="WEBVTT\n\nmkv"),
                    mock.patch.object(app.subtitles, "side_vtt", return_value="WEBVTT\n\nfichier"))
        self.inside, self.beside = [patcher.start() for patcher in patchers][1:]
        for patcher in patchers:
            self.addCleanup(patcher.stop)

    def test_an_embedded_track_is_converted_once_and_kept(self):
        reply = self.get("/api/subs/f1/e3.vtt")
        self.assertEqual((reply.status, reply.body, reply.headers["content-type"], reply.headers["etag"], reply.headers["cache-control"]),
                         (200, b"WEBVTT\n\nmkv", "text/vtt; charset=utf-8", '"f1-77-e3"', "private, no-cache"))
        self.inside.assert_called_once_with("", self.lib.path / "f.mkv", 3)
        self.assertEqual((self.root / "data" / "subs" / "f1-77-e3.vtt").read_bytes(), b"WEBVTT\n\nmkv")
        self.get("/api/subs/f1/e3.vtt")
        self.assertEqual(self.inside.call_count, 1)

    def test_the_stamp_lets_the_browser_keep_its_copy(self):
        again = self.get("/api/subs/f1/e3.vtt", headers={"If-None-Match": '"f1-77-e3"'})
        self.assertEqual((again.status, again.body), (304, b""))
        self.assertEqual(self.get("/api/subs/f1/e3.vtt", headers={"If-None-Match": '"autre"'}).status, 200)
        self.assertEqual(call(self.hub, "HEAD", "/api/subs/f1/e3.vtt")[1].body, b"")

    def test_a_file_next_to_the_video_is_converted_with_its_size_and_date_in_the_stamp(self):
        reply = self.get("/api/subs/f1/s0.vtt")
        info = self.side.stat()
        self.assertEqual((reply.status, reply.body, reply.headers["etag"]), (200, b"WEBVTT\n\nfichier", f'"f1-77-s0-{info.st_size}-{info.st_mtime_ns}"'))
        self.beside.assert_called_once_with("", self.side)

    def test_a_side_file_that_is_not_there_is_a_404(self):
        for index in (1, 2):
            self.assertEqual(self.get(f"/api/subs/f1/s{index}.vtt").json(), {"error": "sous-titres introuvables"}, index)
        self.sides = []
        self.assertEqual(self.get("/api/subs/f1/s0.vtt").status, 404)
        self.sides = [{"index": 0, "label": "x", "lang": "fre", "forced": False, "path": self.lib.path / "disparu.srt"}]
        self.assertEqual(self.get("/api/subs/f1/s0.vtt").status, 404)

    def test_a_conversion_that_fails_is_a_500_with_its_message(self):
        for error in (RuntimeError("ffmpeg a échoué"), OSError("illisible")):
            self.inside.side_effect = error
            reply = self.get("/api/subs/f1/e9.vtt")
            self.assertEqual((reply.status, reply.json()), (500, {"error": str(error)}))
        self.assertFalse((self.root / "data" / "subs" / "f1-77-e9.vtt").exists())

    def test_the_folder_of_the_cache_may_already_exist(self):
        self.get("/api/subs/f1/e3.vtt")
        self.assertEqual(self.get("/api/subs/f1/e4.vtt").status, 200)


class TestPgsRoute(MediaCase):
    PGS = {"subs": [{"index": 0, "lang": "fre", "title": "", "forced": False, "default": False, "text": False, "codec": "hdmv_pgs_subtitle"},
                    {"index": 1, "lang": "eng", "title": "", "forced": False, "default": False, "text": True, "codec": "subrip"}]}

    def setUp(self):
        super().setUp()
        (self.lib.path / "f.mkv").write_bytes(b"x")
        self.fill([item("mv")], [dict(entry("f1", "mv"), rel="f.mkv", mtime=77)])
        self.side = self.lib.path / "f.fr.srt"
        self.side.write_text("1", encoding="utf-8")
        self.sides = [{"index": 0, "label": "Français", "lang": "fre", "forced": False, "path": self.side}]
        patchers = (mock.patch.object(app, "probe_of", lambda hub, e: self.PGS), mock.patch.object(app.subtitles, "side_files", lambda path: self.sides),
                    mock.patch.object(app.subtitles, "ffmpeg_sup", return_value=b"PGS"))
        self.extract = [patcher.start() for patcher in patchers][2]
        for patcher in patchers:
            self.addCleanup(patcher.stop)

    def test_a_pgs_track_is_extracted_once_and_kept(self):
        reply = self.get("/api/subs/f1/e0.sup")
        self.assertEqual((reply.status, reply.body, reply.headers["content-type"], reply.headers["etag"], reply.headers["cache-control"]),
                         (200, b"PGS", "application/octet-stream", '"f1-77-e0"', "private, no-cache"))
        self.extract.assert_called_once_with("", self.lib.path / "f.mkv", 0)
        self.assertEqual((self.root / "data" / "subs" / "f1-77-e0.sup").read_bytes(), b"PGS")
        self.get("/api/subs/f1/e0.sup")
        self.assertEqual(self.extract.call_count, 1)

    def test_the_stamp_lets_the_browser_keep_its_copy(self):
        again = self.get("/api/subs/f1/e0.sup", headers={"If-None-Match": '"f1-77-e0"'})
        self.assertEqual((again.status, again.body), (304, b""))
        self.assertEqual(self.get("/api/subs/f1/e0.sup", headers={"If-None-Match": '"autre"'}).status, 200)
        self.assertEqual(call(self.hub, "HEAD", "/api/subs/f1/e0.sup")[1].body, b"")

    def test_only_a_pgs_track_of_the_file_has_one(self):
        for target in ("/api/subs/f1/e1.sup", "/api/subs/f1/e7.sup", "/api/subs/f1/s0.sup"):       # texte, absente, fichier voisin
            self.assertEqual(self.get(target).json(), {"error": "sous-titres introuvables"}, target)
        self.extract.assert_not_called()

    def test_an_extraction_that_fails_is_a_500_with_its_message(self):
        for error in (RuntimeError("ffmpeg a échoué"), OSError("illisible")):
            self.extract.side_effect = error
            reply = self.get("/api/subs/f1/e0.sup")
            self.assertEqual((reply.status, reply.json()), (500, {"error": str(error)}))
        self.assertFalse((self.root / "data" / "subs" / "f1-77-e0.sup").exists())


class TestBookRoute(MediaCase):
    kind = "books"

    def setUp(self):
        super().setUp()
        fixtures.write_cbz(self.lib.path / "t1.cbz", pages=4)
        fixtures.write_cbz(self.lib.path / "t2.cbz", pages=2)
        fixtures.write_epub(self.lib.path / "r.epub", chapters=3)
        (self.lib.path / "casse.cbz").write_bytes(b"pas une archive")
        (self.lib.path / "x.pdf").write_bytes(b"%PDF")
        self.fill([item("sr", "books"), item("ro", "books")],
                  [dict(entry("k1", "sr", kind="book", number=1), rel="t1.cbz", meta='{"format": "cbz"}'), dict(entry("k2", "sr", kind="book", number=2), rel="t2.cbz", meta='{"format": "cbz"}'),
                   dict(entry("r1", "ro", kind="book"), rel="r.epub", meta='{"format": "epub"}'), dict(entry("b1", "ro", kind="book", number=5), rel="casse.cbz", meta='{"format": "cbz"}'),
                   dict(entry("p1", "ro", kind="book", number=6), rel="x.pdf", meta='{"format": "pdf"}'), entry("v1", "ro")])

    def test_a_cbz_gives_its_pages_where_to_read_them_and_what_follows(self):
        out = self.get("/api/book/k1").json()
        self.assertEqual((out["format"], out["pages"], out["page_url"], out["position"], out["file"]), ("cbz", 4, "/book/k1/page/{n}", 0, "/media/k1/file"))
        self.assertEqual((out["id"], out["item"], out["title"], out["next"]), ("k1", "sr", "Titre k1", {"id": "k2", "title": "Titre k2"}))
        self.assertIsNone(self.get("/api/book/k2").json()["next"])

    def test_an_epub_gives_its_chapters(self):
        out = self.get("/api/book/r1").json()
        self.assertEqual(out["chapters"], ["/book/r1/epub/c1.xhtml", "/book/r1/epub/c2.xhtml", "/book/r1/epub/c3.xhtml"])
        self.assertNotIn("pages", out)

    def test_another_format_is_not_readable(self):
        out = self.get("/api/book/p1").json()
        self.assertIs(out["readable"], False)
        self.assertNotIn("pages", out)
        self.assertNotIn("readable", self.get("/api/book/k1").json())

    def test_the_page_where_the_reader_stopped_is_given_unless_the_book_is_done(self):
        self.db.save_progress(ME, "k1", 3, 4, False)
        self.assertEqual(self.get("/api/book/k1").json()["position"], 3)
        self.db.save_progress(ME, "k1", 3, 4, True)
        self.assertEqual(self.get("/api/book/k1").json()["position"], 0)
        self.assertEqual(self.get("/api/book/k2").json()["position"], 0)

    def test_a_book_that_cannot_be_opened_is_a_422_and_a_video_is_a_400(self):
        self.assertEqual(self.get("/api/book/b1").status, 422)
        reply = self.get("/api/book/v1")
        self.assertEqual((reply.status, reply.json()), (400, {"error": "ce fichier n'est pas un livre"}))
        self.assertEqual(self.get("/api/book/nope").status, 404)

    def test_a_page_comes_with_its_type_and_a_missing_one_is_a_404(self):
        reply = self.get("/book/k1/page/0")
        self.assertEqual((reply.status, reply.body, reply.headers["content-type"], reply.headers["cache-control"]), (200, fixtures.PNG, "image/png", "private, max-age=86400"))
        self.assertEqual(self.get("/book/k1/page/4").json(), {"error": "page inexistante"})
        self.assertEqual(call(self.hub, "HEAD", "/book/k1/page/1")[1].body, b"")

    def test_a_file_of_the_epub_is_served_with_its_security_policy(self):
        reply = self.get("/book/r1/epub/c1.xhtml")
        self.assertEqual((reply.status, reply.headers["content-type"], reply.headers["cache-control"]), (200, "application/xhtml+xml; charset=utf-8", "private, max-age=86400"))
        self.assertIn("script-src 'none'", reply.headers["content-security-policy"])
        self.assertIn(b"Chapitre 1", reply.body)
        self.assertEqual(self.get("/book/r1/epub/absent.xhtml").status, 404)
        self.assertEqual(call(self.hub, "HEAD", "/book/r1/epub/c1.xhtml")[1].body, b"")


class TestBookDirection(MediaCase):
    """`rtl` : le sens de lecture par défaut d'un tome - son champ Manga, sinon la section de sa série."""
    kind = "books"

    def setUp(self):
        super().setUp()
        for name in ("a.cbz", "b.cbz"):
            fixtures.write_cbz(self.lib.path / name, pages=2)
        fixtures.write_epub(self.lib.path / "r.epub")
        (self.lib.path / "x.pdf").write_bytes(b"%PDF")

    def shelve(self, section, volume_meta, rel="a.cbz"):
        meta = {"section": section} if section else {}
        self.fill([item("sr", "series", meta=meta)], [dict(entry("v", "sr", kind="book", number=1), rel=rel, meta=json.dumps(volume_meta))])
        return self.get("/api/book/v").json()

    def rtl(self, section, info, **extra):
        return self.shelve(section, {"format": "cbz", "info": info, **extra})["rtl"]

    def test_the_manga_field_of_the_volume_decides(self):
        self.assertIs(self.rtl("BD", {"Manga": "YesAndRightToLeft"}), True)
        self.assertIs(self.rtl("Manga", {"Manga": "Yes"}), False)
        self.assertIs(self.rtl("Manga", {"Manga": "No"}), False)

    def test_the_field_is_read_without_regard_to_case(self):
        self.assertIs(self.rtl("BD", {"Manga": "yesandrighttoleft"}), True)
        self.assertIs(self.rtl("Manga", {"Manga": "YES"}), False)
        self.assertIs(self.rtl("Manga", {"Manga": "no"}), False)

    def test_without_the_field_the_section_of_the_series_decides(self):
        self.assertIs(self.rtl("Manga", {"Title": "Tome 1"}), True)
        self.assertIs(self.rtl("BD", {"Title": "Tome 1"}), False)
        self.assertIs(self.rtl("Comics", {}), False)

    def test_a_series_without_a_section_reads_left_to_right(self):
        self.assertIs(self.rtl(None, {}), False)

    def test_a_value_that_says_nothing_leaves_it_to_the_section(self):
        self.assertIs(self.rtl("Manga", {"Manga": "Unknown"}), True)
        self.assertIs(self.rtl("BD", {"Manga": "Unknown"}), False)

    def test_a_volume_without_a_sheet_follows_its_section(self):
        self.assertIs(self.shelve("Manga", {"format": "cbz"})["rtl"], True)
        self.assertIs(self.shelve("BD", {"format": "cbz"})["rtl"], False)

    def test_an_epub_or_an_unreadable_format_has_no_direction(self):
        self.assertNotIn("rtl", self.shelve("Manga", {"format": "epub", "info": {"Manga": "YesAndRightToLeft"}}, rel="r.epub"))
        self.assertNotIn("rtl", self.shelve("Manga", {"format": "pdf", "info": {"Manga": "YesAndRightToLeft"}}, rel="x.pdf"))


class TestSubtitleList(unittest.TestCase):
    def test_a_file_next_to_the_video_is_never_the_default_one(self):
        side = {"index": 0, "label": "Français", "lang": "fre", "forced": True, "path": Path("x.srt")}
        with mock.patch.object(app.subtitles, "side_files", return_value=[side]):
            out = app.subtitle_list(None, {"id": "e1"}, {"subs": []}, Path("x.mkv"))
        self.assertEqual(out, [{"id": "s0", "label": "Français", "lang": "fre", "forced": True, "default": False, "text": True, "image": "", "where": "fichier", "url": "/api/subs/e1/s0.vtt"}])

    def test_a_blu_ray_track_has_an_address_but_a_dvd_one_has_none(self):
        probe = {"subs": [{"index": 0, "lang": "fre", "title": "", "forced": False, "default": False, "text": False, "codec": "hdmv_pgs_subtitle"},
                          {"index": 1, "lang": "eng", "title": "", "forced": False, "default": False, "text": False, "codec": "dvd_subtitle"},
                          {"index": 2, "lang": "eng", "title": "", "forced": False, "default": False, "text": True, "codec": "subrip"}]}
        with mock.patch.object(app.subtitles, "side_files", return_value=[]):
            out = app.subtitle_list(None, {"id": "e1"}, probe, Path("x.mkv"))
        self.assertEqual([(s["id"], s["text"], s["image"], s["url"]) for s in out],
                         [("e0", False, "pgs", "/api/subs/e1/e0.sup"), ("e1", False, "", ""), ("e2", True, "", "/api/subs/e1/e2.vtt")])


class TestSagaFilms(MediaCase):
    def setUp(self):
        super().setUp()
        rows = [item("m1", title="Alpha 1", grp="Saga A", year="2001", meta={"tmdb": "movie/1"}), item("m2", title="Alpha 2", grp="Saga A", year="2000", meta={"tmdb": "movie/2"}),
                item("m3", title="Beta", year="1999", meta={"tmdb": "movie/3"}), item("m4", title="Sans identifiant")]
        self.fill(rows, [])

    def films(self, query=""):
        reply = self.get("/api/sagas/films" + query)
        self.assertEqual(reply.status, 200)
        out = reply.json()
        return [f["item"]["title"] for f in out["films"]], out["total"]

    def test_the_films_with_an_identifier_by_title(self):
        self.assertEqual(self.films(), (["Alpha 1", "Alpha 2", "Beta"], 3))
        out = self.get("/api/sagas/films").json()["films"][0]
        self.assertEqual((out["tmdb"], out["date"], out["item"]["id"]), (1, "2001", "m1"))

    def test_a_search_looks_at_the_title_and_the_saga(self):
        self.assertEqual(self.films("?q=alpha"), (["Alpha 1", "Alpha 2"], 2))
        self.assertEqual(self.films("?q=SAGA"), (["Alpha 1", "Alpha 2"], 2))
        self.assertEqual(self.films("?q=bet"), (["Beta"], 1))
        self.assertEqual(self.films("?q=zzz"), ([], 0))

    def test_a_saga_is_listed_in_the_order_of_release(self):
        self.assertEqual(self.films("?group=Saga%20A"), (["Alpha 2", "Alpha 1"], 2))
        self.assertEqual(self.films("?group=Saga%20A&q=1"), (["Alpha 1"], 1))
        self.assertEqual(self.films("?group=Inconnue"), ([], 0))

    def test_the_number_of_films_is_limited_but_the_total_is_not(self):
        self.assertEqual(self.films("?limit=1"), (["Alpha 1"], 3))
        self.assertEqual(self.films("?limit=0")[0], ["Alpha 1"])
        self.assertEqual(self.films("?limit=-4")[0], ["Alpha 1"])
        self.assertEqual(self.films("?limit=2"), (["Alpha 1", "Alpha 2"], 3))

    def test_a_limit_that_is_not_a_number_is_forty(self):
        self.fill([item(f"n{n:02d}", title=f"Film {n:02d}", meta={"tmdb": f"movie/{n + 10}"}) for n in range(45)], [])
        names, total = self.films("?limit=beaucoup")
        self.assertEqual((len(names), total), (40, 45))
        self.assertEqual(len(self.films("?limit=100")[0]), 45)

    def test_a_simple_account_is_refused(self):
        self.hub.cfg.admin = ""
        accounts.create(self.hub.db, "alice", PASSWORD)
        bob = accounts.create(self.hub.db, "bob", PASSWORD)
        reply = self.get("/api/sagas/films", headers=self.hub.cookie(bob))
        self.assertEqual(reply.status, 403)


if __name__ == "__main__":
    unittest.main()
