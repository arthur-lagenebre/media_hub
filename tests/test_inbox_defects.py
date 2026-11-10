"""Défauts du code de la boîte de réception trouvés par les tests de `test_inbox_hostile.py` : chaque test décrit le comportement attendu et échoue tant que `hub/inbox.py` n'est pas corrigé."""

from __future__ import annotations

import io
import threading
import unittest

from hub.inbox import InboxError
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

    def fail_in(self, sub, name="rate.mkv"):
        with self.assertRaises(InboxError) as caught:
            self.inbox.receive("alice", name, io.BytesIO(b"ab"), 10, sub)
        return caught.exception.status

    def test_a_failed_upload_also_takes_back_a_folder_on_its_way_that_was_already_empty(self):
        (self.alice / "Vide").mkdir(parents=True)
        self.assertEqual(self.fail_in("Vide/Neuf"), 400)
        self.assertEqual(tree(self.alice), [])                 # comme `remove` : un parent devenu vide part avec lui, mais jamais le dossier du compte

    def test_a_failed_upload_never_takes_back_a_folder_that_holds_something(self):
        self.send("vieux.mkv", "Serie/S1")
        self.assertEqual(self.fail_in("Serie/S1"), 400)
        self.assertEqual(self.fail_in("Serie/S2"), 400)
        self.assertEqual(self.fail_in("Serie/S1/Bonus"), 400)
        self.assertEqual(tree(self.alice), ["Serie/", "Serie/S1/", "Serie/S1/vieux.mkv"])

    def test_a_failed_upload_in_the_account_folder_keeps_the_account_folder(self):
        self.send("vieux.mkv")
        self.assertEqual(self.fail_in(None), 400)
        self.assertEqual(tree(self.alice), ["vieux.mkv"])
        self.assertEqual(self.fail_in(""), 400)
        self.assertTrue(self.alice.is_dir())

    def test_a_failed_upload_that_other_hands_a_file_into_the_folder_keeps_it(self):
        (self.alice / "Serie" / "S1").mkdir(parents=True)
        (self.alice / "Serie" / "S1" / "x.txt").write_bytes(b"main")          # posé à la main : le dossier n'est pas vide
        self.assertEqual(self.fail_in("serie/s1"), 400)
        self.assertEqual(tree(self.alice), ["Serie/", "Serie/S1/", "Serie/S1/x.txt"])


if __name__ == "__main__":
    unittest.main()
