"""Les routes de la boîte de réception avec des dossiers nommés d'après les comptes : accents, noms qui se confondent, dossiers d'un compte supprimé, traversées dans le segment du dossier, droits entre comptes, boîte hors de HUB_DATA."""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from hub import accounts, inbox
from tests.rig import MiniHub, call
from tests.test_server_inbox import PASSWORD, InboxCase


class FolderRoutes(InboxCase):
    def make_user(self, name, upload=True):
        row = accounts.create(self.hub.db, name, PASSWORD)
        if upload:
            accounts.set_can_upload(self.hub.db, row["id"], True)
        return row

    def url(self, folder, name="Film.mkv"):
        return "/api/inbox/" + quote(folder, safe="") + "/" + quote(name, safe="")


class TestNamedFolders(FolderRoutes):
    def test_the_folder_is_the_name_of_the_account_with_its_accents_and_the_url_carries_it_encoded(self):
        lea = self.make_user("Léa Dupont")
        _, reply = self.send(lea, "Film.mkv", b"0123456789")
        self.assertEqual((reply.status, reply.json()), (201, {"name": "Film.mkv", "size": 10, "account": "Léa Dupont"}))
        self.assertEqual((self.inbox / "Léa Dupont" / "Film.mkv").read_bytes(), b"0123456789")
        self.assertNotIn(lea["id"], [p.name for p in self.inbox.iterdir()])
        listed = self.get(self.boss, "/api/inbox")[1].json()["files"]
        self.assertEqual([(f["account"], f["user"], f["deleted"]) for f in listed], [("Léa Dupont", "Léa Dupont", False)])
        reply = self.get(self.boss, "/api/inbox/" + quote(listed[0]["account"]) + "/Film.mkv")[1]          # %C3%A9 et %20 dans l'URL
        self.assertEqual((reply.status, reply.body), (200, b"0123456789"))
        self.assertEqual(self.get(self.boss, "/api/inbox/L%C3%A9a%20Dupont/Film.mkv")[1].status, 200)
        self.assertEqual(self.get(self.boss, "/api/inbox/L%c3%a9a%20Dupont/Film.mkv")[1].status, 200)

    def test_a_folder_with_spaces_and_brackets_is_reachable(self):
        self.make_user("Léa")
        (self.inbox / "Léa (2)").mkdir(parents=True)
        (self.inbox / "Léa (2)" / "Un Film (été).mkv").write_bytes(b"ok")
        reply = self.get(self.boss, self.url("Léa (2)", "Un Film (été).mkv"))[1]
        self.assertEqual((reply.status, reply.body), (200, b"ok"))

    def test_the_folder_follows_the_account_at_the_first_upload_and_stays_after(self):
        lea = self.make_user("Léa")
        self.assertEqual(self.send(lea, "a.mkv")[1].json()["account"], "Léa")
        self.hub.db.execute("UPDATE users SET name='Marie' WHERE id=?", (lea["id"],))
        _, reply = self.send(lea, "b.mkv")
        self.assertEqual(reply.json()["account"], "Léa")
        self.assertEqual(sorted(p.name for p in (self.inbox / "Léa").iterdir()), ["a.mkv", "b.mkv"])
        listed = self.get(self.boss, "/api/inbox")[1].json()["files"]
        self.assertEqual({(f["account"], f["user"]) for f in listed}, {("Léa", "Marie")})

    def test_names_that_become_the_same_once_cleaned_have_separate_folders_that_do_not_open_to_each_other(self):
        one, two = self.make_user("a:b"), self.make_user("a?b")
        self.assertEqual(self.send(one, "x.mkv", b"un")[1].json()["account"], "a_b")
        self.assertEqual(self.send(two, "x.mkv", b"deux")[1].json()["account"], "a_b (2)")
        self.assertEqual(self.get(self.boss, self.url("a_b", "x.mkv"))[1].body, b"un")
        self.assertEqual(self.get(self.boss, self.url("a_b (2)", "x.mkv"))[1].body, b"deux")
        self.assertEqual(self.delete(one, self.url("a_b (2)", "x.mkv"))[1].status, 403)
        self.assertEqual(self.delete(two, self.url("a_b", "x.mkv"))[1].status, 403)
        self.assertEqual(self.delete(two, self.url("a_b (2)", "x.mkv"))[1].status, 200)
        self.assertEqual(self.everything(), ["a_b/x.mkv"])

    def test_an_account_named_like_a_windows_device_gets_a_usable_folder(self):
        nul = self.make_user("NUL")
        self.assertEqual(self.send(nul, "a.mkv")[1].json()["account"], "_NUL")
        self.assertEqual(self.get(self.boss, self.url("_NUL", "a.mkv"))[1].status, 200)

    def test_a_deleted_account_leaves_its_folder_marked_as_such_and_a_new_account_of_the_same_name_does_not_inherit_it(self):
        lea = self.make_user("Léa")
        self.send(lea, "souvenir.mkv", b"12345")
        accounts.delete(self.hub.db, lea["id"])
        listed = self.get(self.boss, "/api/inbox")[1].json()["files"]
        self.assertEqual([(f["account"], f["user"], f["deleted"], f["name"]) for f in listed], [("Léa", "Léa", True, "souvenir.mkv")])
        self.assertEqual(self.get(self.boss, self.url("Léa", "souvenir.mkv"))[1].body, b"12345")
        again = self.make_user("Léa")
        self.assertEqual(self.send(again, "neuf.mkv")[1].json()["account"], "Léa (2)")
        self.assertEqual([f["name"] for f in self.get(again, "/api/inbox?mine=1")[1].json()["files"]], ["neuf.mkv"])
        self.assertEqual(self.get(again, self.url("Léa", "souvenir.mkv"))[1].status, 403)
        self.assertEqual(self.delete(again, self.url("Léa", "souvenir.mkv"))[1].status, 403)
        self.assertEqual((self.inbox / "Léa" / "souvenir.mkv").read_bytes(), b"12345")
        listed = {(f["account"], f["deleted"]) for f in self.get(self.boss, "/api/inbox")[1].json()["files"]}
        self.assertEqual(listed, {("Léa", True), ("Léa (2)", False)})

    def test_the_administrator_deletes_a_file_of_a_deleted_account_and_the_folder_stays_for_the_next_ones(self):
        lea = self.make_user("Élodie")
        self.send(lea, "a.mkv", b"a")
        self.send(lea, "b.mkv", b"b")
        accounts.delete(self.hub.db, lea["id"])
        self.assertEqual(self.delete(self.boss, self.url("Élodie", "a.mkv"))[1].status, 200)
        self.assertEqual(self.everything(), ["Élodie/b.mkv"])
        listed = self.get(self.boss, "/api/inbox")[1].json()["files"]
        self.assertEqual([(f["account"], f["name"], f["deleted"]) for f in listed], [("Élodie", "b.mkv", True)])
        self.assertEqual(self.delete(self.boss, self.url("Élodie", "a.mkv"))[1].status, 404)

    def test_the_folders_of_before_are_found_again_after_the_migration_with_their_new_name(self):
        """Un dossier nommé d'après l'identifiant, puis renommé au démarrage : la liste, le téléchargement et la suppression suivent."""
        old = self.inbox / self.alice["id"]
        old.mkdir(parents=True)
        (old / "ancien.mkv").write_bytes(b"avant")
        self.assertEqual(self.hub.inbox.migrate(), 1)
        self.assertFalse(old.exists())
        self.assertEqual([(f["account"], f["user"]) for f in self.get(self.boss, "/api/inbox")[1].json()["files"]], [("alice", "alice")])
        self.assertEqual(self.get(self.boss, self.url("alice", "ancien.mkv"))[1].body, b"avant")
        self.assertEqual(self.get(self.alice, "/api/inbox?mine=1")[1].json()["used"], 5)
        self.assertEqual(self.delete(self.alice, self.url("alice", "ancien.mkv"))[1].status, 200)


class TestSegmentOfTheFolder(FolderRoutes):
    def setUp(self):
        super().setUp()
        self.send(self.alice, "Film.mkv", b"0123456789")
        (self.root / "hub.db").write_bytes(b"secret")
        (self.root / "secret.mkv").write_bytes(b"secret")

    BAD = ("..", ".", "%2e%2e", "%2E%2E", "..%2f", "%2e%2e%2f", "..%5c", "%2e%2e%5c", "alice%2f..", "alice%5c..", "alice%00", "%00", "%2e%2e%2f%2e%2e%2fsecret.mkv", "..%2f..%2fsecret",
           ".caché", "%2ecach%C3%A9", "NUL", "con", "COM1", "nul.txt", "alice%20", "%20alice", "alice.", "C%3A", "alice%3Astream", "inconnu")

    def test_nothing_that_is_not_a_plain_folder_name_opens_for_the_administrator(self):
        for segment in self.BAD:
            for target in (f"/api/inbox/{segment}/Film.mkv", f"/api/inbox/{segment}/secret.mkv", f"/api/inbox/{segment}/..%2fhub.db"):
                _, reply = self.get(self.boss, target)
                self.assertIn(reply.status, (404,), target)
                self.assertNotIn(b"secret", reply.body, target)
                self.assertNotIn(b"0123456789", reply.body, target)

    def test_nor_does_it_delete_anything(self):
        for segment in self.BAD:
            for row in (self.boss, self.alice):
                reply = self.delete(row, f"/api/inbox/{segment}/Film.mkv")[1]
                self.assertIn(reply.status, (403, 404), (segment, row["name"]))
        self.assertEqual(sorted(self.everything()), ["alice/Film.mkv"])
        self.assertEqual((self.root / "secret.mkv").read_bytes(), b"secret")

    def test_an_encoded_slash_cannot_make_one_segment_of_two(self):
        for target in ("/api/inbox/alice%2f..%2fbob/Film.mkv", "/api/inbox/alice/..%2falice/Film.mkv", "/api/inbox/alice/Film.mkv%2f..%2f..%2fsecret.mkv"):
            self.assertEqual(self.get(self.boss, target)[1].status, 404, target)
            self.assertEqual(self.delete(self.boss, target)[1].status, 404, target)
        self.assertEqual(self.everything(), ["alice/Film.mkv"])

    def test_a_double_encoding_is_just_a_name_that_does_not_exist(self):
        for segment in ("%252e%252e", "%252f", "..%252f.."):
            self.assertEqual(self.get(self.boss, f"/api/inbox/{segment}/Film.mkv")[1].status, 404, segment)

    def test_a_hidden_folder_that_exists_is_neither_served_nor_listed(self):
        (self.inbox / ".caché").mkdir()
        (self.inbox / ".caché" / "x.mkv").write_bytes(b"x")
        self.assertEqual(self.get(self.boss, "/api/inbox/.cach%C3%A9/x.mkv")[1].status, 404)
        self.assertEqual([f["account"] for f in self.get(self.boss, "/api/inbox")[1].json()["files"]], ["alice"])

    def test_a_symbolic_link_in_place_of_a_folder_is_not_followed(self):
        outside = self.root / "dehors"
        outside.mkdir()
        (outside / "x.mkv").write_bytes(b"x")
        try:
            os.symlink(outside, self.inbox / "mallory", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("pas de liens symboliques ici (droit refusé sous Windows)")
        self.assertEqual(self.get(self.boss, self.url("mallory", "x.mkv"))[1].status, 404)
        self.assertEqual(self.delete(self.boss, self.url("mallory", "x.mkv"))[1].status, 404)
        self.assertEqual([f["account"] for f in self.get(self.boss, "/api/inbox")[1].json()["files"]], ["alice"])
        self.assertEqual((outside / "x.mkv").read_bytes(), b"x")

    def test_a_symbolic_link_to_a_file_inside_a_folder_is_not_followed(self):
        try:
            os.symlink(self.root / "secret.mkv", self.inbox / "alice" / "lien.mkv")
        except (OSError, NotImplementedError):
            self.skipTest("pas de liens symboliques ici (droit refusé sous Windows)")
        _, reply = self.get(self.boss, self.url("alice", "lien.mkv"))
        self.assertEqual(reply.status, 404)
        self.assertNotIn(b"secret", reply.body)
        self.assertEqual(self.delete(self.boss, self.url("alice", "lien.mkv"))[1].status, 404)
        self.assertEqual((self.root / "secret.mkv").read_bytes(), b"secret")

    def test_a_windows_junction_in_place_of_a_folder_is_not_followed(self):
        if sys.platform != "win32":
            self.skipTest("les jonctions n'existent que sous Windows")
        import _winapi
        outside = self.root / "dehors"
        outside.mkdir()
        (outside / "x.mkv").write_bytes(b"x")
        _winapi.CreateJunction(str(outside), str(self.inbox / "mallory"))
        self.assertEqual(self.get(self.boss, self.url("mallory", "x.mkv"))[1].status, 404)
        self.assertEqual(self.delete(self.boss, self.url("mallory", "x.mkv"))[1].status, 404)
        self.assertEqual([f["account"] for f in self.get(self.boss, "/api/inbox")[1].json()["files"]], ["alice"])
        mallory = self.make_user("mallory")
        self.hub.db.execute("UPDATE users SET inbox_dir='mallory' WHERE id=?", (mallory["id"],))
        with mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(self.send(mallory, "y.mkv")[1].status, 503)
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["x.mkv"])

    def test_the_message_of_a_refusal_is_in_french_and_says_nothing_of_the_disk(self):
        for target in ("/api/inbox/%2e%2e/Film.mkv", "/api/inbox/inconnu/Film.mkv", "/api/inbox/alice/absent.mkv"):
            _, reply = self.get(self.boss, target)
            self.assertEqual((reply.status, reply.json()), (404, {"error": "fichier introuvable"}), target)


class TestRightsBetweenFolders(FolderRoutes):
    def setUp(self):
        super().setUp()
        accounts.set_can_upload(self.hub.db, self.bob["id"], True)
        self.send(self.alice, "Film.mkv", b"0123456789")
        self.send(self.bob, "Autre.mkv", b"autre")

    def test_nobody_but_the_administrator_reads_a_folder_not_even_their_own(self):
        for row in (self.alice, self.bob):
            for folder in ("alice", "bob"):
                _, reply = self.get(row, self.url(folder, "Film.mkv" if folder == "alice" else "Autre.mkv"))
                self.assertEqual(reply.status, 403, (row["name"], folder))
                self.assertNotIn(b"0123456789", reply.body)

    def test_an_account_deletes_in_its_own_folder_only_and_by_its_exact_name(self):
        self.assertEqual(self.delete(self.alice, self.url("bob", "Autre.mkv"))[1].status, 403)
        self.assertEqual(self.delete(self.alice, self.url("ALICE", "Film.mkv"))[1].status, 403)       # une autre graphie du même dossier n'est pas le sien
        self.assertEqual(self.delete(self.alice, self.url("alice ", "Film.mkv"))[1].status, 403)
        self.assertEqual(self.delete(self.alice, self.url("inconnu", "Film.mkv"))[1].status, 403)
        self.assertEqual(sorted(self.everything()), ["alice/Film.mkv", "bob/Autre.mkv"])
        self.assertEqual(self.delete(self.alice, self.url("alice", "Film.mkv"))[1].status, 200)
        self.assertEqual(self.everything(), ["bob/Autre.mkv"])

    def test_an_account_that_never_sent_anything_has_no_folder_and_deletes_nothing(self):
        carol = self.make_user("carol")
        self.assertEqual(self.get(carol, "/api/inbox?mine=1")[1].json(), {"files": [], "used": 0})
        for folder in ("carol", "alice", "bob", ""):
            self.assertIn(self.delete(carol, self.url(folder or "x", "Film.mkv"))[1].status, (403, 404))
        self.assertEqual(sorted(self.everything()), ["alice/Film.mkv", "bob/Autre.mkv"])
        self.assertFalse((self.inbox / "carol").exists())                                              # lire ne crée rien

    def test_each_account_lists_and_counts_only_its_own_folder(self):
        for row, name, used in ((self.alice, "Film.mkv", 10), (self.bob, "Autre.mkv", 5)):
            reply = self.get(row, "/api/inbox?mine=1")[1].json()
            self.assertEqual(([f["name"] for f in reply["files"]], reply["used"]), ([name], used))


class TestInboxOutsideTheDataFolder(unittest.TestCase):
    """HUB_INBOX hors de HUB_DATA, de bout en bout : envoi, liste, téléchargement."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.volume = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(shutil.rmtree, self.volume, True)
        (self.root / "web").mkdir()
        self.hub = MiniHub(self.root, inbox=self.volume / "HubReception")
        self.boss = accounts.create(self.hub.db, "boss", PASSWORD)
        self.lea = accounts.create(self.hub.db, "Léa", PASSWORD)
        accounts.set_can_upload(self.hub.db, self.lea["id"], True)

    def cookie(self, row):
        return self.hub.cookie(accounts.by_id(self.hub.db, row["id"]))

    def test_the_files_go_to_the_declared_volume_and_nothing_goes_under_the_data_folder(self):
        _, reply = call(self.hub, "POST", "/api/inbox?name=Film.mkv", b"0123456789", self.cookie(self.lea))
        self.assertEqual((reply.status, reply.json()["account"]), (201, "Léa"))
        self.assertEqual((self.volume / "HubReception" / "Léa" / "Film.mkv").read_bytes(), b"0123456789")
        self.assertFalse((self.root / "data" / "inbox").exists())
        _, reply = call(self.hub, "GET", "/api/inbox/L%C3%A9a/Film.mkv", headers=self.cookie(self.boss))
        self.assertEqual((reply.status, reply.body), (200, b"0123456789"))
        self.assertEqual([f["account"] for f in call(self.hub, "GET", "/api/inbox", headers=self.cookie(self.boss))[1].json()["files"]], ["Léa"])

    def test_the_free_space_is_measured_on_that_volume(self):
        seen = []

        def usage(path):
            seen.append(Path(path))
            return mock.Mock(free=10**12)

        with mock.patch.object(inbox.shutil, "disk_usage", usage):
            call(self.hub, "POST", "/api/inbox?name=a.mkv", b"abc", self.cookie(self.lea))
        self.assertEqual(seen, [self.volume / "HubReception"])

    def test_an_unwritable_volume_is_a_503_in_french_without_the_path_and_the_hub_keeps_running(self):
        (self.volume / "fichier").write_bytes(b"x")
        self.hub.inbox.root = self.volume / "fichier" / "boite"
        with mock.patch.object(sys, "stderr", io.StringIO()):
            handler, reply = call(self.hub, "POST", "/api/inbox?name=a.mkv", b"abc", self.cookie(self.lea))
        self.assertEqual(reply.status, 503)
        self.assertIn("inutilisable", reply.json()["error"])
        self.assertNotIn(str(self.volume), reply.json()["error"])
        self.assertTrue(handler.close_connection)
        self.assertEqual(call(self.hub, "GET", "/api/inbox", headers=self.cookie(self.boss))[1].status, 200)

    def test_a_permission_error_while_storing_is_a_503_in_french_with_no_path_for_the_client(self):
        denied = PermissionError(13, "Permission denied", str(self.volume / "HubReception" / "Léa" / "Film.mkv.part"))
        with mock.patch("hub.inbox.open", side_effect=denied, create=True):
            handler, reply = call(self.hub, "POST", "/api/inbox?name=Film.mkv", b"abc", self.cookie(self.lea))
        self.assertEqual(reply.status, 503)
        self.assertIn("inutilisable", reply.json()["error"])
        for part in (str(self.volume), "HubReception", "Permission denied", "Errno"):
            self.assertNotIn(part, reply.json()["error"])
        self.assertIn("HubReception", handler.stderr)                                  # le journal dit où
        self.assertEqual(call(self.hub, "GET", "/api/inbox", headers=self.cookie(self.boss))[1].json()["files"], [])

    def test_an_unexpected_system_error_that_reaches_the_500_does_not_show_the_inbox_path_either(self):
        denied = PermissionError(13, "Permission denied", str(self.volume / "HubReception" / "Léa" / "Film.mkv.part"))
        with mock.patch.object(inbox.Inbox, "receive", side_effect=denied), mock.patch.object(sys, "stderr", io.StringIO()):
            _, reply = call(self.hub, "POST", "/api/inbox?name=Film.mkv", b"abc", self.cookie(self.lea))
        self.assertEqual(reply.status, 500)
        self.assertNotIn(str(self.volume), reply.json()["error"])
        self.assertIn("…", reply.json()["error"])


if __name__ == "__main__":
    unittest.main()
