"""Défauts du code de la boîte de réception trouvés par les tests de `test_inbox_hostile.py` : chaque test décrit le comportement attendu et échoue tant que `hub/inbox.py` n'est pas corrigé."""

from __future__ import annotations

import threading
import unittest

from tests.test_inbox import make
from tests.test_inbox_hostile import LAG, Gate, Sender
from tests.test_inbox_subfolders import SubCase, tree


class TestFailedUploadsLeaveNothing(SubCase):
    def test_two_uploads_that_fail_in_the_same_new_folder_leave_no_empty_folder(self):
        """DÉFAUT (hub/inbox.py, `receive` : `made` ne retient que les dossiers que CET envoi a créés). Le premier envoi crée `Neuf/Profond` ; le second y entre (dossier déjà là, donc pas dans son `made`).
        Le premier échoue : le dossier n'est pas vide (le `.part` du second), il reste. Le second échoue ensuite : il n'a rien créé, il ne retire rien. Le dossier vide reste pour toujours, compte dans MAX_SUBFOLDERS, et rien ne le nettoie (`sweep` ne retire que ce qu'il vide)."""
        box = make(self.tmp, self.db, inbox_max_uploads=8)
        first_in, first_done = threading.Event(), threading.Event()
        one = Sender(box, "a.mkv", Gate(short=True, wait_for=first_in), 10, "Neuf/Profond")
        two = Sender(box, "b.mkv", Gate(short=True, wait_for=first_done), 10, "neuf/profond")
        one.start()
        self.assertTrue(one.call[2].ready.wait(LAG))
        two.start()
        self.assertTrue(two.call[2].ready.wait(LAG))               # le second est dans le dossier du premier
        first_in.set()
        one.join(LAG)
        first_done.set()
        two.join(LAG)
        self.assertEqual((one.error.status, two.error.status), (400, 400))
        self.assertEqual(tree(self.alice), [])


if __name__ == "__main__":
    unittest.main()
