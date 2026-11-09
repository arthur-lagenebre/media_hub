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

    def test_a_library_is_found_by_its_id(self):
        cfg = config.Config(libraries=[config.Library("a", "A", "movies", self.tmp), config.Library("b", "B", "books", self.tmp)], data=self.tmp, repo=None)
        self.assertEqual(cfg.library("b").label, "B")
        self.assertIsNone(cfg.library("c"))


class TestFindRepo(TempCase):
    def test_the_repo_named_by_the_environment_is_used_when_it_holds_libraries(self):
        repo = self.tmp / "ml"
        (repo / "libraries").mkdir(parents=True)
        self.assertEqual(config._find_repo({"HUB_REPO": str(repo)}), repo)

    def test_a_repo_without_libraries_is_skipped(self):
        repo = self.tmp / "ml"
        repo.mkdir()
        self.assertNotEqual(config._find_repo({"HUB_REPO": str(repo)}), repo)

    def test_nothing_is_required_to_look_for_it(self):
        found = config._find_repo({})
        self.assertTrue(found is None or (found / "libraries").is_dir())


class TestDotenvKey(TempCase):
    def write(self, folder, text):
        (folder).mkdir(parents=True, exist_ok=True)
        (folder / ".env").write_text(text, encoding="utf-8")

    def test_without_a_repo_there_is_no_key(self):
        self.assertEqual(config._read_dotenv_key(None), "")

    def test_the_key_is_read_without_its_quotes_from_the_repo(self):
        repo = self.tmp / "ml"
        self.write(repo, "AUTRE=1\nTMDB_KEY_X=non\n TMDB_KEY = 'abc' \n")
        self.assertEqual(config._read_dotenv_key(repo), "abc")
        self.write(repo, 'TMDB_KEY="déf"\n')
        self.assertEqual(config._read_dotenv_key(repo), "déf")

    def test_an_empty_key_is_skipped_and_the_parent_folder_is_the_next_place(self):
        repo = self.tmp / "ml"
        self.write(repo, "TMDB_KEY=\nTMDB_KEY=  \n")
        self.write(self.tmp, "TMDB_KEY=parent\n")
        self.assertEqual(config._read_dotenv_key(repo), "parent")

    def test_no_file_no_key(self):
        repo = self.tmp / "ml"
        repo.mkdir()
        self.assertEqual(config._read_dotenv_key(repo), "")


class TestLoad(TempCase):
    def load(self, **env):
        base = {"HUB_DATA": str(self.tmp / "data"), "HUB_SRV": str(self.tmp / "srv")}
        with mock.patch("hub.config.shutil.which", return_value=None):
            return config.load({**base, **env})

    def test_the_defaults(self):
        cfg = self.load()
        self.assertEqual((cfg.bind, cfg.port, cfg.password, cfg.admin, cfg.max_streams, cfg.autoscan_hours, cfg.ffmpeg, cfg.ffprobe),
                         ("127.0.0.1", 8080, "", "admin", 2, 0.0, "", ""))
        self.assertEqual(cfg.data, (self.tmp / "data").resolve())
        self.assertEqual(cfg.libraries, [])

    def test_every_setting_can_be_given(self):
        cfg = self.load(HUB_BIND="0.0.0.0", HUB_PORT="9000", HUB_PASSWORD="secret", HUB_ADMIN=" chef ", HUB_MAX_STREAMS="5", HUB_AUTOSCAN_HOURS="1.5", TMDB_KEY="clé")
        self.assertEqual((cfg.bind, cfg.port, cfg.password, cfg.admin, cfg.max_streams, cfg.autoscan_hours, cfg.tmdb_key),
                         ("0.0.0.0", 9000, "secret", "chef", 5, 1.5, "clé"))

    def test_an_empty_admin_name_falls_back_to_admin(self):
        self.assertEqual(self.load(HUB_ADMIN="  ").admin, "admin")

    def test_there_is_always_at_least_one_stream(self):
        self.assertEqual(self.load(HUB_MAX_STREAMS="0").max_streams, 1)
        self.assertEqual(self.load(HUB_MAX_STREAMS="-3").max_streams, 1)

    def test_the_inbox_limits_default_to_safe_values_in_bytes(self):
        cfg = self.load()
        self.assertEqual((cfg.inbox_keep_free, cfg.inbox_max_uploads), (10240 << 20, 2))

    def test_the_inbox_limits_are_given_in_megabytes(self):
        cfg = self.load(HUB_INBOX_KEEP_FREE="50", HUB_INBOX_MAX_UPLOADS="4")
        self.assertEqual((cfg.inbox_keep_free, cfg.inbox_max_uploads), (50 << 20, 4))

    def test_the_inbox_always_takes_at_least_one_upload_and_keeps_a_non_negative_space(self):
        cfg = self.load(HUB_INBOX_KEEP_FREE="-1", HUB_INBOX_MAX_UPLOADS="0")
        self.assertEqual((cfg.inbox_keep_free, cfg.inbox_max_uploads), (0, 1))

    def test_the_tmdb_key_comes_from_the_environment_before_the_dotenv_file(self):
        repo = self.tmp / "ml"
        (repo / "libraries").mkdir(parents=True)
        (repo / ".env").write_text("TMDB_KEY=fichier\n", encoding="utf-8")
        self.assertEqual(self.load(HUB_REPO=str(repo)).tmdb_key, "fichier")
        self.assertEqual(self.load(HUB_REPO=str(repo), TMDB_KEY="env").tmdb_key, "env")


if __name__ == "__main__":
    unittest.main()
