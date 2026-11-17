"""Le choix d'office des pistes d'après les réglages d'un compte : la piste audio (avant le plan de lecture) et un sous-titre. Fonctions pures."""

from __future__ import annotations

import unittest

from hub.media import probe as prober
from hub.media import tracks


def audio(index, lang, default=False, commentary=False, codec="aac", channels=2):
    return {"index": index, "lang": lang, "default": default, "commentary": commentary, "codec": codec, "channels": channels, "title": ""}


def sub(ident, lang, forced=False, text=True, image="", default=False):
    return {"id": ident, "lang": lang, "forced": forced, "text": text, "image": image, "default": default}


CAPS = {"video": ["h264"], "audio": ["aac"], "containers": ["mp4"], "channels": 2}


def film(*tracks_):
    return {"container": "mp4", "bitrate": 0, "video": {"codec": "h264", "width": 1920, "height": 1080, "depth": 8, "hdr": False}, "audio": list(tracks_), "subs": []}


class TestPickAudio(unittest.TestCase):
    def test_without_a_preference_the_file_decides(self):
        self.assertIsNone(tracks.pick_audio([audio(0, "fre", default=True), audio(1, "eng")], []))

    def test_the_first_preferred_language_found_wins(self):
        found = [audio(0, "fre", default=True), audio(1, "eng"), audio(2, "jpn")]
        self.assertEqual(tracks.pick_audio(found, ["ja", "en"]), 2)
        self.assertEqual(tracks.pick_audio(found, ["de", "en"]), 1)

    def test_a_commentary_is_never_picked(self):
        self.assertIsNone(tracks.pick_audio([audio(0, "fre", default=True), audio(1, "eng", commentary=True)], ["en"]))
        self.assertEqual(tracks.pick_audio([audio(0, "eng", commentary=True), audio(1, "eng")], ["en"]), 1)

    def test_among_tracks_of_one_language_the_default_one_comes_first(self):
        self.assertEqual(tracks.pick_audio([audio(0, "fre"), audio(1, "fra", default=True)], ["fr"]), 1)

    def test_a_language_that_is_not_there_or_unknown_picks_nothing(self):
        self.assertIsNone(tracks.pick_audio([audio(0, "und", default=True), audio(1, ""), audio(2, "zxx")], ["de"]))
        self.assertIsNone(tracks.pick_audio([], ["fr"]))


class TestPickAudioAndPlan(unittest.TestCase):
    """Le plan de lecture garde la lecture directe tant que la préférence ne demande pas une autre piste que celle par défaut."""

    def plan_for(self, langs):
        probe = film(audio(0, "fre", default=True), audio(1, "eng"))
        return prober.plan(probe, CAPS, tracks.pick_audio(probe["audio"], langs))

    def test_no_preference_keeps_the_direct_play(self):
        self.assertEqual((self.plan_for([])["mode"], self.plan_for([])["audio"]), ("direct", 0))

    def test_a_preference_for_the_default_language_keeps_the_direct_play(self):
        self.assertEqual(self.plan_for(["fr", "en"])["mode"], "direct")

    def test_a_preference_for_another_language_costs_a_remux(self):
        decision = self.plan_for(["en"])
        self.assertEqual((decision["mode"], decision["audio"]), ("remux", 1))


class TestPickSub(unittest.TestCase):
    LIST = [sub("e0", "fre", forced=True), sub("e1", "fre"), sub("e2", "eng", forced=True), sub("e3", "eng"), sub("s0", "es")]

    def pick(self, mode, langs, audio_lang="eng", understood=(), found=None):
        return tracks.pick_sub(self.LIST if found is None else found, mode, langs, audio_lang, understood)

    def test_never_picks_nothing(self):
        self.assertIsNone(self.pick("never", ["fr"]))

    def test_forced_picks_the_forced_track_of_the_first_language_that_has_one(self):
        self.assertEqual(self.pick("forced", ["fr", "en"]), "e0")
        self.assertEqual(self.pick("forced", ["es", "en"]), "e2")
        self.assertIsNone(self.pick("forced", ["es"]))                                      # des complets en espagnol, mais pas de forcés

    def test_always_picks_full_subtitles_and_else_forced_ones(self):
        self.assertEqual(self.pick("always", ["fr", "en"]), "e1")
        self.assertEqual(self.pick("always", ["de", "es"]), "s0")
        self.assertEqual(self.pick("always", ["fr"], found=[sub("e0", "fre", forced=True), sub("e3", "eng")]), "e0")

    def test_foreign_picks_full_subtitles_only_when_the_audio_is_not_understood(self):
        self.assertEqual(self.pick("foreign", ["fr"], audio_lang="eng"), "e1")
        self.assertEqual(self.pick("foreign", ["fr"], audio_lang="fre"), "e0")             # le son est compris : les forcés seulement
        self.assertEqual(self.pick("foreign", ["fr"], audio_lang="eng", understood=["en"]), "e0")   # une langue audio préférée est comprise

    def test_foreign_with_an_audio_of_unknown_language_shows_only_forced_ones(self):
        self.assertEqual(self.pick("foreign", ["fr"], audio_lang="und"), "e0")
        self.assertEqual(self.pick("foreign", ["fr"], audio_lang=""), "e0")

    def test_an_image_or_dvd_track_is_never_picked(self):
        found = [sub("e0", "fre", forced=True, text=False, image="pgs"), sub("e1", "fre", text=False, image="pgs"), sub("e2", "fre", text=False)]
        for mode in ("forced", "foreign", "always"):
            self.assertIsNone(self.pick(mode, ["fr"], found=found), mode)

    def test_a_track_without_a_known_language_is_never_picked(self):
        found = [sub("e0", "und", forced=True), sub("e1", ""), sub("e2", "zxx")]
        for mode in ("forced", "always"):
            self.assertIsNone(self.pick(mode, ["fr"], found=found), mode)
        self.assertIsNone(self.pick("always", [], found=self.LIST))                          # aucune langue : aucun choix

    def test_among_tracks_of_one_kind_and_language_the_default_one_comes_first(self):
        found = [sub("e0", "fre"), sub("e1", "fre", default=True), sub("s0", "fr", forced=True), sub("s1", "fr", forced=True, default=True)]
        self.assertEqual((self.pick("always", ["fr"], found=found), self.pick("forced", ["fr"], found=found)), ("e1", "s1"))

    def test_an_unknown_mode_picks_nothing(self):
        self.assertIsNone(self.pick("parfois", ["fr"]))


if __name__ == "__main__":
    unittest.main()
