"""Les routes de la boîte de réception avec des sous-dossiers : `?dir=` à l'envoi, à la lecture et à la suppression, le champ `dir` de la liste, `extensions` de `?mine=1`, les refus (400 / 404 / 415…) sans dossier créé, les droits entre comptes, la boîte hors de HUB_DATA."""

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


class SubRoutes(InboxCase):
    def put(self, row, name, sub=None, body=b"abc"):
        target = "/api/inbox?name=" + quote(name, safe="") + ("&dir=" + quote(sub, safe="") if sub is not None else "")
        return call(self.hub, "POST", target, body, self.cookie(row))

    def url(self, folder, name, sub=None):
        return "/api/inbox/" + quote(folder, safe="") + "/" + quote(name, safe="") + ("?dir=" + quote(sub, safe="") if sub is not None else "")


class TestSendIntoSubfolders(SubRoutes):
    def test_a_file_goes_into_the_subfolder_and_the_reply_says_which(self):
        handler, reply = self.put(self.alice, "Film.mkv", "Vacances/2025", b"0123456789")
        self.assertEqual((reply.status, reply.json()), (201, {"name": "Film.mkv", "size": 10, "account": "alice", "dir": "Vacances/2025"}))
        self.assertEqual((self.inbox / "alice" / "Vacances" / "2025" / "Film.mkv").read_bytes(), b"0123456789")
        self.assertFalse(handler.close_connection)

    def test_without_dir_the_reply_has_an_empty_dir_and_the_file_stays_at_the_root(self):
        _, reply = self.send(self.alice, "Film.mkv")
        self.assertEqual(reply.json(), {"name": "Film.mkv", "size": 3, "account": "alice", "dir": ""})
        _, reply = self.put(self.alice, "Autre.mkv", "")
        self.assertEqual((reply.status, reply.json()["dir"]), (201, ""))
        self.assertEqual(self.everything(), ["alice/Autre.mkv", "alice/Film.mkv"])

    def test_the_same_folder_sent_twice_is_merged_whatever_the_case_and_same_names_are_numbered(self):
        self.put(self.alice, "a.mkv", "Dossier/Sous", b"1")
        _, reply = self.put(self.alice, "a.mkv", "dossier/SOUS", b"2")
        self.assertEqual(reply.json(), {"name": "a (2).mkv", "size": 1, "account": "alice", "dir": "Dossier/Sous"})
        self.assertEqual(self.everything(), ["alice/Dossier/Sous/a (2).mkv", "alice/Dossier/Sous/a.mkv"])

    def test_one_to_four_levels_work_and_the_fifth_is_a_400_in_french(self):
        for sub in ("a", "a/b", "a/b/c", "a/b/c/d"):
            self.assertEqual(self.put(self.alice, "x.mkv", sub)[1].status, 201, sub)
        handler, reply = self.put(self.alice, "x.mkv", "a/b/c/d/e", b"x" * 50)
        self.assertEqual((reply.status, reply.json()), (400, {"error": "dossiers trop profonds : 4 niveaux au plus"}))
        self.assertTrue(handler.close_connection)
        self.assertEqual(handler.rfile.tell(), 50)                                       # le corps n'a pas été lu
        self.assertFalse((self.inbox / "alice" / "a" / "b" / "c" / "d" / "e").exists())

    def test_a_bad_path_is_a_400_before_the_body_and_creates_nothing(self):
        for sub in ("a//b", "/a", "a/", ".", "..", "a/../b", "a\\b", "..\\..", " ", "a/ /b", "\x00\x00/..", "../.."):
            handler, reply = self.put(self.alice, "x.mkv", sub, b"x" * 50)
            self.assertEqual(reply.status, 400, sub)
            self.assertRegex(reply.json()["error"], r"segment|séparateur", sub)
            self.assertTrue(handler.close_connection, sub)
            self.assertEqual(handler.rfile.tell(), 50, sub)
        self.assertEqual(self.everything(), [])
        self.assertFalse((self.inbox / "alice").exists())

    def test_an_encoded_dot_dot_sent_encoded_twice_is_only_an_ordinary_name(self):
        _, reply = self.put(self.alice, "x.mkv", "%2e%2e")                              # l'URL en donne « %2e%2e » : jamais décodé une seconde fois
        self.assertEqual((reply.status, reply.json()["dir"]), (201, "%2e%2e"))
        self.assertEqual(self.everything(), ["alice/%2e%2e/x.mkv"])

    def test_the_url_decoded_traversal_is_a_400(self):
        for raw in ("/api/inbox?name=x.mkv&dir=%2e%2e", "/api/inbox?name=x.mkv&dir=..%2f..", "/api/inbox?name=x.mkv&dir=a%00b%2f..", "/api/inbox?name=x.mkv&dir=%5c", "/api/inbox?name=x.mkv&dir=a%2F%2Fb"):
            handler, reply = call(self.hub, "POST", raw, b"abc", self.cookie(self.alice))
            self.assertEqual(reply.status, 400, raw)
        self.assertEqual(self.everything(), [])

    def test_a_null_byte_hidden_and_reserved_names_are_cleaned_not_followed(self):
        _, reply = self.put(self.alice, "x.mkv", "a\x00b/.caché/CON")
        self.assertEqual((reply.status, reply.json()["dir"]), (201, "a_b/caché/_CON"))

    def test_accents_are_kept(self):
        _, reply = self.put(self.alice, "été.mkv", "Été/Léa (2)")
        self.assertEqual(reply.json()["dir"], "Été/Léa (2)")
        self.assertEqual(self.everything(), ["alice/Été/Léa (2)/été.mkv"])

    def test_a_refused_extension_or_name_creates_no_folder(self):
        for name in ("x.exe", "x", "script.sh", ".."):
            _, reply = self.put(self.alice, name, "a/b/c")
            self.assertIn(reply.status, (400, 415), name)
        self.assertFalse((self.inbox / "alice").exists())

    def test_a_refused_right_creates_no_folder(self):
        handler, reply = self.put(self.bob, "x.mkv", "a/b", b"x" * 50)
        self.assertEqual(reply.status, 403)
        self.assertTrue(handler.close_connection)
        self.assertEqual(self.everything(), [])
        self.assertFalse((self.inbox / "bob").exists())

    def test_no_session_is_a_401_and_creates_nothing(self):
        _, reply = call(self.hub, "POST", "/api/inbox?name=x.mkv&dir=a", b"abc")
        self.assertEqual(reply.status, 401)
        self.assertFalse(self.inbox.exists())

    def test_too_many_uploads_and_no_room_create_no_folder(self):
        self.send(self.alice, "premier.mkv")
        with mock.patch.object(self.hub.inbox, "max_uploads", 0):
            self.assertEqual(self.put(self.alice, "x.mkv", "a/b")[1].status, 429)
        with mock.patch.object(self.hub.inbox, "keep_free", 1 << 60):
            self.assertEqual(self.put(self.alice, "x.mkv", "a/b")[1].status, 507)
        self.assertEqual(self.everything(), ["alice/premier.mkv"])
        self.assertEqual(sorted(p.name for p in (self.inbox / "alice").iterdir()), ["premier.mkv"])

    def test_the_ceiling_of_subfolders_is_a_409_in_french_and_an_existing_folder_is_not_concerned(self):
        with mock.patch.object(inbox, "MAX_SUBFOLDERS", 2):
            self.put(self.alice, "x.mkv", "a")
            self.put(self.alice, "x.mkv", "b")
            handler, reply = self.put(self.alice, "x.mkv", "c", b"x" * 50)
            self.assertEqual((reply.status, reply.json()), (409, {"error": "trop de sous-dossiers dans ton dossier (2 au plus)"}))
            self.assertTrue(handler.close_connection)
            self.assertFalse((self.inbox / "alice" / "c").exists())
            self.assertEqual(self.put(self.alice, "y.mkv", "A")[1].status, 201)

    def test_a_subfolder_is_in_the_folder_of_the_sender_even_if_it_is_named_like_another_account(self):
        self.put(self.alice, "x.mkv", "bob")
        self.assertEqual(self.everything(), ["alice/bob/x.mkv"])
        self.assertFalse((self.inbox / "bob").exists())

    def test_an_administrator_sends_into_a_subfolder_of_their_own_folder(self):
        _, reply = self.put(self.boss, "x.mkv", "a/b")
        self.assertEqual(reply.json(), {"name": "x.mkv", "size": 3, "account": "boss", "dir": "a/b"})


class TestListAndRead(SubRoutes):
    def setUp(self):
        super().setUp()
        self.put(self.alice, "a.mkv", "Série/Saison 1", b"01234")
        self.put(self.alice, "b.mkv", None, b"67")
        self.put(self.boss, "c.mkv", "x", b"abc")

    def test_the_administrator_lists_everything_with_the_dir_of_each_file(self):
        files = self.get(self.boss, "/api/inbox")[1].json()["files"]
        self.assertEqual(sorted((f["account"], f["dir"], f["name"], f["size"]) for f in files), [("alice", "", "b.mkv", 2), ("alice", "Série/Saison 1", "a.mkv", 5), ("boss", "x", "c.mkv", 3)])
        for f in files:
            self.assertEqual(set(f), {"account", "user", "deleted", "name", "size", "date", "dir"})

    def test_mine_lists_only_the_own_files_with_used_and_the_accepted_extensions(self):
        reply = self.get(self.alice, "/api/inbox?mine=1")[1].json()
        self.assertEqual(sorted(reply), ["extensions", "files", "used"])
        self.assertEqual((reply["used"], sorted(f["name"] for f in reply["files"])), (7, ["a.mkv", "b.mkv"]))
        self.assertEqual(reply["extensions"], sorted(inbox.EXTENSIONS))
        self.assertIn(".mkv", reply["extensions"])
        self.assertNotIn(".exe", reply["extensions"])

    def test_extensions_are_for_the_senders_only(self):
        self.assertEqual(self.get(self.bob, "/api/inbox?mine=1")[1].status, 403)
        self.assertNotIn("extensions", self.get(self.boss, "/api/inbox")[1].json())

    def test_the_administrator_downloads_a_file_of_a_subfolder_by_its_dir(self):
        reply = self.get(self.boss, self.url("alice", "a.mkv", "Série/Saison 1"))[1]
        self.assertEqual((reply.status, reply.body), (200, b"01234"))
        self.assertIn("attachment", reply.headers["content-disposition"])
        self.assertEqual(self.get(self.boss, "/api/inbox/alice/a.mkv?dir=S%C3%A9rie%2FSaison%201")[1].body, b"01234")

    def test_the_url_of_a_file_at_the_root_does_not_change(self):
        self.assertEqual(self.get(self.boss, "/api/inbox/alice/b.mkv")[1].body, b"67")
        self.assertEqual(self.get(self.boss, "/api/inbox/alice/b.mkv?dir=")[1].body, b"67")

    def test_a_file_asked_in_the_wrong_place_is_a_404(self):
        for target in (self.url("alice", "a.mkv"), self.url("alice", "a.mkv", "Série"), self.url("alice", "b.mkv", "x"), self.url("boss", "a.mkv", "Série/Saison 1"),
                       self.url("alice", "a.mkv", "SÉRIE/Saison 1" if os.name != "nt" else "inconnu")):
            reply = self.get(self.boss, target)[1]
            self.assertEqual((reply.status, reply.json()), (404, {"error": "fichier introuvable"}), target)

    def test_every_traversal_in_dir_is_a_404_for_a_read_and_a_delete(self):
        self.put(self.bob_with_right(), "s.mkv", None, b"secret")
        for sub in ("..", ".", "../bob", "Série/..", "Série/../Série/Saison 1", "%2e%2e", "..%2f", "a\\b", "\\", "/", "/Série", "Série/", "Série//Saison 1", "\x00", "a/b/c/d/e", ".caché", "CON", " "):
            for target in (self.url("alice", "a.mkv", sub), self.url("alice", "s.mkv", sub)):
                self.assertEqual(self.get(self.boss, target)[1].status, 404, sub)
                self.assertEqual(self.delete(self.boss, target)[1].status, 404, sub)
        self.assertEqual(len(self.everything()), 4)

    def bob_with_right(self):
        accounts.set_can_upload(self.hub.db, self.bob["id"], True)
        return self.bob

    def test_an_account_reads_nothing_through_the_download_route_even_its_own_subfolder(self):
        self.assertEqual(self.get(self.alice, self.url("alice", "a.mkv", "Série/Saison 1"))[1].status, 403)


class TestDeleteInSubfolders(SubRoutes):
    def test_an_account_deletes_its_own_file_and_the_emptied_folders_go_but_not_its_own_folder(self):
        self.put(self.alice, "a.mkv", "x/y/z")
        reply = self.delete(self.alice, self.url("alice", "a.mkv", "x/y/z"))[1]
        self.assertEqual((reply.status, reply.json()), (200, {"ok": True}))
        self.assertEqual(self.everything(), [])
        self.assertEqual([p.name for p in self.inbox.iterdir()], ["alice"])
        self.assertEqual([p for p in (self.inbox / "alice").iterdir()], [])

    def test_a_folder_that_still_holds_a_file_stays(self):
        self.put(self.alice, "a.mkv", "x/y")
        self.put(self.alice, "b.mkv", "x/y")
        self.put(self.alice, "c.mkv", "x")
        self.assertEqual(self.delete(self.alice, self.url("alice", "a.mkv", "x/y"))[1].status, 200)
        self.assertEqual(self.everything(), ["alice/x/c.mkv", "alice/x/y/b.mkv"])
        self.assertEqual(self.delete(self.alice, self.url("alice", "b.mkv", "x/y"))[1].status, 200)
        self.assertEqual(self.everything(), ["alice/x/c.mkv"])
        self.assertTrue((self.inbox / "alice" / "x").is_dir())

    def test_the_administrator_deletes_a_file_of_any_account_in_a_subfolder(self):
        self.put(self.alice, "a.mkv", "x")
        self.assertEqual(self.delete(self.boss, self.url("alice", "a.mkv", "x"))[1].status, 200)
        self.assertEqual(self.everything(), [])

    def test_an_account_cannot_delete_or_read_the_folder_of_another(self):
        accounts.set_can_upload(self.hub.db, self.bob["id"], True)
        self.put(self.bob, "secret.mkv", "prive", b"secret")
        for sub in ("prive", "inconnu", "..", "../alice"):
            self.assertEqual(self.delete(self.alice, self.url("bob", "secret.mkv", sub))[1].status, 403, sub)
            self.assertEqual(self.get(self.alice, self.url("bob", "secret.mkv", sub))[1].status, 403, sub)
        self.assertEqual(self.everything(), ["bob/prive/secret.mkv"])
        self.assertEqual(self.get(self.alice, "/api/inbox?mine=1")[1].json()["files"], [])
        self.assertEqual(self.delete(self.bob, self.url("bob", "secret.mkv", "prive"))[1].status, 200)

    def test_an_account_without_the_right_deletes_nothing(self):
        self.put(self.alice, "a.mkv", "x")
        self.assertEqual(self.delete(self.bob, self.url("alice", "a.mkv", "x"))[1].status, 403)
        self.assertEqual(self.everything(), ["alice/x/a.mkv"])

    def test_deleting_twice_is_a_404(self):
        self.put(self.alice, "a.mkv", "x")
        self.delete(self.alice, self.url("alice", "a.mkv", "x"))
        self.assertEqual(self.delete(self.alice, self.url("alice", "a.mkv", "x"))[1].status, 404)

    def test_a_link_in_place_of_a_subfolder_is_neither_read_nor_deleted_through(self):
        self.put(self.alice, "a.mkv")
        self.put(self.boss, "s.mkv", None, b"secret")
        target = self.inbox / "boss"
        try:
            os.symlink(target, self.inbox / "alice" / "lien", target_is_directory=True)
        except (OSError, NotImplementedError):
            if sys.platform != "win32":
                self.skipTest("pas de liens symboliques ici")
            import _winapi
            _winapi.CreateJunction(str(target), str(self.inbox / "alice" / "lien"))
        self.assertEqual(self.get(self.boss, self.url("alice", "s.mkv", "lien"))[1].status, 404)
        self.assertEqual(self.delete(self.boss, self.url("alice", "s.mkv", "lien"))[1].status, 404)
        self.assertEqual(self.put(self.alice, "z.mkv", "lien")[1].status, 409)
        files = self.get(self.boss, "/api/inbox")[1].json()["files"]
        self.assertEqual(sorted((f["account"], f["dir"], f["name"]) for f in files), [("alice", "", "a.mkv"), ("boss", "", "s.mkv")])
        self.assertEqual((target / "s.mkv").read_bytes(), b"secret")


class TestSubfoldersOutsideTheDataFolder(unittest.TestCase):
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

    def test_the_subfolders_are_made_on_the_declared_volume_and_never_under_the_data_folder(self):
        _, reply = call(self.hub, "POST", "/api/inbox?name=Film.mkv&dir=Vacances%2F2025", b"0123456789", self.cookie(self.lea))
        self.assertEqual((reply.status, reply.json()["dir"]), (201, "Vacances/2025"))
        self.assertEqual((self.volume / "HubReception" / "Léa" / "Vacances" / "2025" / "Film.mkv").read_bytes(), b"0123456789")
        self.assertFalse((self.root / "data" / "inbox").exists())
        self.assertEqual([p.name for p in (self.root / "data").iterdir() if p.is_dir()], [])
        _, reply = call(self.hub, "GET", "/api/inbox/L%C3%A9a/Film.mkv?dir=Vacances%2F2025", headers=self.cookie(self.boss))
        self.assertEqual((reply.status, reply.body), (200, b"0123456789"))
        _, reply = call(self.hub, "DELETE", "/api/inbox/L%C3%A9a/Film.mkv?dir=Vacances%2F2025", headers=self.cookie(self.lea))
        self.assertEqual(reply.status, 200)
        self.assertEqual(list((self.volume / "HubReception" / "Léa").iterdir()), [])

    def test_a_path_that_climbs_out_never_writes_outside_the_account_folder(self):
        for sub in ("..", "../..", "../../data", "a/../../.."):
            self.assertEqual(call(self.hub, "POST", "/api/inbox?name=x.mkv&dir=" + quote(sub, safe=""), b"abc", self.cookie(self.lea))[1].status, 400, sub)
        self.assertEqual([p for p in self.root.rglob("x.mkv")] + [p for p in self.volume.rglob("x.mkv")], [])


if __name__ == "__main__":
    unittest.main()
