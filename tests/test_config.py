"""Les réglages : où l'on cherche ffmpeg, et d'où viennent les bibliothèques (HUB_LIBRARIES, sinon les dossiers connus sous HUB_SRV)."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import config


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def touch(self, *parts):
        path = self.tmp.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        return path


class TestFindTool(TempCase):
    def find(self, explicit=None, on_path=None):
        env = {"HUB_FFMPEG": str(explicit)} if explicit else {}
        with mock.patch("hub.config.shutil.which", return_value=on_path):
            return config._find_tool("ffmpeg", env, self.tmp / "data")

    def test_nothing_found_is_an_empty_string(self):
        self.assertEqual(self.find(), "")

    def test_the_path_comes_first_without_an_explicit_setting(self):
        self.touch("data", "bin", "ffmpeg")
        self.assertEqual(self.find(on_path="/usr/bin/ffmpeg"), "/usr/bin/ffmpeg")

    def test_the_data_folder_is_the_last_resort(self):
        found = self.touch("data", "bin", "ffmpeg")
        self.assertEqual(self.find(), str(found))
        found.unlink()
        exe = self.touch("data", "bin", "ffmpeg.exe")
        self.assertEqual(self.find(), str(exe))

    def test_an_explicit_file_named_like_the_tool_wins_over_the_path(self):
        tool = self.touch("outils", "ffmpeg-7")
        self.assertEqual(self.find(explicit=tool, on_path="/usr/bin/ffmpeg"), str(tool))

    def test_an_explicit_folder_holds_the_tool_with_or_without_exe(self):
        tool = self.touch("outils", "ffmpeg")
        self.assertEqual(self.find(explicit=tool.parent, on_path="/usr/bin/ffmpeg"), str(tool))
        tool.unlink()
        exe = self.touch("outils", "ffmpeg.exe")
        self.assertEqual(self.find(explicit=exe.parent), str(exe))

    def test_an_explicit_file_of_the_other_tool_leads_to_its_sibling(self):
        probe = self.touch("outils", "ffprobe.exe")
        sibling = self.touch("outils", "ffmpeg.exe")
        self.assertEqual(config._find_tool("ffmpeg", {"HUB_FFMPEG": str(probe)}, self.tmp), str(sibling))

    def test_an_explicit_setting_that_leads_nowhere_falls_back_to_the_path(self):
        self.assertEqual(self.find(explicit=self.tmp / "absent", on_path="/usr/bin/ffmpeg"), "/usr/bin/ffmpeg")
        lone = self.touch("seul", "ffprobe")                                       # son frère ffmpeg n'existe pas
        self.assertEqual(self.find(explicit=lone, on_path="/usr/bin/ffmpeg"), "/usr/bin/ffmpeg")


class TestLibraries(TempCase):
    def test_the_known_folders_become_libraries_in_the_default_order(self):
        for name in ("Livres", "Films", "TV_Shows", "Autre"):
            (self.tmp / name).mkdir()
        (self.tmp / "Musique").write_bytes(b"")                                    # un fichier n'est pas une bibliothèque
        found = config._libraries({}, self.tmp)
        self.assertEqual([(lib.id, lib.label, lib.kind, lib.path.name) for lib in found],
                         [("films", "Films", "movies", "Films"), ("series", "Séries", "shows", "TV_Shows"), ("livres", "Livres", "books", "Livres")])

    def test_folder_names_are_matched_without_regard_to_case(self):
        (self.tmp / "FILMS").mkdir()
        self.assertEqual([lib.path.name for lib in config._libraries({}, self.tmp)], ["FILMS"])

    def test_the_first_known_name_of_a_library_wins(self):
        (self.tmp / "Movies").mkdir()
        (self.tmp / "Films").mkdir()
        self.assertEqual([lib.path.name for lib in config._libraries({}, self.tmp)], ["Films"])

    def test_a_missing_root_has_no_libraries(self):
        self.assertEqual(config._libraries({}, self.tmp / "absent"), [])

    def describe(self, content):
        path = self.tmp / "libraries.json"
        path.write_text(json.dumps(content), encoding="utf-8")
        return {"HUB_LIBRARIES": str(path)}

    def test_a_file_describes_the_libraries_as_a_list_or_under_a_key(self):
        items = [{"id": "docs", "kind": "books", "path": "/x/docs"}, {"id": "ciné", "label": "Cinéma", "kind": "movies", "path": "/x/cine"}]
        for content in (items, {"libraries": items}):
            found = config._libraries(self.describe(content), self.tmp / "ignoré")
            self.assertEqual([(lib.id, lib.label, lib.kind, lib.path) for lib in found],
                             [("docs", "docs", "books", Path("/x/docs")), ("ciné", "Cinéma", "movies", Path("/x/cine"))])

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaisesRegex(ValueError, "genre de bibliothèque inconnu"):
            config._libraries(self.describe([{"id": "x", "kind": "jeux", "path": "/x"}]), self.tmp)


if __name__ == "__main__":
    unittest.main()
