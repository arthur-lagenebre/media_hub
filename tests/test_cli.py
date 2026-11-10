"""Les commandes `python -m hub user ...` : de vrais comptes dans une vraie base de HUB_DATA, la saisie du mot de passe simulée."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import tempfile
import types
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

    def test_an_action_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stop:
            cli.user_command([])
        self.assertEqual(stop.exception.code, 2)

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

    def test_a_password_of_eleven_characters_is_refused_by_add_and_passwd(self):
        with self.assertRaises(SystemExit) as stop:
            self.run_cli("add", "alice", typed=("a" * 11, "a" * 11))
        self.assertIn("de 12 à 200 caractères", str(stop.exception))
        self.assertEqual(self.names(), [])
        self.run_cli("add", "alice", typed=("a" * 12, "a" * 12))
        with self.assertRaises(SystemExit) as stop:
            self.run_cli("passwd", "alice", typed=("b" * 11, "b" * 11))
        self.assertIn("de 12 à 200 caractères", str(stop.exception))
        db = Database(Path(self.tmp) / "hub.db")
        self.assertEqual(accounts.authenticate(db, "alice", "a" * 12)["name"], "alice")             # l'ancien mot de passe est resté

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

    def test_rights_shows_and_changes_what_an_account_sees_and_downloads(self):
        libraries = Path(self.tmp) / "libraries.json"
        libraries.write_text('[{"id": "films", "label": "Films", "kind": "movies", "path": "/x/films"}, {"id": "livres", "label": "Livres", "kind": "books", "path": "/x/livres"}]', encoding="utf-8")
        with mock.patch.dict(os.environ, {"HUB_LIBRARIES": str(libraries)}):
            self.run_cli("add", "chef")
            self.run_cli("add", "alice")
            said = self.run_cli("rights", "alice")
            self.assertEqual(said.splitlines()[1:], ["  films            voir  lire  télécharger", "  livres           voir  lire  télécharger"])         # sans réglage, tout est permis
            said = self.run_cli("rights", "alice", "--set", "films=play", "--set", "livres=none")
            self.assertEqual(said.splitlines()[1:], ["  films            voir  lire  -----------", "  livres           ----  ----  -----------"])
            said = self.run_cli("rights", "alice", "--set", "films=view")
            self.assertEqual(said.splitlines()[1], "  films            voir  ----  -----------")
            self.assertIn("alice (user)", said)
            self.assertIn("un administrateur a tous les droits", self.run_cli("rights", "chef"))
            for bad in (("--set", "films"), ("--set", "films=tout")):
                with self.assertRaises(SystemExit) as stop:
                    self.run_cli("rights", "alice", *bad)
                self.assertIn("attendu <bibliothèque>=all|play|view|none", str(stop.exception))
            with self.assertRaises(SystemExit) as stop:
                self.run_cli("rights", "alice", "--set", "musique=none")
            self.assertIn("bibliothèque inconnue : musique", str(stop.exception))
            with self.assertRaises(SystemExit) as stop:
                self.run_cli("rights", "chef", "--set", "films=none")
            self.assertIn("un administrateur a tous les droits", str(stop.exception))
            self.run_cli("rights", "alice", "--set", "livres=all")
            db = Database(Path(self.tmp) / "hub.db")
            self.assertEqual(accounts.grants(db, accounts.by_name(db, "alice")["id"]), {"films": (True, False, False)})

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


class Console(io.StringIO):
    """Une sortie qui retient ce qui a été écrit sans être vidé : ce que `docker logs` ne verrait pas encore."""

    def __init__(self):
        super().__init__()
        self.unflushed = ""
        self.flushes = []

    def write(self, text):
        self.unflushed += text
        return super().write(text)

    def flush(self):
        self.flushes.append(self.unflushed)
        self.unflushed = ""


def fake_config(tmp, **over):
    lib = types.SimpleNamespace(id="films", label="Films", kind="movies", path=Path(tmp) / "Films")
    return types.SimpleNamespace(**{"bind": "127.0.0.1", "port": 8080, "libraries": [lib], "ffmpeg": "/usr/bin/ffmpeg", "repo": tmp, "password": "", **over})


class TestAnnounce(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def said(self, cfg, auth=True, inbox_problem=None):
        out, err = Console(), io.StringIO()
        hub = types.SimpleNamespace(db=Database(Path(self.tmp) / "hub.db"), auth=lambda: auth, inbox=types.SimpleNamespace(check=lambda: inbox_problem))
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cli.announce(cfg, hub)
        self.console = out
        return out.getvalue(), err.getvalue()

    def test_the_address_and_each_library_are_flushed_at_once(self):
        self.said(fake_config(self.tmp))
        self.assertEqual(self.console.unflushed, "")
        self.assertEqual(len(self.console.flushes), 2)                                      # l'adresse, puis la bibliothèque
        self.assertTrue(self.console.flushes[0].startswith("media_hub "))

    def test_a_healthy_start_says_where_it_listens_and_warns_of_nothing(self):
        (Path(self.tmp) / "Films").mkdir()
        out, err = self.said(fake_config(self.tmp))
        self.assertIn("http://127.0.0.1:8080", out)
        self.assertIn("Films", out)
        self.assertNotIn("introuvable", out)
        self.assertEqual(err, "")

    def test_a_missing_folder_is_flagged_on_its_line(self):
        self.assertIn("(dossier introuvable)", self.said(fake_config(self.tmp))[0])

    def test_no_library_and_no_ffmpeg_are_warned_on_stderr(self):
        err = self.said(fake_config(self.tmp, libraries=[], ffmpeg=""))[1]
        self.assertIn("aucune bibliothèque", err)
        self.assertIn("ffmpeg introuvable", err)

    def test_an_inbox_that_cannot_be_written_is_said_at_startup(self):
        err = self.said(fake_config(self.tmp), inbox_problem="la boîte de réception (/x) n'est pas utilisable : refusé")[1]
        self.assertIn("ATTENTION : la boîte de réception (/x) n'est pas utilisable", err)

    def test_an_inbox_that_overlaps_the_data_folder_is_warned_through_the_same_line(self):
        from hub.inbox import Inbox
        data = Path(self.tmp) / "data"
        box = Inbox(types.SimpleNamespace(inbox=data, data=data, libraries=[], inbox_keep_free=0, inbox_max_uploads=2), None)
        err = self.said(fake_config(self.tmp), inbox_problem=box.check())[1]
        self.assertIn("ATTENTION : ", err)
        self.assertIn("HUB_INBOX", err)

    def test_a_short_hub_password_is_warned_a_long_one_is_not(self):
        self.assertIn("HUB_PASSWORD fait moins de 12 caractères", self.said(fake_config(self.tmp, password="court"))[1])
        self.assertNotIn("HUB_PASSWORD", self.said(fake_config(self.tmp, password="x" * accounts.MIN_PASSWORD))[1])
        self.assertNotIn("HUB_PASSWORD", self.said(fake_config(self.tmp, password=""))[1])

    def test_listening_on_the_network_without_an_account_is_warned(self):
        self.assertIn("ATTENTION", self.said(fake_config(self.tmp, bind="0.0.0.0"), auth=False)[1])
        self.assertEqual(self.said(fake_config(self.tmp, bind="0.0.0.0"), auth=True)[1], "")           # avec un compte : rien à signaler
        self.assertEqual(self.said(fake_config(self.tmp, bind="localhost"), auth=False)[1], "")        # en local : rien à signaler


class TestMain(unittest.TestCase):
    def serve(self, argv, serve_forever):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        hub = mock.Mock()
        hub.auth.return_value = True
        hub.inbox.check.return_value = None
        server = mock.Mock(hub=hub, serve_forever=serve_forever)
        out = self.console = Console()
        with mock.patch.object(sys, "argv", ["hub", *argv]), mock.patch.object(cli.config, "load", return_value=fake_config(tmp)),                 mock.patch.object(cli.config, "use_media_library"), mock.patch.dict(sys.modules, {"hub.server.app": types.SimpleNamespace(make_server=lambda cfg: server)}), contextlib.redirect_stdout(out):
            cli.main()
        return server, hub, out.getvalue()

    def test_it_scans_then_serves_and_closes_the_socket_on_ctrl_c(self):
        server, hub, out = self.serve([], mock.Mock(side_effect=KeyboardInterrupt))
        hub.first_scan.assert_called_once_with()
        hub.autoscan.assert_called_once_with()
        hub.inbox.keep_clean.assert_called_once_with()
        hub.inbox.migrate.assert_called_once_with()
        calls = [name for name, *_ in hub.inbox.method_calls]
        self.assertLess(calls.index("migrate"), calls.index("keep_clean"))                  # les dossiers sont renommés avant le premier nettoyage
        server.server_close.assert_called_once_with()
        self.assertIn("arrêt.", out)
        self.assertEqual(self.console.unflushed, "")

    def test_user_hands_over_to_the_account_commands(self):
        with mock.patch.object(cli, "user_command") as users:
            self.serve(["user", "list"], mock.Mock())
        users.assert_called_once_with(["list"])

if __name__ == "__main__":
    unittest.main()
