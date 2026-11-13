"""Les tâches de fond : une file, une tâche à la fois, jamais deux fois la même en attente ; le nettoyage des fichiers parasites et ce qu'il dit quand le volume est en lecture seule."""

from __future__ import annotations

import errno
import json
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

from hub.db import Database  # noqa: E402
from hub.media.probe import ProbeError  # noqa: E402
from hub.server import jobs  # noqa: E402
from tests.test_server_views import entry, item  # noqa: E402


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
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.jobs = jobs.Jobs(types.SimpleNamespace(libraries=[], ffprobe=""), Database(root / "hub.db"))          # une vraie base : les intitulés lisent les libellés

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

    NO_RATING = {"rated": 0, "failed": 0}

    def test_the_enrichments_ask_for_the_force_only_when_told_to(self):
        for task, target in (("enrich", "enrich_shows"), ("enrich_sagas", "enrich_sagas"), ("enrich_artists", "enrich_artists")):
            for force in (None, True):
                with mock.patch.object(jobs.enrich, target, return_value={"failed": 0}) as called, \
                        mock.patch.object(jobs.enrich, "enrich_ratings", return_value=self.NO_RATING) as ratings:
                    job = getattr(self.jobs, task)(self.lib) if force is None else getattr(self.jobs, task)(self.lib, force)
                    self.wait(job)
                self.assertIs(called.call_args.args[-1], bool(force), (task, force))
                if ratings.called:
                    self.assertIs(ratings.call_args.args[-1], bool(force), (task, force))

    def test_the_tmdb_enrichments_end_with_the_ratings_and_count_both(self):
        for task, target in (("enrich", "enrich_shows"), ("enrich_sagas", "enrich_sagas")):
            with mock.patch.object(jobs.enrich, target, return_value={"sagas": 1, "failed": 2}), \
                    mock.patch.object(jobs.enrich, "enrich_ratings", return_value={"rated": 5, "failed": 1}) as ratings:
                job = getattr(self.jobs, task)(self.lib)
                self.wait(job)
            self.assertEqual(ratings.call_args.args[2], self.lib, task)
            self.assertLessEqual(ratings.call_args.kwargs["since"], int(job.started) + 1, task)         # le début de la tâche : ce qui a été noté depuis n'est pas redemandé
            self.assertEqual(job.result, {"sagas": 1, "rated": 5, "failed": 3}, task)

    def test_the_artists_have_no_rating(self):
        with mock.patch.object(jobs.enrich, "enrich_artists", return_value={}), mock.patch.object(jobs.enrich, "enrich_ratings") as ratings:
            self.wait(self.jobs.enrich_artists(self.lib))
        ratings.assert_not_called()

    def test_every_task_has_its_own_label(self):
        labels = set()
        with mock.patch.object(jobs.enrich, "enrich_shows", return_value={"failed": 0}), mock.patch.object(jobs.enrich, "enrich_sagas", return_value={"failed": 0}), \
                mock.patch.object(jobs.enrich, "enrich_artists"), mock.patch.object(jobs.enrich, "enrich_ratings", return_value=self.NO_RATING), \
                mock.patch.object(jobs.scan, "scan_library", return_value={}), mock.patch.object(jobs, "clean_junk", return_value={}):
            for job in (self.jobs.scan(self.lib), self.jobs.scan_all(), self.jobs.enrich(self.lib), self.jobs.enrich_sagas(self.lib), self.jobs.enrich_artists(self.lib),
                        self.jobs.cleanup(False), self.jobs.cleanup(True)):
                self.wait(job)
                labels.add(job.label)
        self.assertEqual(labels, {"Mise à jour · Films", "Mise à jour · tout", "Enrichissement TMDB · Films", "Sagas TMDB · Films", "Fiches artistes · Films",
                                  "Fichiers parasites · Compter", "Fichiers parasites · Nettoyage"})

    def test_a_scan_of_everything_scans_each_library(self):
        libs = [types.SimpleNamespace(label="A"), types.SimpleNamespace(label="B")]
        jobs_ = jobs.Jobs(types.SimpleNamespace(libraries=libs, ffprobe=""), "base")
        with mock.patch.object(jobs.scan, "scan_library", side_effect=lambda db, lib, log: lib.label) as scanned:
            job = jobs_.scan_all()
            self.wait(job)
        self.assertEqual((job.result, scanned.call_count), (["A", "B"], 2))


SUMMARY = {"container": "mkv", "duration": 10.0, "bitrate": 1, "size": 1, "video": {"codec": "h264", "width": 1920, "height": 1080, "hdr": False}, "audio": [], "subs": []}


def probe_cfg(root, ffprobe="ffprobe"):
    """Des réglages avec une bibliothèque « lib » sous `root`, et ffprobe (un nom : les tests ne le lancent pas)."""
    lib = types.SimpleNamespace(id="lib", label="Films", path=root / "lib", kind="movies")
    return types.SimpleNamespace(libraries=[lib], ffprobe=ffprobe)


def videos(db, *names, lib="lib", **over):
    """Un film par nom dans la bibliothèque, pas encore sondé."""
    db.replace_library(lib, [item(n, lib=lib) for n in names], [entry(f"{n}-v", n, lib=lib, **over) for n in names])


def probe_column(db):
    return {r["id"]: r["probe"] for r in db.query("SELECT id, probe FROM entries")}


class TestProbeVideos(unittest.TestCase):
    """La sonde des vidéos que personne n'a encore lues : une par une, sans jamais échouer pour un fichier."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.db = Database(self.root / "hub.db")
        self.cfg = probe_cfg(self.root)
        self.log, self.unreadable = [], set()

    def run_probe(self, answer=None, stop=lambda: False):
        with mock.patch.object(jobs.prober, "run_ffprobe", side_effect=answer or (lambda ffprobe, path: SUMMARY)) as called:
            result = jobs.probe_videos(self.cfg, self.db, self.log.append, self.unreadable, stop)
        return result, [call.args for call in called.call_args_list]

    def test_only_the_videos_without_a_summary_are_probed(self):
        self.db.replace_library("lib", [item("a"), item("b"), item("c", "album")],
                                [entry("a-v", "a"), entry("b-v", "b", probe=json.dumps({"vu": 1})), entry("c-t", "c", kind="audio")])
        self.db.execute("UPDATE entries SET probe=? WHERE id='b-v'", (json.dumps({"vu": 1}),))          # replace_library ne garde que les sondes d'avant
        result, calls = self.run_probe()
        self.assertEqual(result, {"probed": 1, "failed": 0, "left": 0, "interrupted": False})
        self.assertEqual(calls, [("ffprobe", self.root / "lib" / "a" / "a-v")])
        self.assertEqual(probe_column(self.db), {"a-v": json.dumps(SUMMARY), "b-v": json.dumps({"vu": 1}), "c-t": None})
        self.assertEqual(self.log, ["1 vidéo(s) à analyser.", "1 vidéo(s) analysée(s)."])

    def test_nothing_to_probe_says_so(self):
        result, calls = self.run_probe()
        self.assertEqual((result, calls, self.log), ({"probed": 0, "failed": 0, "left": 0, "interrupted": False}, [], ["Aucune vidéo à analyser."]))

    def test_a_second_run_has_nothing_left_to_do(self):
        videos(self.db, "a", "b")
        self.run_probe()
        result, calls = self.run_probe()
        self.assertEqual((result["probed"], calls), (0, []))

    def test_an_unreadable_file_is_said_and_the_next_ones_are_probed(self):
        videos(self.db, "a", "b", "c")

        def answer(ffprobe, path):
            if path.name == "a-v":
                raise ProbeError("fichier abîmé")
            if path.name == "b-v":
                raise ValueError("réponse illisible")
            return SUMMARY

        result, calls = self.run_probe(answer)
        self.assertEqual(result, {"probed": 1, "failed": 2, "left": 0, "interrupted": False})
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.log[1:], ["  illisible : a/a-v : fichier abîmé", "  illisible : b/b-v : réponse illisible", "1 vidéo(s) analysée(s), 2 illisible(s)."])
        self.assertEqual(probe_column(self.db), {"a-v": None, "b-v": None, "c-v": json.dumps(SUMMARY)})

    def test_an_unreadable_file_is_not_tried_again_until_it_changes(self):
        videos(self.db, "a")
        self.run_probe(mock.Mock(side_effect=ProbeError("abîmé")))
        result, calls = self.run_probe()
        self.assertEqual((result["probed"], calls), (0, []))
        videos(self.db, "a", mtime=2)                              # le fichier a été remplacé
        result, calls = self.run_probe()
        self.assertEqual((result["probed"], len(calls)), (1, 1))

    def test_without_ffprobe_nothing_is_probed_and_nothing_fails(self):
        videos(self.db, "a", "b")
        self.cfg.ffprobe = ""
        result, calls = self.run_probe()
        self.assertEqual((result, calls), ({"probed": 0, "failed": 0, "left": 2, "interrupted": False}, []))
        self.assertEqual(self.log, ["ffprobe est introuvable : les vidéos ne sont pas analysées (installe ffmpeg sur le serveur, voir deploy/README.md)."])

    def test_it_stops_between_two_files_when_asked(self):
        videos(self.db, "a", "b", "c")
        asked = []
        result, calls = self.run_probe(stop=lambda: asked.append(1) or len(asked) > 1)          # oui à la deuxième question : après le premier fichier
        self.assertEqual(result, {"probed": 1, "failed": 0, "left": 2, "interrupted": True})
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.log[-1], "Interrompue : 1 vidéo(s) analysée(s), il en reste 2 ; la suite après la tâche qui attend.")
        self.assertEqual(self.run_probe()[0], {"probed": 2, "failed": 0, "left": 0, "interrupted": False})       # la reprise finit le travail

    def test_the_files_of_a_library_that_is_no_longer_configured_are_left_alone(self):
        videos(self.db, "a", lib="partie")
        result, calls = self.run_probe()
        self.assertEqual((result["left"], calls, self.log), (0, [], ["Aucune vidéo à analyser."]))


class TestProbeJob(unittest.TestCase):
    """La sonde dans la file : lancée par un scan, elle laisse passer la tâche qui attend et reprend après."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.db = Database(self.root / "hub.db")
        self.cfg = probe_cfg(self.root)
        self.jobs = jobs.Jobs(self.cfg, self.db)

    def idle(self):
        for _ in range(500):
            if not self.jobs.busy():
                return
            threading.Event().wait(0.01)
        self.fail("la file ne se vide pas")

    def labels(self):
        return [j.label for j in reversed(self.jobs.history)]

    def scan(self):
        with mock.patch.object(jobs.scan, "scan_library", return_value={}), mock.patch.object(jobs.prober, "run_ffprobe", return_value=SUMMARY):
            self.jobs.scan(self.cfg.libraries[0])
            self.idle()

    def test_a_scan_is_followed_by_the_probe_of_what_is_not_probed_yet(self):
        videos(self.db, "a", "b")
        self.scan()
        self.assertEqual(self.labels(), ["Mise à jour · Films", "Analyse des vidéos"])
        self.assertEqual(self.jobs.history[0].result, {"probed": 2, "failed": 0, "left": 0, "interrupted": False})
        self.assertEqual(set(probe_column(self.db).values()), {json.dumps(SUMMARY)})

    def test_a_scan_of_everything_too(self):
        videos(self.db, "a")
        with mock.patch.object(jobs.scan, "scan_library", return_value={}), mock.patch.object(jobs.prober, "run_ffprobe", return_value=SUMMARY):
            self.jobs.scan_all()
            self.idle()
        self.assertEqual(self.labels(), ["Mise à jour · tout", "Analyse des vidéos"])

    def test_a_scan_with_nothing_left_to_probe_starts_nothing(self):
        self.scan()
        self.assertEqual(self.labels(), ["Mise à jour · Films"])
        videos(self.db, "a")
        self.scan()
        self.scan()
        self.assertEqual(self.labels(), ["Mise à jour · Films", "Mise à jour · Films", "Analyse des vidéos", "Mise à jour · Films"])

    def test_a_scan_without_ffprobe_starts_nothing(self):
        videos(self.db, "a")
        self.cfg.ffprobe = ""
        self.scan()
        self.assertEqual(self.labels(), ["Mise à jour · Films"])

    def test_the_probe_asked_by_hand_does_not_fail_without_ffprobe(self):
        videos(self.db, "a")
        self.cfg.ffprobe = ""
        job = self.jobs.probe()
        self.idle()
        self.assertEqual((job.status, job.result["left"]), ("done", 1))

    def test_it_gives_way_to_a_waiting_job_and_resumes_after_it(self):
        videos(self.db, "a", "b", "c")
        first, go = threading.Event(), threading.Event()
        order = []

        def answer(ffprobe, path):
            order.append(path.name)
            first.set()
            go.wait(10)
            return SUMMARY

        with mock.patch.object(jobs.prober, "run_ffprobe", side_effect=answer):
            probe = self.jobs.probe()
            first.wait(10)
            self.assertIs(self.jobs.probe(), probe)                                  # déjà en cours : pas une seconde
            self.jobs._submit("autre", lambda log: order.append("autre"))            # une tâche arrive pendant le premier fichier
            go.set()
            self.idle()
        self.assertEqual(order, ["a-v", "autre", "b-v", "c-v"])
        self.assertEqual(self.labels(), ["Analyse des vidéos", "autre", "Analyse des vidéos"])
        self.assertEqual(probe.result, {"probed": 1, "failed": 0, "left": 2, "interrupted": True})
        self.assertEqual(self.jobs.history[0].result, {"probed": 2, "failed": 0, "left": 0, "interrupted": False})


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
