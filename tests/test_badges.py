"""Les badges d'une vidéo : qualité d'image, HDR, langues du son et des sous-titres, tirés du résumé ffprobe ; et ceux d'une saison, tirés de ses épisodes. Fonctions pures."""

from __future__ import annotations

import unittest

from hub.media import badges


def track(lang, **over):
    return {"lang": lang, "commentary": False, "forced": False, **over}


def probe(width=1920, height=1080, hdr=False, audio=("fre",), subs=()):
    return {"video": {"width": width, "height": height, "hdr": hdr}, "audio": [t if isinstance(t, dict) else track(t) for t in audio],
            "subs": [t if isinstance(t, dict) else track(t) for t in subs]}


def badge(quality="fullhd", hdr=False, audio=("fr",), subs=()):
    return {"quality": quality, "hdr": hdr, "audio": list(audio), "subs": list(subs)}


class TestQuality(unittest.TestCase):
    def test_the_usual_sizes(self):
        sizes = ((3840, 2160), (1920, 1080), (1280, 720), (720, 576), (0, 0))
        self.assertEqual([badges.quality(w, h) for w, h in sizes], ["4k", "fullhd", "hd", "sd", "sd"])

    def test_a_wide_film_is_classed_by_its_width(self):
        sizes = ((3840, 1600), (1920, 800), (1280, 536))
        self.assertEqual([badges.quality(w, h) for w, h in sizes], ["4k", "fullhd", "hd"])

    def test_a_narrow_film_is_classed_by_its_height(self):
        sizes = ((2880, 2160), (1440, 1080), (960, 720))
        self.assertEqual([badges.quality(w, h) for w, h in sizes], ["4k", "fullhd", "hd"])

    def test_the_thresholds_are_included(self):
        at = [badges.quality(w, h) for w, h in ((3200, 0), (0, 1800), (1800, 0), (0, 1000), (1200, 0), (0, 700))]
        under = [badges.quality(w, h) for w, h in ((3199, 1799), (1799, 999), (1199, 699))]
        self.assertEqual(at, ["4k", "4k", "fullhd", "fullhd", "hd", "hd"])
        self.assertEqual(under, ["fullhd", "hd", "sd"])


class TestLangCode(unittest.TestCase):
    def test_three_letters_become_two(self):
        codes = ("fre", "fra", "eng", "ger", "deu", "jpn", "spa", "ita", "chi", "zho", "dut", "nld")
        self.assertEqual([badges.lang_code(c) for c in codes], ["fr", "fr", "en", "de", "de", "ja", "es", "it", "zh", "zh", "nl", "nl"])

    def test_two_letters_stay_in_lower_case(self):
        self.assertEqual([badges.lang_code(c) for c in ("fr", "EN", " Ja ", "FRE")], ["fr", "en", "ja", "fr"])

    def test_a_region_is_dropped(self):
        self.assertEqual([badges.lang_code(c) for c in ("fr-FR", "pt_BR", "eng-US")], ["fr", "pt", "en"])

    def test_no_language_or_an_unknown_one_is_nothing(self):
        self.assertEqual([badges.lang_code(c) for c in ("", None, "und", "UND", "zxx", "mul", "qaa", "français", "f1")], [""] * 9)


class TestOfProbe(unittest.TestCase):
    def test_a_film(self):
        out = badges.of_probe(probe(3840, 1600, True, audio=("fre", "eng"), subs=("fre", "eng")))
        self.assertEqual(out, {"quality": "4k", "hdr": True, "audio": ["fr", "en"], "subs": ["fr", "en"]})

    def test_the_languages_keep_the_order_of_the_tracks_once_each(self):
        out = badges.of_probe(probe(audio=("eng", "fre", "en", "fra", "jpn"), subs=("jpn", "fre", "jpn")))
        self.assertEqual((out["audio"], out["subs"]), (["en", "fr", "ja"], ["ja", "fr"]))

    def test_a_commentary_is_not_a_language_of_the_film(self):
        out = badges.of_probe(probe(audio=("fre", track("eng", commentary=True))))
        self.assertEqual(out["audio"], ["fr"])

    def test_forced_subtitles_do_not_count(self):
        out = badges.of_probe(probe(subs=(track("fre", forced=True), "eng")))
        self.assertEqual(out["subs"], ["en"])

    def test_a_track_without_a_language_is_left_out(self):
        out = badges.of_probe(probe(audio=("", "und", "fre"), subs=("und", "")))
        self.assertEqual((out["audio"], out["subs"]), (["fr"], []))

    def test_the_files_beside_the_video_count_after_its_own_tracks(self):
        side = [{"lang": "en", "forced": False}, {"lang": "fr", "forced": True}, {"lang": "spa", "forced": False}, {"lang": "", "forced": False}]
        out = badges.of_probe(probe(subs=("fre",)), side)
        self.assertEqual(out["subs"], ["fr", "en", "es"])

    def test_a_file_without_an_image_has_no_badge(self):
        self.assertEqual(badges.of_probe({"video": None, "audio": [track("fre")], "subs": []}), {})


class TestOfSeason(unittest.TestCase):
    def test_no_probed_episode_gives_nothing(self):
        self.assertEqual(badges.of_season([]), {})

    def test_the_quality_is_the_one_of_most_episodes(self):
        out = badges.of_season([badge("hd"), badge("fullhd"), badge("hd")])
        self.assertEqual(out["quality"], "hd")

    def test_a_tie_goes_to_the_better_quality(self):
        self.assertEqual(badges.of_season([badge("sd"), badge("4k"), badge("hd"), badge("fullhd")])["quality"], "4k")
        self.assertEqual(badges.of_season([badge("hd"), badge("sd")])["quality"], "hd")

    def test_hdr_only_when_every_episode_is(self):
        self.assertTrue(badges.of_season([badge(hdr=True), badge(hdr=True)])["hdr"])
        self.assertFalse(badges.of_season([badge(hdr=True), badge(hdr=False)])["hdr"])

    def test_the_languages_are_those_of_every_episode_in_the_order_of_the_first(self):
        out = badges.of_season([badge(audio=("ja", "fr", "en"), subs=("fr", "en")), badge(audio=("fr", "ja"), subs=("en",)), badge(audio=("fr", "ja", "de"), subs=("en", "fr"))])
        self.assertEqual((out["audio"], out["subs"]), (["ja", "fr"], ["en"]))

    def test_one_episode_is_its_own_season(self):
        self.assertEqual(badges.of_season([badge("4k", True, ("fr",), ("en",))]), badge("4k", True, ("fr",), ("en",)))


if __name__ == "__main__":
    unittest.main()
