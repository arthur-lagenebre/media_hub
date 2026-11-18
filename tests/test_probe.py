"""Comment jouer un fichier : le résumé de ffprobe, la décision (direct, remux, transcodage) et la ligne de commande de ffmpeg. Fonctions pures, essayées sur des fichiers décrits à la main."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hub.media import probe as prober

CAPS = {"video": ["h264"], "audio": ["aac", "mp3"], "containers": ["mp4", "webm"], "channels": 2, "height": 1080}


def audio(index=0, codec="aac", channels=2, default=True, commentary=False):
    return {"index": index, "codec": codec, "channels": channels, "lang": "fr", "title": "", "default": default, "commentary": commentary}


def info(container="mp4", bitrate=4_000_000, tracks=None, **video):
    base = {"codec": "h264", "profile": "High", "width": 1920, "height": 1080, "fps": 24, "depth": 8, "pix_fmt": "yuv420p", "hdr": False, "bitrate": 3_500_000}
    return {"container": container, "duration": 100.0, "bitrate": bitrate, "size": 1, "video": {**base, **video} if video is not None else None,
            "audio": [audio()] if tracks is None else tracks, "subs": []}


class TestSummarize(unittest.TestCase):
    def test_an_empty_answer_is_an_empty_file(self):
        self.assertEqual(prober.summarize({}), {"container": "", "duration": 0.0, "bitrate": 0, "size": 0, "video": None, "audio": [], "subs": [], "chapters": [], "probe_version": prober.PROBE_VERSION})

    def test_a_film_with_two_audio_tracks_and_subtitles(self):
        data = {"format": {"format_name": "matroska,webm", "duration": "95.5", "bit_rate": "5000000", "size": "123"},
                "streams": [{"codec_type": "video", "codec_name": "h264", "profile": "High", "width": 1920, "height": 1080, "avg_frame_rate": "24000/1001", "pix_fmt": "yuv420p", "bit_rate": "4000000"},
                            {"codec_type": "audio", "codec_name": "eac3", "channels": 6, "tags": {"LANGUAGE": "fre", "title": "VF"}, "disposition": {"default": 1}},
                            {"codec_type": "audio", "codec_name": "aac", "tags": {}, "disposition": {"comment": 1}},
                            {"codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "fre"}, "disposition": {"forced": 1}},
                            {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "disposition": {"default": 1}}]}
        out = prober.summarize(data)
        self.assertEqual((out["container"], out["duration"], out["bitrate"], out["size"]), ("mkv", 95.5, 5_000_000, 123))
        self.assertEqual(out["video"], {"codec": "h264", "profile": "High", "width": 1920, "height": 1080, "fps": 23.976, "depth": 8, "pix_fmt": "yuv420p", "hdr": False, "bitrate": 4_000_000})
        self.assertEqual(out["audio"], [{"index": 0, "codec": "eac3", "channels": 6, "lang": "fre", "title": "VF", "default": True, "commentary": False},
                                        {"index": 1, "codec": "aac", "channels": 2, "lang": "", "title": "", "default": False, "commentary": True}])
        self.assertEqual(out["subs"], [{"index": 0, "codec": "subrip", "lang": "fre", "title": "", "forced": True, "default": False, "text": True},
                                       {"index": 1, "codec": "hdmv_pgs_subtitle", "lang": "", "title": "", "forced": False, "default": True, "text": False}])

    def test_the_container_family(self):
        family = lambda names: prober.summarize({"format": {"format_name": names}})["container"]      # noqa: E731
        self.assertEqual([family(n) for n in ("matroska,webm", "webm", "mov,mp4,m4a,3gp,3g2,mj2", "avi", "flv", "")], ["mkv", "webm", "mp4", "avi", "flv", ""])

    def test_a_cover_picture_is_not_the_video(self):
        data = {"streams": [{"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}}, {"codec_type": "audio", "codec_name": "mp3"}]}
        out = prober.summarize(data)
        self.assertIsNone(out["video"])
        self.assertEqual(len(out["audio"]), 1)

    def test_the_depth_comes_from_the_bits_or_else_from_the_pixel_format(self):
        depth = lambda **v: prober.summarize({"streams": [{"codec_type": "video", **v}]})["video"]["depth"]      # noqa: E731
        self.assertEqual(depth(bits_per_raw_sample="10"), 10)
        self.assertEqual(depth(bits_per_raw_sample="8", pix_fmt="yuv420p10le"), 8)
        self.assertEqual(depth(pix_fmt="yuv420p10le"), 10)
        self.assertEqual(depth(bits_per_raw_sample="N/A", pix_fmt="yuv420p"), 8)
        self.assertEqual(depth(), 8)

    def test_hdr_is_known_by_its_transfer_function(self):
        hdr = lambda transfer: prober.summarize({"streams": [{"codec_type": "video", "color_transfer": transfer}]})["video"]["hdr"]      # noqa: E731
        self.assertEqual([hdr(t) for t in ("smpte2084", "arib-std-b67", "bt709", None)], [True, True, False, False])

    def test_an_unknown_codec_keeps_its_name_and_a_missing_one_is_empty(self):
        out = prober.summarize({"streams": [{"codec_type": "video", "codec_name": "wmv3"}, {"codec_type": "audio", "codec_name": "wmapro"}, {"codec_type": "audio"}]})
        self.assertEqual((out["video"]["codec"], out["audio"][0]["codec"], out["audio"][1]["codec"]), ("wmv3", "wmapro", ""))

    def test_a_video_without_size_or_bitrate_has_zeros(self):
        out = prober.summarize({"streams": [{"codec_type": "video", "codec_name": "h264"}]})["video"]
        self.assertEqual((out["width"], out["height"], out["bitrate"]), (0, 0, 0))

    def test_the_frame_rate(self):
        self.assertEqual([prober._fps(t) for t in ("24000/1001", "25", "0/0", "x/y", "", None)], [23.976, 25.0, 0, 0, 0.0, 0.0])


def chapter(start="0", end="10", title=None):
    tags = {} if title is None else {"title": title}
    return {"start_time": start, "end_time": end, "tags": tags}


def chapters(*raw, duration="100"):
    return prober.summarize({"format": {"duration": duration}, "chapters": list(raw)})["chapters"]


def spans(*raw, **kw):
    return [(c["start"], c["end"]) for c in chapters(*raw, **kw)]


class TestChapters(unittest.TestCase):
    def test_a_chapter_has_a_title_a_start_an_end_and_what_it_lets_skip(self):
        raw = {"id": 1, "time_base": "1/1000", "start": 0, "start_time": "0.000000", "end": 90500, "end_time": "90.500000", "tags": {"TITLE": "Générique de début"}}
        self.assertEqual(chapters(raw), [{"title": "Générique de début", "start": 0.0, "end": 90.5, "skip": "intro"}])

    def test_a_file_without_chapters_has_an_empty_list(self):
        self.assertEqual(prober.summarize({"format": {"duration": "100"}})["chapters"], [])
        for nothing in (None, [], {}, "chapitres", 3):
            self.assertEqual(prober.summarize({"format": {"duration": "100"}, "chapters": nothing})["chapters"], [], nothing)

    def test_the_chapters_are_ordered_by_start(self):
        self.assertEqual(spans(chapter("20", "30"), chapter("0", "10"), chapter("10", "20")), [(0.0, 10.0), (10.0, 20.0), (20.0, 30.0)])

    def test_two_chapters_with_the_same_start_are_one_the_first_of_the_file(self):
        out = chapters(chapter("5", "6", "Premier"), chapter("5.0001", "9", "Second"), chapter("5", "7", "Troisième"))
        self.assertEqual([(c["title"], c["start"], c["end"]) for c in out], [("Premier", 5.0, 6.0)])

    def test_a_chapter_without_a_usable_start_is_dropped(self):
        for start in (None, "", "x", "nan", "inf", "-inf", "-1", -0.5, [], {}, True, 10 ** 400):
            self.assertEqual(spans(chapter(start), chapter("20", "30")), [(20.0, 30.0)], start)
        self.assertEqual(spans({"end_time": "5"}), [])

    def test_something_that_is_not_a_chapter_is_dropped(self):
        self.assertEqual(spans("x", None, 3, [], chapter("1", "2")), [(1.0, 2.0)])

    def test_an_end_that_cannot_be_used_becomes_the_next_start(self):
        for end in (None, "", "x", "nan", "inf", "-1", "10", "4", [], True):
            self.assertEqual(spans(chapter("10", end), chapter("50", "60"))[0], (10.0, 50.0), end)

    def test_the_last_chapter_without_a_usable_end_stops_with_the_file(self):
        self.assertEqual(spans(chapter("10", "x")), [(10.0, 100.0)])
        self.assertEqual(spans({"start_time": "10"}), [(10.0, 100.0)])

    def test_an_end_after_the_file_is_brought_back_to_its_duration(self):
        self.assertEqual(spans(chapter("10", "500")), [(10.0, 100.0)])

    def test_a_chapter_that_starts_at_or_after_the_end_of_the_file_is_dropped(self):
        self.assertEqual(spans(chapter("10", "20"), chapter("100", "110"), chapter("150", "160")), [(10.0, 20.0)])

    def test_the_dropped_chapters_do_not_give_their_start_as_an_end(self):
        self.assertEqual(spans(chapter("10", "x"), chapter("150", "160")), [(10.0, 100.0)])

    def test_without_a_known_duration_the_ends_of_the_file_are_kept_and_a_last_chapter_without_end_is_dropped(self):
        for duration in ("0", "nan", "inf", "-5"):
            self.assertEqual(spans(chapter("10", "500"), chapter("600", "x"), duration=duration), [(10.0, 500.0)], duration)

    def test_a_chapter_at_zero_without_any_end_is_dropped(self):
        self.assertEqual(spans(chapter("0", "x"), duration="0"), [])

    def test_minus_zero_is_written_as_zero(self):
        self.assertEqual(json.dumps(chapters(chapter("-0", "1"))[0]["start"]), "0.0")

    def test_a_bad_title_is_an_empty_title(self):
        for tags in (None, "x", [], {"title": 3}, {"title": None}, {}):
            self.assertEqual(chapters({"start_time": "0", "end_time": "1", "tags": tags})[0]["title"], "", tags)

    def test_a_title_is_on_one_line(self):
        self.assertEqual(chapters(chapter(title="  Une\n  scène\t"))[0]["title"], "Une scène")

    def test_what_is_kept_in_the_database_is_bounded(self):
        many = [chapter(str(i), str(i + 1), "x" * 5000) for i in range(prober.MAX_CHAPTERS + 50)]
        out = chapters(*many, duration="100000")
        self.assertEqual(len(out), prober.MAX_CHAPTERS)
        self.assertEqual({len(c["title"]) for c in out}, {prober.MAX_CHAPTER_TITLE})

    def test_the_last_kept_chapter_still_ends_at_the_next_one(self):
        many = [chapter(str(i), "x") for i in range(prober.MAX_CHAPTERS + 1)]
        self.assertEqual(spans(*many, duration="100000")[-1], (prober.MAX_CHAPTERS - 1.0, float(prober.MAX_CHAPTERS)))

    def test_the_result_is_strict_json(self):
        raw = [chapter("nan", "1"), chapter("1", "inf"), chapter("2", "nan"), chapter("3", "-0")]
        json.dumps(prober.summarize({"format": {"duration": "10"}, "chapters": raw})["chapters"], allow_nan=False)


class TestChapterSkip(unittest.TestCase):
    def test_the_titles_of_an_introduction(self):
        for title in ("Intro", "INTRO", "intro", "Introduction", "L'introduction", "L’intro", "Opening", "Opening Credits", "opening_credits", "OP", "op", "OP 1", "OP1", "OP-2",
                      "Générique de début", "GÉNÉRIQUE DE DÉBUT", "generique de debut", "Générique du début", "Générique-de-début", "Intro - Partie 1", "01. Intro"):
            self.assertEqual(prober.chapter_skip(title), "intro", title)

    def test_the_titles_of_the_credits(self):
        for title in ("Credits", "CREDITS", "End Credits", "End-Credits", "Ending", "ED", "ed", "ED 2", "ED2", "Outro", "Générique", "GÉNÉRIQUE", "generique", "Générique de fin", "Générique-de-fin", "12 - Credits"):
            self.assertEqual(prober.chapter_skip(title), "credits", title)

    def test_the_false_friends(self):
        for title in ("", "Chapitre 1", "Reintroduction", "Réintroduction", "Re-introduction", "Introductions", "Introducing", "Sending", "Pending", "Bending the rules", "Credit Suisse", "Credit", "Accredits",
                      "Discredits", "Reopening", "Pré-générique", "Génériques", "Outros", "Scoop", "Top", "Stop", "OP Art", "Le OP", "OP 1 bis", "Ed Wood", "Red", "Ted", "Mr. Ed", "ED 2 fin", "Edition", "Opera"):
            self.assertIsNone(prober.chapter_skip(title), title)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg absent")
class TestRealChapters(unittest.TestCase):
    def test_the_chapters_of_a_real_file(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        section = "[CHAPTER]\nTIMEBASE=1/1000\nSTART={}\nEND={}\ntitle={}\n"
        (root / "chapters.txt").write_text(";FFMETADATA1\n" + section.format(0, 1000, "Intro") + section.format(1000, 2000, "La scène") + section.format(2000, 3000, "Générique de fin"), encoding="utf-8")
        film = root / "film.mkv"
        subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=10:d=3", "-f", "ffmetadata", "-i", str(root / "chapters.txt"),
                        "-map", "0", "-map_chapters", "1", "-c:v", "mpeg4", str(film)], check=True, capture_output=True, timeout=60)
        out = prober.run_ffprobe(shutil.which("ffprobe"), film)
        self.assertEqual([(c["title"], c["start"], c["end"], c["skip"]) for c in out["chapters"]],
                         [("Intro", 0.0, 1.0, "intro"), ("La scène", 1.0, 2.0, None), ("Générique de fin", 2.0, 3.0, "credits")])
        self.assertEqual(out["probe_version"], prober.PROBE_VERSION)


class TestRunFfprobe(unittest.TestCase):
    def run_with(self, returncode=0, stdout=b"", stderr=b"", ffprobe="ffprobe", **failure):
        done = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
        with mock.patch.object(prober.subprocess, "run", return_value=done, **failure) as run:
            return prober.run_ffprobe(ffprobe, "/films/a.mkv"), run

    def test_the_answer_of_ffprobe_is_summarized(self):
        out, run = self.run_with(stdout=json.dumps({"format": {"format_name": "matroska,webm", "duration": "5"}}).encode("utf-8"))
        self.assertEqual((out["container"], out["duration"]), ("mkv", 5.0))
        self.assertEqual(run.call_args.args[0], ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "-show_chapters", "/films/a.mkv"])
        self.assertEqual(run.call_args.kwargs, {"capture_output": True, "timeout": 60})

    def test_an_empty_answer_is_an_empty_file(self):
        self.assertEqual(self.run_with(stdout=b"")[0]["container"], "")

    def test_without_ffprobe_nothing_is_probed(self):
        with self.assertRaisesRegex(prober.ProbeError, "ffprobe est introuvable"):
            self.run_with(ffprobe="")

    def test_a_failure_says_what_ffprobe_said(self):
        with self.assertRaisesRegex(prober.ProbeError, "^fichier cassé é$"):
            self.run_with(returncode=1, stderr=" fichier cassé é\n".encode("utf-8"))
        with self.assertRaisesRegex(prober.ProbeError, "^ffprobe a échoué$"):
            self.run_with(returncode=1)
        with self.assertRaises(prober.ProbeError) as caught:
            self.run_with(returncode=2, stderr=b"x" * 500)
        self.assertEqual(len(str(caught.exception)), 300)

    def test_a_program_that_cannot_run_is_an_error_too(self):
        for failure in (OSError("pas de programme"), subprocess.TimeoutExpired("ffprobe", 60)):
            with self.assertRaisesRegex(prober.ProbeError, "ffprobe a échoué : "):
                self.run_with(side_effect=failure)


class TestKeyframeAt(unittest.TestCase):
    """D'où part vraiment une image recopiée : la première image que ffprobe lit après s'être placé à l'instant voulu."""

    def lands(self, stdout=b"30.000000,K__\n", returncode=0, at=33.0, ffprobe="ffprobe", **failure):
        done = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=b"")
        with mock.patch.object(prober.subprocess, "run", return_value=done, **failure) as run:
            return prober.keyframe_at(ffprobe, "/films/a.mkv", at), run

    def test_the_stream_starts_on_the_keyframe_ffprobe_lands_on(self):
        start, run = self.lands()
        self.assertEqual(start, 30.0)
        cmd = run.call_args.args[0]
        self.assertEqual((cmd[0], cmd[cmd.index("-read_intervals") + 1], cmd[-1]), ("ffprobe", "33.000%+#1", "/films/a.mkv"))
        self.assertEqual(cmd[cmd.index("-select_streams") + 1], "v:0")

    def test_the_keyframe_is_given_to_the_millisecond(self):
        self.assertEqual(self.lands(stdout=b"29.919958,K__\n")[0], 29.92)

    def test_a_start_at_the_beginning_asks_nothing(self):
        start, run = self.lands(at=0)
        self.assertEqual(start, 0.0)
        run.assert_not_called()
        self.assertEqual(self.lands(at=-4)[0], 0.0)

    def test_without_a_trustworthy_answer_the_start_stays_where_it_was_asked(self):
        for kw in ({"stdout": b""}, {"stdout": b"30.000000,___\n"}, {"stdout": b"N/A,K__\n"}, {"returncode": 1}, {"side_effect": OSError("pas de programme")},
                   {"side_effect": subprocess.TimeoutExpired("ffprobe", 30)}, {"ffprobe": ""}):
            self.assertEqual(self.lands(**kw)[0], 33.0, kw)


class TestPlan(unittest.TestCase):
    def plan(self, probe=None, caps=CAPS, **kw):
        return prober.plan(probe or info(), caps, **kw)

    def test_a_file_the_player_reads_as_it_is_goes_direct(self):
        self.assertEqual(self.plan(), {"mode": "direct", "audio": 0, "why": [], "copy_video": True, "copy_audio": True})

    def test_a_file_without_sound_goes_direct_with_no_track(self):
        self.assertEqual(self.plan(info(tracks=[])), {"mode": "direct", "audio": None, "why": [], "copy_video": True, "copy_audio": True})

    def test_an_image_codec_the_player_does_not_read_is_transcoded(self):
        out = self.plan(info(codec="hevc"))
        self.assertEqual((out["mode"], out["copy_video"], out["copy_audio"]), ("transcode", False, False))
        self.assertEqual(out["why"], ["image hevc non lisible par ce lecteur"])
        self.assertEqual(out["audio"], 0)

    def test_ten_bits_need_the_player_to_say_so_and_hdr_too(self):
        deep = info(codec="hevc", depth=10)
        self.assertEqual(self.plan(deep, {**CAPS, "video": ["hevc"]})["why"], ["image 10 bits / HDR non lisible par ce lecteur"])
        self.assertEqual(self.plan(deep, {**CAPS, "video": ["hevc", "hevc10"]})["mode"], "direct")
        hdr = info(codec="hevc", depth=10, hdr=True)
        self.assertEqual(self.plan(hdr, {**CAPS, "video": ["hevc", "hevc10"]})["mode"], "transcode")
        self.assertEqual(self.plan(hdr, {**CAPS, "video": ["hevc", "hevc10"], "hdr": True})["mode"], "direct")

    def test_an_image_taller_than_asked_is_transcoded(self):
        out = self.plan(max_height=720)
        self.assertEqual((out["mode"], out["why"]), ("transcode", ["image 1080 p au-delà de 720 p"]))
        self.assertEqual(self.plan(caps={**CAPS, "height": 480})["why"], ["image 1080 p au-delà de 480 p"])             # la hauteur du lecteur, faute de demande
        self.assertEqual(self.plan(max_height=1080)["mode"], "direct")
        self.assertEqual(self.plan(caps={**CAPS, "height": 0})["mode"], "direct")                                        # pas de limite

    def test_a_bitrate_above_the_request_is_transcoded_with_five_percent_of_slack(self):
        out = self.plan(max_kbps=3000)
        self.assertEqual((out["mode"], out["why"]), ("transcode", ["débit 4.0 Mb/s au-delà de 3.0 Mb/s demandés"]))
        self.assertEqual(self.plan(max_kbps=3800)["mode"], "transcode")           # 4000 > 3800 × 1,05
        self.assertEqual(self.plan(max_kbps=3850)["mode"], "direct")                  # 4000 ≤ 3850 × 1,05
        self.assertEqual(self.plan(info(bitrate=0), max_kbps=100)["mode"], "direct")        # débit inconnu : on ne peut pas dire qu'il est trop haut
        self.assertEqual(self.plan(info(bitrate=2_100_000), max_kbps=2000)["mode"], "direct")       # pile à la marge : encore lisible

    def test_the_bitrate_reason_is_rounded_to_a_tenth_of_a_megabit(self):
        why = self.plan(info(bitrate=4_300_000), max_kbps=3300)["why"]
        self.assertEqual(why, ["débit 4.3 Mb/s au-delà de 3.3 Mb/s demandés"])

    def test_every_reason_is_listed(self):
        out = self.plan(info(codec="hevc", bitrate=9_000_000), max_kbps=2000, max_height=720)
        self.assertEqual(len(out["why"]), 3)

    def test_a_sound_codec_the_player_does_not_read_is_remuxed(self):
        out = self.plan(info(tracks=[audio(codec="eac3")]))
        self.assertEqual(out, {"mode": "remux", "audio": 0, "why": ["son eac3 non lisible par ce lecteur"], "copy_video": True, "copy_audio": False})

    def test_too_many_channels_are_remuxed(self):
        out = self.plan(info(tracks=[audio(channels=6)]))
        self.assertEqual((out["mode"], out["copy_audio"], out["why"]), ("remux", False, ["son 6 voies, le lecteur en prend 2"]))
        self.assertEqual(self.plan(info(tracks=[audio(channels=6)]), {**CAPS, "channels": 6})["mode"], "direct")
        self.assertEqual(self.plan(info(tracks=[audio(channels=6)]), {**CAPS, "channels": None})["why"], ["son 6 voies, le lecteur en prend 2"])      # 2 par défaut

    def test_a_container_the_player_does_not_read_is_remuxed_with_the_sound_copied(self):
        out = self.plan(info(container="mkv"))
        self.assertEqual(out, {"mode": "remux", "audio": 0, "why": ["conteneur mkv non lu par ce lecteur"], "copy_video": True, "copy_audio": True})
        self.assertEqual(self.plan(info(container="mkv"), {**CAPS, "containers": ["mkv"]})["mode"], "direct")

    def test_the_containers_default_to_mp4_and_webm(self):
        caps = {"video": ["h264"], "audio": ["aac"]}
        self.assertEqual(self.plan(info(container="webm"), caps)["mode"], "direct")
        self.assertEqual(self.plan(info(container="mkv"), caps)["mode"], "remux")

    def test_another_audio_track_than_the_default_is_a_remux(self):
        tracks = [audio(0, "aac", default=True), audio(1, "aac", default=False)]
        out = self.plan(info(tracks=tracks), audio=1)
        self.assertEqual((out["mode"], out["audio"], out["why"]), ("remux", 1, ["autre piste audio que celle par défaut"]))
        self.assertEqual(self.plan(info(tracks=tracks), audio=0)["mode"], "direct")

    def test_an_unknown_track_falls_back_to_the_default_one(self):
        tracks = [audio(0, "aac", default=False), audio(1, "aac", default=True)]
        self.assertEqual((self.plan(info(tracks=tracks), audio=7)["audio"], self.plan(info(tracks=tracks))["audio"]), (1, 1))

    def test_the_default_track_is_not_a_commentary(self):
        tracks = [audio(0, "aac", default=True, commentary=True), audio(1, "aac", default=False)]
        self.assertEqual(prober.default_audio(info(tracks=[*tracks, audio(2, "aac", default=True)]))["index"], 2)
        self.assertEqual(prober.default_audio(info(tracks=tracks))["index"], 0)                                      # aucune piste par défaut qui ne soit un commentaire : la première
        self.assertEqual(prober.default_audio(info(tracks=[audio(0, "aac", default=False)]))["index"], 0)
        self.assertIsNone(prober.default_audio(info(tracks=[])))

    def test_without_capabilities_nothing_is_readable_as_is(self):
        out = prober.plan(info(), None)
        self.assertEqual((out["mode"], out["why"]), ("transcode", ["image h264 non lisible par ce lecteur", "son aac non lisible par ce lecteur"]))

    def test_a_file_with_no_image_is_transcoded_with_no_reason(self):
        out = prober.plan({**info(), "video": None}, CAPS)
        self.assertEqual((out["mode"], out["why"]), ("transcode", []))

    def test_the_two_reasons_of_a_remux_come_together(self):
        tracks = [audio(0, "eac3", default=True), audio(1, "aac", default=False)]
        out = self.plan(info(container="mkv", tracks=tracks), audio=1)
        self.assertEqual(out["why"], ["conteneur mkv non lu par ce lecteur", "autre piste audio que celle par défaut"])


class TestFfmpegCommand(unittest.TestCase):
    def command(self, probe=None, decision=None, **kw):
        decision = decision or {"mode": "direct", "audio": 0, "why": [], "copy_video": True, "copy_audio": True}
        return prober.ffmpeg_command("ffmpeg", "/films/a.mkv", probe or info(), decision, **kw)

    def after(self, cmd, flag):
        return cmd[cmd.index(flag) + 1]

    TRANSCODE = {"mode": "transcode", "audio": 0, "why": [], "copy_video": False, "copy_audio": False}

    def test_a_copy_goes_to_a_fragmented_mp4_on_the_standard_output(self):
        cmd = self.command()
        self.assertEqual(cmd[:4], ["ffmpeg", "-v", "error", "-nostdin"])
        self.assertEqual(cmd[-5:], ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"])
        self.assertEqual((self.after(cmd, "-c:v"), self.after(cmd, "-c:a")), ("copy", "copy"))
        self.assertNotIn("-ss", cmd)
        self.assertEqual((self.after(cmd, "-i"), self.after(cmd, "-map")), ("/films/a.mkv", "0:v:0"))
        self.assertIn("0:a:0", cmd)

    def test_the_start_is_given_to_the_input_in_seconds(self):
        cmd = self.command(decision=self.TRANSCODE, start=12.5)
        self.assertLess(cmd.index("-ss"), cmd.index("-i"))
        self.assertEqual(self.after(cmd, "-ss"), "12.500")                 # l'image ré-encodée commence à l'instant demandé
        self.assertNotIn("-ss", self.command(start=-3))

    def test_a_copied_image_is_asked_a_little_after_its_keyframe(self):
        """`start` d'une image recopiée est une image clé ; ffmpeg retire parfois 0,13 s à l'instant demandé avant de chercher (MKV avec images B) et partirait de l'image clé d'avant."""
        self.assertEqual(self.after(self.command(start=12.5), "-ss"), "12.750")
        self.assertEqual(self.after(self.command(decision={**self.TRANSCODE, "copy_video": True}, start=12.5), "-ss"), "12.750")
        self.assertNotIn("-ss", self.command(start=0))                      # pas de départ, pas de marge

    def test_a_copied_hevc_is_tagged_for_apple_players(self):
        self.assertEqual(self.after(self.command(info(codec="hevc")), "-tag:v"), "hvc1")
        self.assertNotIn("-tag:v", self.command())

    def test_the_audio_track_asked_for_is_the_one_mapped(self):
        self.assertIn("0:a:2", self.command(decision={"mode": "remux", "audio": 2, "why": [], "copy_video": True, "copy_audio": True}))

    def test_without_a_track_there_is_no_sound_at_all(self):
        cmd = self.command(decision={"mode": "direct", "audio": None, "why": [], "copy_video": True, "copy_audio": True})
        self.assertFalse([part for part in cmd if part.startswith("0:a") or part == "-c:a"])

    def test_a_transcode_encodes_in_h264_8_bits_and_scales_down_only(self):
        cmd = self.command(decision=self.TRANSCODE, max_height=720)
        self.assertEqual((self.after(cmd, "-c:v"), self.after(cmd, "-preset"), self.after(cmd, "-vf")), ("libx264", "veryfast", "scale=-2:720,format=yuv420p"))
        self.assertEqual(self.after(self.command(decision=self.TRANSCODE, max_height=2160), "-vf"), "format=yuv420p")         # on n'agrandit pas
        self.assertEqual(self.after(self.command(decision=self.TRANSCODE), "-vf"), "format=yuv420p")
        self.assertEqual(self.after(self.command(decision=self.TRANSCODE, preset="slow"), "-preset"), "slow")

    def test_a_transcode_of_unknown_size_has_no_scaling(self):
        self.assertEqual(self.after(self.command(info(height=0), decision=self.TRANSCODE, max_height=720), "-vf"), "format=yuv420p")

    def test_the_bitrate_cap_leaves_room_for_the_sound(self):
        cmd = self.command(decision=self.TRANSCODE, max_kbps=4000)
        self.assertEqual((self.after(cmd, "-maxrate"), self.after(cmd, "-bufsize")), ("3680k", "8000k"))
        self.assertNotIn("-maxrate", self.command(decision=self.TRANSCODE))

    def test_transcoded_sound_is_aac_with_at_most_the_channels_of_the_source_and_the_player(self):
        for source, wanted, rate in ((6, 2, "224k"), (6, 6, "480k"), (1, 2, "160k"), (2, 2, "224k")):
            cmd = self.command(info(tracks=[audio(channels=source)]), decision={**self.TRANSCODE, "copy_video": True}, channels=wanted)
            self.assertEqual((self.after(cmd, "-c:a"), self.after(cmd, "-b:a"), self.after(cmd, "-ac")), ("aac", rate, str(min(source, wanted))), (source, wanted))

    def test_the_sound_always_has_one_channel_at_least(self):
        cmd = self.command(decision={**self.TRANSCODE, "copy_video": True}, channels=0)
        self.assertEqual((self.after(cmd, "-ac"), self.after(cmd, "-b:a")), ("1", "160k"))

    def test_a_start_at_zero_is_no_start(self):
        self.assertNotIn("-ss", self.command(start=0))
        self.assertEqual(self.after(self.command(decision=self.TRANSCODE, start=0.5), "-ss"), "0.500")

    def test_a_probe_without_video_height_is_not_scaled(self):
        probe = info()
        del probe["video"]["height"]
        self.assertEqual(self.after(self.command(probe, decision=self.TRANSCODE, max_height=720), "-vf"), "format=yuv420p")

    def test_a_track_missing_from_the_probe_is_taken_as_stereo(self):
        cmd = self.command(info(tracks=[]), decision={**self.TRANSCODE, "audio": 3, "copy_video": True}, channels=6)
        self.assertEqual((self.after(cmd, "-ac"), self.after(cmd, "-b:a")), ("2", "224k"))


if __name__ == "__main__":
    unittest.main()
