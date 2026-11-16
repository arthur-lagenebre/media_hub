"""Le verrou de HUB_DATA (hub.runlock) : le serveur le garde tant qu'il tourne, une restauration le prend avant de toucher aux données. Un verrou du système (fcntl.flock, msvcrt.locking) : libéré seul quand le processus s'arrête."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
