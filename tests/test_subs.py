"""Les sous-titres posés à côté d'une vidéo : lesquels, et comment on les nomme."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hub.media import subs


class TestSideFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.video = self.tmp / "Film (2020).mkv"

    def listing(self, *names):
        for name in names:
            (self.tmp / name).write_text("x", encoding="utf-8")
        return [(s["index"], s["path"].name, s["label"], s["lang"], s["forced"]) for s in subs.side_files(self.video)]

    def test_only_subtitle_files_with_the_name_of_the_video_are_listed(self):
        found = self.listing("Film (2020).fr.srt", "Film (2020).mkv", "Film (2020).nfo", "Autre.fr.srt", "film (2020).en.vtt", "Film (2020).ass", "Film (2020).ssa", "Film (2020).txt")
        self.assertEqual([name for _, name, *_ in found], ["Film (2020).ass", "film (2020).en.vtt", "Film (2020).fr.srt", "Film (2020).ssa"])        # triés sans tenir compte de la casse, et le nom de la vidéo aussi
        self.assertEqual([i for i, *_ in found], [0, 1, 2, 3])

    def test_the_language_gives_the_label(self):
        self.assertEqual(self.listing("Film (2020).fr.srt"), [(0, "Film (2020).fr.srt", "Français", "fr", False)])
        found = self.listing("Film (2020).ENG.srt")
        self.assertEqual([(label, lang) for _, name, label, lang, _ in found if "ENG" in name], [("English", "eng")])

    def test_forced_and_sdh_are_flagged_in_any_order_and_spelling(self):
        found = {name: (label, lang, forced) for _, name, label, lang, forced in self.listing(
            "Film (2020).fr.forced.srt", "Film (2020).en.SDH.srt", "Film (2020).jpn.Forcé.sdh.srt", "Film (2020).forced.srt", "Film (2020)_es-cc.srt")}
        self.assertEqual(found["Film (2020).fr.forced.srt"], ("Français · Forcé", "fr", True))
        self.assertEqual(found["Film (2020).en.SDH.srt"], ("English · SDH", "en", False))
        self.assertEqual(found["Film (2020).jpn.Forcé.sdh.srt"], ("日本語 · Forcé · SDH", "jpn", True))
        self.assertEqual(found["Film (2020).forced.srt"], ("forced · Forcé", "", True))          # sans langue, le nom tel quel, puis le drapeau
        self.assertEqual(found["Film (2020)_es-cc.srt"], ("Español · SDH", "es", False))

    def test_an_unknown_language_keeps_what_the_name_says_and_no_name_is_generic(self):
        found = {name: (label, lang) for _, name, label, lang, _ in self.listing("Film (2020).Commentaire du réalisateur.srt", "Film (2020).srt", "Film (2020).und.srt", "Film (2020).xx.forced.srt")}
        self.assertEqual(found["Film (2020).Commentaire du réalisateur.srt"], ("Commentaire du réalisateur", ""))
        self.assertEqual(found["Film (2020).srt"], ("Sous-titres", ""))
        self.assertEqual(found["Film (2020).und.srt"], ("Sous-titres", "und"))              # « und » est connu mais sans nom
        self.assertEqual(found["Film (2020).xx.forced.srt"], ("xx.forced · Forcé", ""))       # tout ce que dit la fin du nom est gardé quand la langue est inconnue

    def test_a_folder_that_cannot_be_read_has_none(self):
        self.assertEqual(subs.side_files(self.tmp / "absent" / "Film.mkv"), [])
        self.assertEqual(subs.siblings(self.tmp / "absent"), [])

    def test_a_folder_already_listed_is_not_read_again(self):
        self.listing("Film (2020).fr.srt", "Film (2020).en.srt")
        listed = subs.siblings(self.tmp)
        (self.tmp / "Film (2020).es.srt").write_text("x", encoding="utf-8")           # arrivé après : la liste donnée fait foi
        self.assertEqual([s["lang"] for s in subs.side_files(self.video, listed)], ["en", "fr"])
        self.assertEqual([s["lang"] for s in subs.side_files(self.video)], ["en", "es", "fr"])


class TestLanguage(unittest.TestCase):
    def test_a_known_code_gives_its_name_whatever_its_case(self):
        self.assertEqual(subs.language("fr"), "Français")
        self.assertEqual(subs.language("ENG"), "English")

    def test_an_unknown_code_is_kept_and_no_code_is_empty(self):
        self.assertEqual(subs.language("xx"), "xx")
        self.assertEqual(subs.language(""), "")
        self.assertEqual(subs.language(None), "")
        self.assertEqual(subs.language("und"), "")


class TestConversion(unittest.TestCase):
    def test_decode_tries_utf8_then_cp1252_then_latin1(self):
        self.assertEqual(subs.decode("﻿é".encode("utf-8")), "é")                      # le BOM disparaît
        self.assertEqual(subs.decode("é".encode("cp1252")), "é")
        self.assertEqual(subs.decode(b"\x81"), "\x81")                                     # cp1252 ne connaît pas 0x81 : latin-1 le garde

    def test_srt_becomes_webvtt(self):
        srt = "﻿1\r\n00:00:01,500 --> 01:02:03,004\r\nBonjour\r\n\r\n2\r2\r"
        self.assertEqual(subs.srt_to_vtt(srt), "WEBVTT\n\n1\n00:00:01.500 --> 01:02:03.004\nBonjour\n\n2\n2\n")

    def test_side_vtt_reads_vtt_and_srt_itself_and_leaves_the_rest_to_ffmpeg(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "a.VTT").write_bytes("WEBVTT\n\nsalut".encode("utf-8"))
        (tmp / "b.SRT").write_bytes("00:00:01,000 --> 00:00:02,000\nsalut".encode("utf-8"))
        (tmp / "c.ass").write_bytes(b"[Script Info]")
        self.assertEqual(subs.side_vtt(None, tmp / "a.VTT"), "WEBVTT\n\nsalut")
        self.assertEqual(subs.side_vtt(None, tmp / "b.SRT"), "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nsalut")
        with mock.patch.object(subs, "ffmpeg_vtt", return_value="WEBVTT\n") as converted:
            self.assertEqual(subs.side_vtt("ffmpeg", tmp / "c.ass"), "WEBVTT\n")
        converted.assert_called_once_with("ffmpeg", tmp / "c.ass")


class TestFfmpegVtt(unittest.TestCase):
    def run_with(self, returncode=0, stdout=b"WEBVTT\n", stderr=b"", **kwargs):
        done = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
        with mock.patch.object(subs.subprocess, "run", return_value=done) as run:
            result = subs.ffmpeg_vtt("ffmpeg", "film.mkv", **kwargs)
        return result, run

    def test_without_ffmpeg_nothing_is_converted(self):
        with self.assertRaisesRegex(RuntimeError, "ffmpeg est introuvable"):
            subs.ffmpeg_vtt("", "film.mkv")

    def test_the_whole_file_or_one_track_is_asked_for(self):
        result, run = self.run_with()
        self.assertEqual(result, "WEBVTT\n")
        self.assertEqual(run.call_args.args[0], ["ffmpeg", "-v", "error", "-nostdin", "-i", "film.mkv", "-f", "webvtt", "pipe:1"])
        self.assertEqual(run.call_args.kwargs, {"capture_output": True, "timeout": 120})
        _, run = self.run_with(stream=0, timeout=5)
        self.assertEqual(run.call_args.args[0], ["ffmpeg", "-v", "error", "-nostdin", "-i", "film.mkv", "-map", "0:s:0", "-f", "webvtt", "pipe:1"])
        self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_a_failure_says_what_ffmpeg_said(self):
        with self.assertRaisesRegex(RuntimeError, "^oups é$"):
            self.run_with(returncode=1, stderr=" oups é\n".encode("utf-8"))
        with self.assertRaisesRegex(RuntimeError, "^conversion impossible$"):
            self.run_with(returncode=1, stderr=b"")
        with self.assertRaisesRegex(RuntimeError, "^conversion impossible$"):
            self.run_with(returncode=0, stdout=b"")                    # un succès qui ne produit rien est un échec
        with self.assertRaises(RuntimeError) as caught:
            self.run_with(returncode=1, stderr=b"x" * 500)
        self.assertEqual(len(str(caught.exception)), 200)


class TestFfmpegSup(unittest.TestCase):
    def run_with(self, returncode=0, stdout=b"PG", stderr=b"", **kwargs):
        done = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
        with mock.patch.object(subs.subprocess, "run", return_value=done) as run:
            result = subs.ffmpeg_sup("ffmpeg", "film.mkv", 2, **kwargs)
        return result, run

    def test_only_the_blu_ray_codec_is_a_pgs_track(self):
        self.assertTrue(subs.is_pgs("hdmv_pgs_subtitle"))
        for codec in ("dvd_subtitle", "subrip", "ass", "", None):
            self.assertFalse(subs.is_pgs(codec), codec)

    def test_the_track_is_copied_as_it_is_without_decoding_anything(self):
        result, run = self.run_with()
        self.assertEqual(result, b"PG")
        self.assertEqual(run.call_args.args[0], ["ffmpeg", "-v", "error", "-nostdin", "-i", "film.mkv", "-map", "0:s:2", "-c", "copy", "-f", "sup", "pipe:1"])
        self.assertEqual(run.call_args.kwargs, {"capture_output": True, "timeout": 900})        # un remux de 30 Go se lit en plusieurs minutes
        self.assertEqual(self.run_with(timeout=5)[1].call_args.kwargs["timeout"], 5)

    def test_without_ffmpeg_nothing_is_extracted(self):
        with self.assertRaisesRegex(RuntimeError, "ffmpeg est introuvable"):
            subs.ffmpeg_sup("", "film.mkv", 0)

    def test_a_failure_says_what_ffmpeg_said(self):
        with self.assertRaisesRegex(RuntimeError, "^oups$"):
            self.run_with(returncode=1, stderr=b" oups\n")
        with self.assertRaisesRegex(RuntimeError, "^extraction impossible$"):
            self.run_with(returncode=1, stderr=b"")
        with self.assertRaisesRegex(RuntimeError, "^extraction impossible$"):
            self.run_with(returncode=0, stdout=b"")


if __name__ == "__main__":
    unittest.main()
