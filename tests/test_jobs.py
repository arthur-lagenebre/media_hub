"""Les tâches de fond : une file, une tâche à la fois, jamais deux fois la même en attente ; le nettoyage des fichiers parasites et ce qu'il dit quand le volume est en lecture seule."""

from __future__ import annotations

import errno
import shutil
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from tests import fixtures
from hub import config

fixtures.REPO  # noqa: B018  (rend `libraries` importable)
config.use_media_library(fixtures.REPO)

from hub.server import jobs  # noqa: E402


class Waiting:
    """Une tâche qui attend qu'on la libère : de quoi tenir la file occupée le temps d'un test."""

    def __init__(self):
        self.go, self.started = threading.Event(), threading.Event()

    def __call__(self, log):
        self.started.set()
        self.go.wait(10)
        return "fini"


class JobsCase(unittest.TestCase):
    def setUp(self):
        self.jobs = jobs.Jobs(types.SimpleNamespace(libraries=[]), None)

    def wait(self, job, status="done"):
        for _ in range(500):
            if job.status == status:
                return
            threading.Event().wait(0.01)
        self.fail(f"{job.label} : {job.status}")


class TestJob(unittest.TestCase):
    def test_a_job_waits_then_reports_what_it_did(self):
        job = jobs.Job(3, "Étiquette", None)
        job.line("une ligne")
        self.assertEqual(job.status, "waiting")
        out = job.public()
        self.assertEqual((out["id"], out["label"], out["status"], out["started"], out["ended"], out["result"]), (3, "Étiquette", "waiting", None, None, None))
        self.assertIsInstance(out["queued"], int)
        self.assertNotIn("log", out)

    def test_the_log_comes_only_when_asked(self):
        job = jobs.Job(1, "x", None)
        job.line("a")
        job.line("b")
        self.assertEqual(job.public(with_log=True)["log"], ["a", "b"])
        self.assertNotIn("log", job.public(with_log=False))

    def test_the_times_are_whole_seconds(self):
        job = jobs.Job(1, "x", None)
        job.started, job.ended = 10.9, 12.2
        out = job.public()
        self.assertEqual((out["started"], out["ended"]), (10, 12))

    def test_the_log_keeps_the_last_lines_only(self):
        job = jobs.Job(1, "x", None)
        for n in range(jobs.LOG_LINES + 5):
            job.line(str(n))
        self.assertEqual((len(job.log), job.log[0]), (jobs.LOG_LINES, "5"))


class TestQueue(JobsCase):
    def test_the_first_job_is_number_one_and_the_next_one_number_two(self):
        first = self.jobs._submit("a", lambda log: 1)
        second = self.jobs._submit("b", lambda log: 2)
        self.assertEqual((first.id, second.id), (1, 2))
        self.wait(second)
        self.assertEqual((first.result, second.result, first.status), (1, 2, "done"))
        self.assertIsNotNone(first.started)
        self.assertIsNotNone(first.ended)

    def test_a_job_that_fails_says_so_and_does_not_stop_the_next(self):
        def boom(log):
            raise ValueError("oups")

        bad = self.jobs._submit("mauvaise", boom)
        good = self.jobs._submit("bonne", lambda log: "ok")
        self.wait(good)
        self.assertEqual((bad.status, list(bad.log), good.result), ("failed", ["ERREUR : oups"], "ok"))

    def test_the_function_of_a_job_receives_the_way_to_write_in_its_log(self):
        job = self.jobs._submit("journal", lambda log: log("bonjour"))
        self.wait(job)
        self.assertEqual(list(job.log), ["bonjour"])

    def test_the_same_job_is_not_queued_twice_but_another_one_is(self):
        wait = Waiting()
        first = self.jobs._submit("a", wait)
        wait.started.wait(10)
        self.assertIs(self.jobs._submit("a", lambda log: None), first)                        # déjà en cours
        other = self.jobs._submit("b", lambda log: None)
        self.assertIsNot(other, first)
        self.assertIs(self.jobs._submit("b", lambda log: None), other)                        # déjà en attente
        self.assertIs(self.jobs._submit("a", lambda log: None), first)                        # même derrière une autre tâche plus récente
        self.assertEqual((first.id, other.id), (1, 2))
        wait.go.set()
        self.wait(other)
        again = self.jobs._submit("a", lambda log: None)                                      # fini : on peut la relancer
        self.assertIsNot(again, first)
        self.assertEqual(again.id, 3)

    def test_busy_while_something_runs_or_waits(self):
        self.assertFalse(self.jobs.busy())
        wait = Waiting()
        job = self.jobs._submit("a", wait)
        wait.started.wait(10)
        self.assertTrue(self.jobs.busy())
        wait.go.set()
        self.wait(job)
        self.assertFalse(self.jobs.busy())

    def test_the_snapshot_lists_the_newest_first_without_the_logs_unless_asked(self):
        a = self.jobs._submit("a", lambda log: log("x"))
        b = self.jobs._submit("b", lambda log: None)
        self.wait(b)
        self.wait(a)
        self.assertEqual([j["label"] for j in self.jobs.snapshot()], ["b", "a"])
        self.assertTrue(all("log" not in j for j in self.jobs.snapshot()))
        self.assertEqual([j["log"] for j in self.jobs.snapshot(True)], [[], ["x"]])

    def test_only_the_last_thirty_jobs_are_remembered(self):
        for n in range(32):
            job = self.jobs._submit(f"t{n}", lambda log: None)
        self.wait(job)
        self.assertEqual(len(self.jobs.snapshot()), 30)


class TestTasks(JobsCase):
    lib = types.SimpleNamespace(label="Films", id="films", path=Path("."), kind="movies")

    def test_the_enrichments_ask_for_the_force_only_when_told_to(self):
        for task, target in (("enrich", "enrich_shows"), ("enrich_sagas", "enrich_sagas"), ("enrich_artists", "enrich_artists")):
            for force in (None, True):
                with mock.patch.object(jobs.enrich, target, return_value="ok") as called:
                    job = getattr(self.jobs, task)(self.lib) if force is None else getattr(self.jobs, task)(self.lib, force)
                    self.wait(job)
                self.assertIs(called.call_args.args[-1], bool(force), (task, force))

    def test_every_task_has_its_own_label(self):
        labels = set()
        with mock.patch.object(jobs.enrich, "enrich_shows"), mock.patch.object(jobs.enrich, "enrich_sagas"), mock.patch.object(jobs.enrich, "enrich_artists"), \
                mock.patch.object(jobs.scan, "scan_library", return_value={}), mock.patch.object(jobs, "clean_junk", return_value={}):
            for job in (self.jobs.scan(self.lib), self.jobs.scan_all(), self.jobs.enrich(self.lib), self.jobs.enrich_sagas(self.lib), self.jobs.enrich_artists(self.lib),
                        self.jobs.cleanup(False), self.jobs.cleanup(True)):
                self.wait(job)
                labels.add(job.label)
        self.assertEqual(labels, {"Mise à jour · Films", "Mise à jour · tout", "Enrichissement TMDB · Films", "Sagas TMDB · Films", "Fiches artistes · Films",
                                  "Nettoyage · fichiers parasites (simulation)", "Nettoyage · fichiers parasites"})

    def test_a_scan_of_everything_scans_each_library(self):
        libs = [types.SimpleNamespace(label="A"), types.SimpleNamespace(label="B")]
        jobs_ = jobs.Jobs(types.SimpleNamespace(libraries=libs), "base")
        with mock.patch.object(jobs.scan, "scan_library", side_effect=lambda db, lib, log: lib.label) as scanned:
            job = jobs_.scan_all()
            self.wait(job)
        self.assertEqual((job.result, scanned.call_count), (["A", "B"], 2))


class TestIsJunk(unittest.TestCase):
    def test_what_windows_and_macos_sow(self):
        for name in ("Thumbs.db", "desktop.ini", ".DS_Store", "AlbumArtSmall.jpg", "AlbumArt_{12345678-1234-1234-1234-123456789abc}_Large.jpg",
                     "ALBUMART_{12345678-1234-1234-1234-123456789abc}_SMALL.JPG"):
            self.assertTrue(jobs.is_junk(name), name)
            self.assertTrue(jobs.is_junk(name, True), name)

    def test_an_ordinary_file_is_not_and_an_image_is_only_in_music(self):
        for name in ("film.mkv", "cover.jpg", "folder.jpg", "AlbumArt_{pas-un-id}_Large.jpg"):
            self.assertFalse(jobs.is_junk(name), name)
        self.assertTrue(jobs.is_junk("folder.jpg", music=True))
        self.assertTrue(jobs.is_junk("front.PNG", music=True))

    def test_in_music_the_cover_the_missing_album_cover_and_the_photo_of_the_artist_stay(self):
        for name in ("cover.jpg", "Cover.PNG", "artist.webp", "1997 - OK Computer.jpg", "01 - titre.flac"):
            self.assertFalse(jobs.is_junk(name, music=True), name)


class TestWalk(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        for rel in ("a/Thumbs.db", "a/b/desktop.ini", "a/film.mkv", "a/folder.jpg", ".zfs/snapshot/Thumbs.db", f"{jobs.coverfile.CANDIDATES_DIR}/choix.jpg", "z/.DS_Store"):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")

    def found(self, music=False):
        return sorted(Path(p).relative_to(self.root).as_posix() for p in jobs.walk_junk(self.root, music))

    def test_the_junk_is_found_in_every_folder_but_not_in_the_snapshots(self):
        self.assertEqual(self.found(), ["a/Thumbs.db", "a/b/desktop.ini", "z/.DS_Store"])

    def test_music_adds_the_images_but_leaves_the_folder_of_candidate_covers(self):
        self.assertEqual(self.found(True), ["a/Thumbs.db", "a/b/desktop.ini", "a/folder.jpg", "z/.DS_Store"])

    def test_the_default_is_not_music(self):
        self.assertNotIn("a/folder.jpg", [Path(p).relative_to(self.root).as_posix() for p in jobs.walk_junk(self.root)])

    def test_symbolic_links_are_not_followed(self):
        with mock.patch.object(jobs.os, "walk", return_value=[]) as walked:
            list(jobs.walk_junk(self.root))
        self.assertIs(walked.call_args.kwargs["followlinks"], False)


class TestCleanJunk(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.lib = types.SimpleNamespace(path=self.root, kind="movies")
        for name in ("a/Thumbs.db", "b/Thumbs.db", "c/Thumbs.db"):
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_bytes(b"x")
        self.log = []

    def clean(self, apply):
        out = jobs.clean_junk([self.lib], apply, self.log.append)
        self.sample = out.pop("sample")
        return out

    def named(self, *folders):
        return sorted(f"{self.root.name}/{folder}/Thumbs.db" for folder in folders)

    def test_a_simulation_counts_removes_nothing_and_names_the_files(self):
        self.assertEqual(self.clean(False), {"found": 3, "removed": 0})
        self.assertEqual(self.sample, self.named("a", "b", "c"))
        self.assertEqual(self.log[-1], "3 fichier(s) de ce genre (simulation : rien n'a été effacé).")
        self.assertEqual(sorted(self.log[:-1]), sorted("  " + name for name in self.sample))
        self.assertTrue((self.root / "a" / "Thumbs.db").exists())

    def test_the_real_thing_removes_them_and_lists_nothing_in_the_log(self):
        self.assertEqual(self.clean(True), {"found": 3, "removed": 3})
        self.assertEqual(self.sample, self.named("a", "b", "c"))
        self.assertEqual(self.log, ["3 fichier(s) de ce genre, 3 effacé(s)."])
        self.assertFalse((self.root / "a" / "Thumbs.db").exists())

    def test_only_the_first_twenty_are_named_and_the_rest_is_counted(self):
        for n in range(22):
            (self.root / f"d{n:02d}").mkdir()
            (self.root / f"d{n:02d}" / "Thumbs.db").write_bytes(b"x")
        self.assertEqual(self.clean(False), {"found": 25, "removed": 0})
        self.assertEqual(len(self.sample), 20)
        self.assertEqual(self.sample, sorted(self.sample))
        self.assertEqual(len(set(self.sample)), 20)
        self.assertEqual(self.log[-2:], ["  … et 5 autre(s).", "25 fichier(s) de ce genre (simulation : rien n'a été effacé)."])
        self.assertEqual(len(self.log), 22)

    def test_exactly_twenty_files_have_no_and_more_line(self):
        for n in range(17):
            (self.root / f"d{n:02d}").mkdir()
            (self.root / f"d{n:02d}" / "Thumbs.db").write_bytes(b"x")
        self.assertEqual(self.clean(False), {"found": 20, "removed": 0})
        self.assertEqual(len(self.log), 21)
        self.assertNotIn("autre(s)", " ".join(self.log[:-1]))

    def test_a_file_is_named_from_the_root_of_its_library_with_slashes(self):
        deep = self.root / "Artiste" / "Album" / "CD1"
        deep.mkdir(parents=True)
        (deep / "Thumbs.db").write_bytes(b"x")
        self.clean(False)
        self.assertIn(f"{self.root.name}/Artiste/Album/CD1/Thumbs.db", self.sample)

    def test_a_read_only_volume_is_told_once_then_counted(self):
        with mock.patch.object(jobs, "force_unlink", side_effect=OSError(errno.EROFS, "lecture seule")):
            self.assertEqual(self.clean(True), {"found": 3, "removed": 0})
        self.assertEqual(len(self.log), 3)
        self.assertIn("volume en lecture seule", self.log[0])
        self.assertEqual(self.log[1], f"  … et 2 autre(s) fichier(s) de {self.root} sur ce volume en lecture seule.")
        self.assertEqual(self.log[2], "3 fichier(s) de ce genre, 0 effacé(s).")

    def test_one_file_on_a_read_only_volume_gets_no_summary_line(self):
        (self.root / "b" / "Thumbs.db").unlink()
        (self.root / "c" / "Thumbs.db").unlink()
        with mock.patch.object(jobs, "force_unlink", side_effect=OSError(errno.EROFS, "lecture seule")):
            self.clean(True)
        self.assertEqual(len(self.log), 2)
        self.assertIn("volume en lecture seule", self.log[0])

    def test_every_other_error_is_told_for_its_file_and_does_not_count_as_read_only(self):
        with mock.patch.object(jobs, "force_unlink", side_effect=OSError(errno.EACCES, "refusé")):
            self.clean(True)
        self.assertEqual(len(self.log), 4)
        self.assertTrue(all("impossible d'effacer" in line and "refusé" in line and "lecture seule" not in line for line in self.log[:3]))

    def test_a_music_library_is_cleaned_of_its_extra_images(self):
        (self.root / "a" / "folder.jpg").write_bytes(b"x")
        self.lib.kind = "music"
        self.assertEqual(self.clean(False)["found"], 4)

    def test_each_library_has_its_own_read_only_count(self):
        other = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, other, True)
        (other / "Thumbs.db").write_bytes(b"x")
        with mock.patch.object(jobs, "force_unlink", side_effect=OSError(errno.EROFS, "lecture seule")):
            jobs.clean_junk([self.lib, types.SimpleNamespace(path=other, kind="movies")], True, self.log.append)
        told = [line for line in self.log if "volume en lecture seule (retire" in line]
        self.assertEqual(len(told), 2)
        self.assertEqual(sum("… et" in line for line in self.log), 1)


class TestForceUnlink(unittest.TestCase):
    def test_an_ordinary_file_is_removed(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        (folder / "a").write_bytes(b"x")
        jobs.force_unlink(str(folder / "a"))
        self.assertFalse((folder / "a").exists())


if __name__ == "__main__":
    unittest.main()
