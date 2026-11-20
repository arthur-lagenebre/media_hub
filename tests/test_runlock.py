"""Le verrou de HUB_DATA (hub.runlock) : le serveur le garde tant qu'il tourne, une restauration le prend avant de toucher aux données. Un verrou du système (fcntl.flock, msvcrt.locking) : libéré seul quand le processus s'arrête."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from hub import runlock


class TestRunLock(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.data, True)

    def test_the_first_holder_gets_the_lock_and_a_second_does_not(self):
        first = runlock.hold(self.data)
        self.addCleanup(runlock.release, first)
        self.assertIsNotNone(first)
        self.assertIsNone(runlock.hold(self.data))

    def test_the_lock_is_free_again_once_released(self):
        runlock.release(runlock.hold(self.data))
        again = runlock.hold(self.data)
        self.addCleanup(runlock.release, again)
        self.assertIsNotNone(again)

    def test_releasing_nothing_does_nothing(self):
        runlock.release(None)

    def test_the_data_folder_is_created_if_missing(self):
        folder = self.data / "neuf"
        handle = runlock.hold(folder)
        self.addCleanup(runlock.release, handle)
        self.assertTrue((folder / runlock.NAME).exists())

    def test_the_data_folder_is_created_with_its_parents(self):
        folder = self.data / "a" / "b"
        handle = runlock.hold(folder)
        self.addCleanup(runlock.release, handle)
        self.assertTrue((folder / runlock.NAME).exists())

    def test_another_process_holding_the_lock_is_seen_and_its_end_frees_it(self):
        """Comme le conteneur du serveur et celui d'une restauration : deux processus, un seul fichier."""
        holder = subprocess.Popen([sys.executable, "-c", "import sys, time; from hub import runlock; h = runlock.hold(sys.argv[1]); print('pris' if h else 'refusé', flush=True); time.sleep(60)", str(self.data)],
                                  stdout=subprocess.PIPE, text=True, cwd=Path(__file__).resolve().parent.parent)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "pris")
        self.assertIsNone(runlock.hold(self.data))
        holder.kill()                         # un arrêt brutal : rien n'efface le fichier, le système libère le verrou
        holder.wait()
        handle = runlock.hold(self.data)
        self.addCleanup(runlock.release, handle)
        self.assertIsNotNone(handle)


class TestWindowsLock(unittest.TestCase):
    """Sous Windows (pas de fcntl), le verrou est un octet du fichier, pris et rendu depuis son début (`msvcrt.locking`). Un faux `msvcrt` le vérifie partout, sans Windows."""

    def setUp(self):
        self.data = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.data, True)
        self.calls = []
        self.taken = False
        (self.data / runlock.NAME).write_bytes(b"xx")          # ouvert en ajout, le fichier se place à la fin : la position demandée se voit
        fake = types.SimpleNamespace(LK_NBLCK="prendre", LK_UNLCK="rendre", locking=self.locking)
        for patch in (mock.patch.object(runlock, "fcntl", None), mock.patch.object(runlock, "msvcrt", fake, create=True)):
            patch.start()
            self.addCleanup(patch.stop)

    def locking(self, fd, mode, size):
        self.calls.append((mode, os.lseek(fd, 0, os.SEEK_CUR), size))
        if mode == "prendre" and self.taken:
            raise OSError("verrou pris")
        self.taken = mode == "prendre"

    def test_one_byte_from_the_start_of_the_file_is_taken_without_waiting_and_given_back_from_there(self):
        handle = runlock.hold(self.data)
        self.assertIsNotNone(handle)
        runlock.release(handle)
        self.assertEqual(self.calls, [("prendre", 0, 1), ("rendre", 0, 1)])
        self.assertTrue(handle.closed)

    def test_a_lock_that_is_already_taken_is_refused(self):
        first = runlock.hold(self.data)
        self.addCleanup(runlock.release, first)
        self.assertIsNone(runlock.hold(self.data))

    def test_a_lock_that_cannot_be_given_back_is_still_closed(self):
        handle = runlock.hold(self.data)
        self.taken = False
        with mock.patch.object(runlock.msvcrt, "locking", side_effect=OSError("déjà rendu")):
            runlock.release(handle)
        self.assertTrue(handle.closed)


if __name__ == "__main__":
    unittest.main()
