"""Les sous-titres posés à côté d'une vidéo : lesquels, et comment on les nomme."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
