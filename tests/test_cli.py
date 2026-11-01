"""Les commandes `python -m hub user ...` : de vrais comptes dans une vraie base de HUB_DATA, la saisie du mot de passe simulée."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import __main__ as cli
from hub import accounts
from hub.db import Database

PASSWORD = "un-mot-de-passe-long"


class TestUserCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patch = mock.patch.dict(os.environ, {"HUB_DATA": self.tmp})
        patch.start()
        self.addCleanup(patch.stop)

    def run_cli(self, *argv, typed=(PASSWORD, PASSWORD)):
        """Ce que la commande écrit ; `typed` : les saisies du mot de passe, dans l'ordre."""
        out = io.StringIO()
        with mock.patch.object(cli, "read_secret", side_effect=list(typed)), contextlib.redirect_stdout(out):
            cli.user_command(list(argv))
        return out.getvalue()

    def names(self):
        db = Database(Path(self.tmp) / "hub.db")
        return [(r["name"], r["role"]) for r in accounts.rows(db)]

    def test_list_is_empty_without_accounts(self):
        self.assertEqual(self.run_cli("list"), "")

    def test_the_first_account_is_an_administrator_and_says_it_takes_over_the_old_progress(self):
        said = self.run_cli("add", "alice")
        self.assertIn("compte « alice » créé (admin", said)
        self.assertIn("reprend la progression", said)
        self.assertEqual(self.names(), [("alice", "admin")])

    def test_the_next_accounts_are_users_unless_asked_otherwise(self):
        self.run_cli("add", "alice")
        said = self.run_cli("add", "bob")
        self.assertIn("créé (user)", said)
        self.assertNotIn("reprend la progression", said)
        self.run_cli("add", "carole", "--admin")
        self.assertEqual(self.names(), [("alice", "admin"), ("bob", "user"), ("carole", "admin")])

    def test_list_shows_name_role_and_id(self):
        self.run_cli("add", "alice")
        line = self.run_cli("list").strip()
        self.assertTrue(line.startswith("alice"))
        self.assertIn("admin", line)

    def test_two_different_passwords_create_nothing(self):
        with self.assertRaises(SystemExit) as stop:
            self.run_cli("add", "alice", typed=(PASSWORD, "autre chose"))
        self.assertIn("ne correspondent pas", str(stop.exception))
        self.assertEqual(self.names(), [])

    def test_an_account_error_ends_the_command_with_its_message(self):
        self.run_cli("add", "alice")
        with self.assertRaises(SystemExit) as stop:
            self.run_cli("add", "ALICE")                                # un nom déjà pris, à la casse près
        self.assertTrue(str(stop.exception))
        self.assertEqual(len(self.names()), 1)

    def test_passwd_changes_the_password_of_an_existing_account(self):
        self.run_cli("add", "alice")
        said = self.run_cli("passwd", "Alice", typed=("nouveau-mot-de-passe", "nouveau-mot-de-passe"))
        self.assertIn("mot de passe de « alice » changé", said)
        db = Database(Path(self.tmp) / "hub.db")
        self.assertEqual(accounts.authenticate(db, "alice", "nouveau-mot-de-passe")["name"], "alice")
        with self.assertRaises(accounts.AccountError):
            accounts.authenticate(db, "alice", PASSWORD)                                      # l'ancien ne passe plus

    def test_rm_deletes_an_account(self):
        self.run_cli("add", "alice")
        self.run_cli("add", "bob")
        said = self.run_cli("rm", "bob")
        self.assertIn("compte « bob » supprimé", said)
        self.assertEqual(self.names(), [("alice", "admin")])

    def test_an_unknown_account_is_refused_for_passwd_and_rm(self):
        for action in ("passwd", "rm"):
            with self.assertRaises(SystemExit) as stop:
                self.run_cli(action, "personne")
            self.assertIn("compte inconnu : personne", str(stop.exception))


class TestReadSecret(unittest.TestCase):
    def test_from_a_pipe_each_entry_is_one_line(self):
        with mock.patch("sys.stdin", io.StringIO("premier\r\nsecond\n")), mock.patch("sys.stdin.isatty", return_value=False, create=True):
            self.assertEqual((cli.read_secret("a : "), cli.read_secret("b : ")), ("premier", "second"))

    def test_at_the_keyboard_nothing_is_echoed(self):
        stdin = mock.Mock()
        stdin.isatty.return_value = True
        with mock.patch("sys.stdin", stdin), mock.patch("getpass.getpass", return_value="secret") as asked:
            self.assertEqual(cli.read_secret("Mot de passe : "), "secret")
        asked.assert_called_once_with("Mot de passe : ")


if __name__ == "__main__":
    unittest.main()
