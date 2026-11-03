"""La création d'un compte : nom, rôle, mot de passe, et ce qui change pour le premier."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from hub import accounts, prefs
from hub.accounts import AccountError
from hub.db import Database


class TestCreate(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.db = Database(root / "hub.db")

    def refused(self, call, status=400):
        with self.assertRaises(AccountError) as caught:
            call()
        self.assertEqual(caught.exception.status, status)
        return caught.exception.message

    def test_the_first_account_is_the_administrator_whatever_role_was_asked(self):
        row = accounts.create(self.db, "alice", "un-mot-de-passe", role="user")
        self.assertEqual((row["id"], row["name"], row["role"]), (accounts.FIRST_ID, "alice", "admin"))
        other = accounts.create(self.db, "bob", "un-mot-de-passe")
        self.assertEqual(other["role"], "user")
        self.assertNotEqual(other["id"], accounts.FIRST_ID)

    def test_a_name_that_is_taken_is_refused_whatever_its_case(self):
        accounts.create(self.db, "alice", "un-mot-de-passe")
        self.assertIn("déjà pris", self.refused(lambda: accounts.create(self.db, "ALICE", "un-mot-de-passe"), 409))

    def test_an_unknown_role_is_refused(self):
        self.assertIn("rôle inconnu", self.refused(lambda: accounts.create(self.db, "alice", "un-mot-de-passe", role="roi")))

    def test_a_short_password_is_refused_unless_the_check_is_off(self):
        self.assertIn("doit faire de", self.refused(lambda: accounts.create(self.db, "alice", "court")))
        self.assertEqual(accounts.create(self.db, "alice", "court", check=False)["name"], "alice")

    def test_without_the_check_a_password_must_still_be_a_non_empty_text(self):
        for password in ("", None, 1234, b"octets"):
            self.assertIn("mot de passe vide", self.refused(lambda: accounts.create(self.db, "alice", password, check=False)), repr(password))
        self.assertEqual(accounts.rows(self.db), [])


class TestDeleteKeepsNothingOfTheAccount(unittest.TestCase):
    def test_the_settings_of_a_deleted_account_go_with_it(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        db = Database(root / "hub.db")
        accounts.create(db, "alice", "un-mot-de-passe")
        bob = accounts.create(db, "bob", "un-mot-de-passe")
        db.kv_set(prefs.KEY + bob["id"], {"music_view": "artists"})
        db.kv_set(prefs.KEY + accounts.FIRST_ID, {"music_view": "artists"})
        accounts.delete(db, bob["id"])
        self.assertEqual((db.kv_get(prefs.KEY + bob["id"]), db.kv_get(prefs.KEY + accounts.FIRST_ID)), (None, {"music_view": "artists"}))


if __name__ == "__main__":
    unittest.main()
