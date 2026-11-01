"""Comment jouer un fichier : le résumé de ffprobe, la décision (direct, remux, transcodage) et la ligne de commande de ffmpeg. Fonctions pures, essayées sur des fichiers décrits à la main."""

from __future__ import annotations

import unittest

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
        self.assertEqual(prober.summarize({}), {"container": "", "duration": 0.0, "bitrate": 0, "size": 0, "video": None, "audio": [], "subs": []})

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

    def test_the_frame_rate(self):
        self.assertEqual([prober._fps(t) for t in ("24000/1001", "25", "0/0", "x/y", "", None)], [23.976, 25.0, 0, 0, 0.0, 0.0])


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
        cmd = self.command(start=12.5)
        self.assertLess(cmd.index("-ss"), cmd.index("-i"))
        self.assertEqual(self.after(cmd, "-ss"), "12.500")
        self.assertNotIn("-ss", self.command(start=-3))

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

    def test_a_track_missing_from_the_probe_is_taken_as_stereo(self):
        cmd = self.command(info(tracks=[]), decision={**self.TRANSCODE, "audio": 3, "copy_video": True}, channels=6)
        self.assertEqual((self.after(cmd, "-ac"), self.after(cmd, "-b:a")), ("2", "224k"))


if __name__ == "__main__":
    unittest.main()
