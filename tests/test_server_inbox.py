"""Les routes de la boîte de réception (`/api/inbox`) : qui peut envoyer, la réception d'un fichier, la liste, le téléchargement et la suppression par l'administrateur, le droit « Envoyer » d'un compte."""

from __future__ import annotations

import http.client
import io
import json
import shutil
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from tests.rig import MiniHub, Rig, call
from hub import accounts
from hub.inbox import Inbox, InboxError
from tests.test_server import AccountsCase, Client

PASSWORD = "un-mot-de-passe-ok"


class NoLength(Rig):
    """Une requête sans `Content-Length`."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        del self.headers["Content-Length"]


class Breaks(Rig):
    """Un client qui disparaît après `sent` octets du corps."""

    def __init__(self, *args, sent=5, **kw):
        super().__init__(*args, **kw)
        data = io.BytesIO(self.rfile.getvalue()[:sent])

        class Reader:
            def read(self, n):
                got = data.read(n)
                if not got:
                    raise ConnectionResetError("le client est parti")
                return got

        self.rfile = Reader()


class InboxCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "web").mkdir()
        self.hub = MiniHub(self.root)
        self.boss = accounts.create(self.hub.db, "boss", PASSWORD)
        self.alice = accounts.create(self.hub.db, "alice", PASSWORD)
        self.bob = accounts.create(self.hub.db, "bob", PASSWORD)
        accounts.set_can_upload(self.hub.db, self.alice["id"], True)
        self.inbox = self.root / "data" / "inbox"

    def cookie(self, row):
        return self.hub.cookie(accounts.by_id(self.hub.db, row["id"]))

    def send(self, row, name, body=b"abc", rig=Rig, headers=None, hub=None):
        target = "/api/inbox" + ("?name=" + quote(name, safe="") if name is not None else "")
        return call(hub or self.hub, "POST", target, body, {**(self.cookie(row) if row else {}), **(headers or {})}, rig)

    def stored(self, row):
        folder = self.inbox / row["id"]
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []

    def everything(self):
        """Tous les fichiers sous HUB_DATA/inbox, avec leur chemin relatif."""
        return sorted(p.relative_to(self.inbox).as_posix() for p in self.inbox.rglob("*") if p.is_file()) if self.inbox.is_dir() else []

    def get(self, row, target):
        return call(self.hub, "GET", target, headers=self.cookie(row))

    def delete(self, row, target, body=b""):
        return call(self.hub, "DELETE", target, body, self.cookie(row))


class TestUpload(InboxCase):
    def test_an_account_with_the_right_sends_a_file_into_its_own_folder(self):
        handler, reply = self.send(self.alice, "Film.mkv", b"0123456789")
        self.assertEqual((reply.status, reply.json()), (201, {"name": "Film.mkv", "size": 10, "account": self.alice["id"]}))
        self.assertEqual((self.inbox / self.alice["id"] / "Film.mkv").read_bytes(), b"0123456789")
        self.assertEqual(self.everything(), [f"{self.alice['id']}/Film.mkv"])
        self.assertFalse(handler.close_connection)

    def test_an_administrator_may_always_send(self):
        _, reply = self.send(self.boss, "Film.mkv")
        self.assertEqual(reply.status, 201)
        self.assertEqual(self.stored(self.boss), ["Film.mkv"])

    def test_an_account_without_the_right_gets_a_403_and_nothing_is_read_or_written(self):
        handler, reply = self.send(self.bob, "Film.mkv", b"x" * 50)
        self.assertEqual((reply.status, reply.json()), (403, {"error": "tu n'as pas le droit d'envoyer des fichiers"}))
        self.assertEqual(self.everything(), [])
        self.assertTrue(handler.close_connection)
        self.assertEqual(handler.rfile.tell(), 0)

    def test_the_right_is_read_at_each_request(self):
        self.assertEqual(self.send(self.bob, "a.mkv")[1].status, 403)
        accounts.set_can_upload(self.hub.db, self.bob["id"], True)
        self.assertEqual(self.send(self.bob, "a.mkv")[1].status, 201)
        accounts.set_can_upload(self.hub.db, self.bob["id"], False)
        self.assertEqual(self.send(self.bob, "b.mkv")[1].status, 403)

    def test_without_a_session_it_is_a_401(self):
        _, reply = self.send(None, "Film.mkv")
        self.assertEqual(reply.status, 401)
        self.assertEqual(self.everything(), [])

    def test_a_server_without_accounts_refuses_every_upload(self):
        hub = MiniHub(self.root / "ouvert")
        handler, reply = self.send(None, "Film.mkv", hub=hub)
        self.assertEqual((reply.status, reply.json()), (403, {"error": "pas d'envoi sans compte : crée un compte sur le serveur"}))
        self.assertTrue(handler.close_connection)
        self.assertFalse((self.root / "ouvert" / "data" / "inbox").exists())

    def test_the_name_is_required_and_a_bad_one_is_refused_before_the_body_is_read(self):
        for name, status in ((None, 400), ("", 400), ("..", 400), ("x.exe", 415), ("x", 415), ("script.sh", 415)):
            handler, reply = self.send(self.alice, name, b"x" * 50)
            self.assertEqual(reply.status, status, name)
            self.assertTrue(handler.close_connection, name)
            self.assertEqual(handler.rfile.tell(), 0, name)
        self.assertEqual(self.everything(), [])

    def test_a_trick_name_always_ends_up_inside_the_folder_of_the_account(self):
        names = ["../x.mkv", "..\\x.mkv", "../../../../etc/passwd.mkv", "..\\..\\..\\Windows\\win.mkv", "/etc/shadow.mkv", "C:\\boot.mkv", "NUL.mkv", "con.srt",
                 "a/../../b.mkv", "é" * 500 + ".mkv", "x" * 5000 + ".mkv", ".hidden.mkv", "..%2f..%2fy.mkv", "%2e%2e%2fz.mkv", "a\x00b.mkv", "nom\r\nHeader: x.mkv"]
        for name in names:
            _, reply = self.send(self.alice, name)
            self.assertEqual(reply.status, 201, name)
            self.assertNotIn("/", reply.json()["name"])
            self.assertNotIn("\\", reply.json()["name"])
            self.assertLessEqual(len(reply.json()["name"].encode()), 210)
        files = self.everything()
        self.assertEqual(len(files), len(names))
        self.assertTrue(all(f.startswith(self.alice["id"] + "/") and f.count("/") == 1 for f in files), files)
        stray = [p for p in self.root.rglob("*") if p.is_file() and p.suffix in (".mkv", ".srt")]
        self.assertEqual(len(stray), len(names))
        self.assertTrue(all(self.inbox in p.parents for p in stray), stray)

    def test_the_content_length_is_required(self):
        handler, reply = self.send(self.alice, "Film.mkv", rig=NoLength)
        self.assertEqual((reply.status, reply.json()), (411, {"error": "Content-Length obligatoire"}))
        self.assertTrue(handler.close_connection)
        handler, reply = self.send(self.alice, "Film.mkv", rig=NoLength, headers={"Transfer-Encoding": "chunked"})
        self.assertEqual(reply.status, 411)
        handler, reply = self.send(self.alice, "Film.mkv", headers={"Transfer-Encoding": "chunked"})
        self.assertEqual(reply.status, 411)
        self.assertEqual(self.everything(), [])

    def test_an_unreadable_negative_or_zero_content_length_is_a_400(self):
        for value in ("abc", "-4", "1.5", "0"):
            handler, reply = self.send(self.alice, "Film.mkv", b"", headers={"Content-Length": value})
            self.assertEqual(reply.status, 400, value)
            self.assertTrue(handler.close_connection, value)
        self.assertEqual(self.everything(), [])

    def test_too_little_free_space_is_a_507(self):
        self.hub.inbox.keep_free = 10**30
        handler, reply = self.send(self.alice, "a.mkv")
        self.assertEqual(reply.status, 507)
        self.assertIn("place", reply.json()["error"])
        self.assertNotIn(str(self.root), reply.json()["error"])
        self.assertEqual((handler.rfile.tell(), handler.close_connection), (0, True))

    def test_a_body_shorter_than_the_content_length_is_a_400_and_nothing_stays(self):
        handler, reply = self.send(self.alice, "Film.mkv", b"x" * 40, headers={"Content-Length": "100"})
        self.assertEqual(reply.status, 400)
        self.assertIn("interrompu", reply.json()["error"])
        self.assertTrue(handler.close_connection)
        self.assertEqual(self.everything(), [])

    def test_a_client_that_disappears_mid_upload_leaves_no_part(self):
        folder = self.inbox / self.alice["id"]
        handler, reply = self.send(self.alice, "Film.mkv", b"x" * 100, rig=Breaks)
        self.assertIsNone(reply)
        self.assertTrue(handler.close_connection)
        self.assertEqual(self.everything(), [])
        self.assertTrue(folder.is_dir())
        self.assertEqual(self.send(self.alice, "Film.mkv")[1].json()["name"], "Film.mkv")      # le nom est libre : pas de « (2) »

    def test_a_stalled_upload_that_times_out_leaves_no_part(self):
        class Stalls(Breaks):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                inner = self.rfile

                class Reader:
                    def read(self, n):
                        try:
                            return inner.read(n)
                        except ConnectionResetError:
                            raise TimeoutError("timed out")
                self.rfile = Reader()

        handler, reply = self.send(self.alice, "Film.mkv", b"x" * 100, rig=Stalls)
        self.assertEqual((reply, handler.close_connection, self.everything()), (None, True, []))

    def test_a_system_error_before_the_body_is_read_cuts_the_connection(self):
        with mock.patch.object(Inbox, "_open_part", side_effect=OSError(24, "Too many open files")):
            handler, reply = self.send(self.alice, "Film.mkv", b"x" * 50)
        self.assertEqual(reply.status, 500)
        self.assertTrue(handler.close_connection)
        self.assertEqual(handler.rfile.tell(), 0)
        self.assertEqual(self.everything(), [])

    def test_a_content_length_that_is_not_plain_digits_is_a_400_and_cuts_the_connection(self):
        for value in ("1_0", "+10", "0x10", "١٠", "10 000", "1e1", "9" * 30, "-1"):
            handler, reply = self.send(self.alice, "Film.mkv", b"x" * 10, headers={"Content-Length": value})
            self.assertEqual(reply.status, 400, value)
            self.assertTrue(handler.close_connection, value)
        self.assertEqual(self.everything(), [])

    def test_two_content_length_headers_are_a_400_and_cut_the_connection(self):
        class Twice(Rig):
            def __init__(self, *args, **kw):
                super().__init__(*args, **kw)
                self.headers["Content-Length"] = "3"                        # un second en-tête, comme deux lignes envoyées

        handler, reply = self.send(self.alice, "Film.mkv", b"abc", rig=Twice)
        self.assertEqual(handler.headers.get_all("Content-Length"), ["3", "3"])
        self.assertEqual(reply.status, 400)
        self.assertTrue(handler.close_connection)
        self.assertEqual(self.everything(), [])

    def test_an_existing_file_is_not_overwritten(self):
        self.assertEqual(self.send(self.alice, "Film.mkv", b"un")[1].json()["name"], "Film.mkv")
        self.assertEqual(self.send(self.alice, "Film.mkv", b"deux")[1].json()["name"], "Film (2).mkv")
        self.assertEqual((self.inbox / self.alice["id"] / "Film.mkv").read_bytes(), b"un")

    def test_too_many_uploads_at_once_is_a_429(self):
        with self.hub.inbox.admit("x", 1), self.hub.inbox.admit("y", 1):
            handler, reply = self.send(self.alice, "Film.mkv")
            self.assertEqual(reply.status, 429)
            self.assertTrue(handler.close_connection)
        self.assertEqual(self.send(self.alice, "Film.mkv")[1].status, 201)

    def test_an_inbox_error_keeps_its_status(self):
        for status in (400, 413, 415, 429, 507):
            with self.subTest(status=status):
                self.hub.inbox.receive = lambda *a, status=status: (_ for _ in ()).throw(InboxError(status, "refus"))
                self.assertEqual(self.send(self.alice, "a.mkv")[1].status, status)


class TestRight(InboxCase):
    def set(self, actor, target, body):
        return call(self.hub, "POST", f"/api/user/{target['id']}/upload", body, self.cookie(actor))

    def test_the_administrator_gives_and_takes_the_right(self):
        _, reply = self.set(self.boss, self.bob, {"upload": True})
        self.assertEqual((reply.status, reply.json()["can_upload"], reply.json()["id"]), (200, True, self.bob["id"]))
        self.assertEqual(self.send(self.bob, "a.mkv")[1].status, 201)
        _, reply = self.set(self.boss, self.bob, {"upload": False})
        self.assertEqual((reply.status, reply.json()["can_upload"]), (200, False))
        self.assertEqual(self.send(self.bob, "b.mkv")[1].status, 403)

    def test_a_user_cannot_give_the_right_not_even_to_himself(self):
        for actor in (self.alice, self.bob):
            _, reply = self.set(actor, self.bob, {"upload": True})
            self.assertEqual((reply.status, reply.json()), (403, {"error": "réservé à l'administrateur"}))
        self.assertEqual(self.send(self.bob, "a.mkv")[1].status, 403)

    def test_a_value_that_is_not_a_boolean_an_unknown_account_and_an_administrator_are_refused(self):
        for body in ({"upload": "oui"}, {"upload": 1}, {"upload": None}, {}, {"droit": True}):
            self.assertEqual(self.set(self.boss, self.bob, body)[1].status, 400, body)
        self.assertEqual(self.set(self.boss, {"id": "inconnu"}, {"upload": True})[1].status, 404)
        _, reply = self.set(self.boss, self.boss, {"upload": False})
        self.assertEqual((reply.status, reply.json()["error"]), (400, "un administrateur peut toujours envoyer"))
        self.assertEqual(self.send(self.boss, "a.mkv")[1].status, 201)

    def test_the_accounts_list_and_the_state_tell_who_may_send(self):
        _, reply = call(self.hub, "GET", "/api/users", headers=self.cookie(self.boss))
        flags = {u["name"]: u["can_upload"] for u in reply.json()["users"]}
        self.assertEqual(flags, {"boss": True, "alice": True, "bob": False})
        for row, expected in ((self.boss, True), (self.alice, True), (self.bob, False)):
            _, reply = call(self.hub, "GET", "/api/state", headers=self.cookie(row))
            self.assertEqual(reply.json()["user"]["can_upload"], expected, row["name"])

    def test_a_server_without_accounts_says_nobody_may_send(self):
        _, reply = call(MiniHub(self.root / "ouvert"), "GET", "/api/state")
        self.assertEqual((reply.json()["user"]["role"], reply.json()["user"]["can_upload"]), ("admin", False))

    def test_the_public_view_of_an_account_carries_the_right(self):
        row = accounts.by_id(self.hub.db, self.alice["id"])
        self.assertEqual(accounts.public(row)["can_upload"], True)
        self.assertEqual(accounts.public(accounts.by_id(self.hub.db, self.bob["id"]))["can_upload"], False)


class TestRead(InboxCase):
    def setUp(self):
        super().setUp()
        accounts.set_can_upload(self.hub.db, self.bob["id"], True)
        self.send(self.alice, "Film.mkv", b"0123456789")
        self.send(self.bob, "Chanson é.flac", b"musique")
        self.send(self.boss, "Livre.epub", b"livre")

    def test_the_administrator_lists_every_account(self):
        _, reply = self.get(self.boss, "/api/inbox")
        files = reply.json()["files"]
        self.assertEqual(reply.status, 200)
        self.assertEqual(sorted((f["user"], f["account"], f["name"], f["size"]) for f in files),
                         sorted([("alice", self.alice["id"], "Film.mkv", 10), ("bob", self.bob["id"], "Chanson é.flac", 7), ("boss", self.boss["id"], "Livre.epub", 5)]))
        self.assertTrue(all(isinstance(f["date"], int) and f["date"] > 0 for f in files))

    def test_the_files_of_a_deleted_account_stay_for_the_administrator_under_its_identifier(self):
        accounts.delete(self.hub.db, self.alice["id"])
        _, reply = self.get(self.boss, "/api/inbox")
        mine = [f for f in reply.json()["files"] if f["account"] == self.alice["id"]]
        self.assertEqual([(f["user"], f["name"]) for f in mine], [(self.alice["id"], "Film.mkv")])
        self.assertEqual(self.get(self.boss, f"/api/inbox/{self.alice['id']}/Film.mkv")[1].body, b"0123456789")

    def test_a_user_may_not_list_everything(self):
        for row in (self.alice, self.bob):
            _, reply = self.get(row, "/api/inbox")
            self.assertEqual((reply.status, reply.json()), (403, {"error": "réservé à l'administrateur"}))

    def test_mine_lists_only_the_files_of_the_caller_and_what_he_still_has_room_for(self):
        _, reply = self.get(self.alice, "/api/inbox?mine=1")
        self.assertEqual([(f["name"], f["size"]) for f in reply.json()["files"]], [("Film.mkv", 10)])
        self.assertEqual(reply.json(), {"files": reply.json()["files"], "used": 10})                   # ni quota ni taille maximale
        _, reply = self.get(self.boss, "/api/inbox?mine=1")
        self.assertEqual([f["name"] for f in reply.json()["files"]], ["Livre.epub"])

    def test_mine_is_for_those_who_have_the_right(self):
        carol = accounts.create(self.hub.db, "carol", PASSWORD)
        self.assertEqual(self.get(carol, "/api/inbox?mine=1")[1].status, 403)
        accounts.set_can_upload(self.hub.db, self.alice["id"], False)
        self.assertEqual(self.get(self.alice, "/api/inbox?mine=1")[1].status, 403)

    def test_the_list_is_empty_before_any_upload(self):
        hub = MiniHub(self.root / "autre")
        boss = accounts.create(hub.db, "boss", PASSWORD)
        _, reply = call(hub, "GET", "/api/inbox", headers=hub.cookie(boss))
        self.assertEqual(reply.json()["files"], [])

    def test_the_administrator_downloads_a_file_as_an_attachment(self):
        _, reply = self.get(self.boss, f"/api/inbox/{self.bob['id']}/{quote('Chanson é.flac')}")
        self.assertEqual((reply.status, reply.body), (200, b"musique"))
        self.assertEqual(reply.headers["content-disposition"], "attachment; filename*=UTF-8''" + quote("Chanson é.flac"))
        self.assertEqual(reply.headers["content-type"], "application/octet-stream")
        self.assertEqual(reply.headers["x-content-type-options"], "nosniff")

    def test_the_range_requests_of_a_resumed_download_work(self):
        _, reply = call(self.hub, "GET", f"/api/inbox/{self.alice['id']}/Film.mkv", headers={**self.cookie(self.boss), "Range": "bytes=2-5"})
        self.assertEqual((reply.status, reply.body), (206, b"2345"))

    def test_nobody_but_the_administrator_downloads(self):
        for row in (self.alice, self.bob):
            for account in (self.alice, self.bob):
                _, reply = self.get(row, f"/api/inbox/{account['id']}/Film.mkv")
                self.assertEqual(reply.status, 403, (row["name"], account["name"]))
                self.assertNotIn(b"0123456789", reply.body)

    def test_a_download_without_a_session_is_a_401(self):
        self.assertEqual(call(self.hub, "GET", f"/api/inbox/{self.alice['id']}/Film.mkv")[1].status, 401)
        self.assertEqual(call(self.hub, "GET", "/api/inbox")[1].status, 401)

    def test_the_media_token_does_not_open_the_inbox(self):
        token = self.hub.media_token(accounts.by_id(self.hub.db, self.boss["id"]))
        self.assertEqual(call(self.hub, "GET", f"/api/inbox/{self.alice['id']}/Film.mkv?token={token}")[1].status, 401)

    def test_a_path_that_leaves_the_inbox_is_a_404(self):
        secret = self.root / "data" / "secret.txt"
        secret.write_bytes(b"secret")
        (self.root / "hub.db").write_bytes(b"secret")
        alice = self.alice["id"]
        for target in (f"/api/inbox/{alice}/..", f"/api/inbox/{alice}/.", f"/api/inbox/{alice}/..%2f..%2fsecret.txt", f"/api/inbox/{alice}/%2e%2e%2f%2e%2e%2fhub.db",
                       f"/api/inbox/{alice}/..%5c..%5chub.db", f"/api/inbox/{alice}/../../hub.db", "/api/inbox/../../hub.db", "/api/inbox/..%2fhub.db", f"/api/inbox/{alice}/Film.mkv%00.txt",
                       f"/api/inbox/{alice}/Film.mkv.part", f"/api/inbox/{alice}/", f"/api/inbox/{alice}", f"/api/inbox/%2e%2e/Film.mkv", f"/api/inbox/{alice}%2f..%2f{self.bob['id']}/Film.mkv"):
            _, reply = self.get(self.boss, target)
            self.assertEqual(reply.status, 404, target)
            self.assertNotIn(b"secret", reply.body, target)

    def test_a_file_that_does_not_exist_is_a_404_with_a_message(self):
        _, reply = self.get(self.boss, f"/api/inbox/{self.alice['id']}/absent.mkv")
        self.assertEqual((reply.status, reply.json()), (404, {"error": "fichier introuvable"}))

    def test_the_inbox_is_never_probed_or_scanned(self):
        self.get(self.boss, "/api/inbox")
        self.get(self.boss, f"/api/inbox/{self.alice['id']}/Film.mkv")
        self.assertFalse([c for c in self.hub.jobs.method_calls if c[0] != "busy"])
        self.assertEqual(self.hub.images.method_calls, [])


class TestDelete(InboxCase):
    def setUp(self):
        super().setUp()
        accounts.set_can_upload(self.hub.db, self.bob["id"], True)
        self.send(self.alice, "Film.mkv", b"0123456789")
        self.send(self.bob, "Autre.mkv", b"autre")

    def test_the_administrator_deletes_any_file(self):
        _, reply = self.delete(self.boss, f"/api/inbox/{self.alice['id']}/Film.mkv")
        self.assertEqual((reply.status, reply.json()), (200, {"ok": True}))
        self.assertEqual((self.stored(self.alice), self.stored(self.bob)), ([], ["Autre.mkv"]))

    def test_an_account_deletes_its_own_files_and_only_those(self):
        _, reply = self.delete(self.alice, f"/api/inbox/{self.bob['id']}/Autre.mkv")
        self.assertEqual((reply.status, reply.json()), (403, {"error": "ce fichier n'est pas à toi"}))
        self.assertEqual(self.stored(self.bob), ["Autre.mkv"])
        _, reply = self.delete(self.alice, f"/api/inbox/{self.alice['id']}/Film.mkv")
        self.assertEqual(reply.status, 200)
        self.assertEqual(self.stored(self.alice), [])

    def test_an_account_that_lost_the_right_deletes_nothing(self):
        accounts.set_can_upload(self.hub.db, self.alice["id"], False)
        self.assertEqual(self.delete(self.alice, f"/api/inbox/{self.alice['id']}/Film.mkv")[1].status, 403)
        self.assertEqual(self.stored(self.alice), ["Film.mkv"])

    def test_a_missing_file_is_a_404_and_a_second_delete_too(self):
        target = f"/api/inbox/{self.alice['id']}/Film.mkv"
        self.assertEqual(self.delete(self.boss, target)[1].status, 200)
        self.assertEqual(self.delete(self.boss, target)[1].status, 404)

    def test_a_path_that_leaves_the_inbox_deletes_nothing(self):
        (self.root / "hub.db").write_bytes(b"secret")
        alice = self.alice["id"]
        for target in (f"/api/inbox/{alice}/..", f"/api/inbox/{alice}/..%2f..%2f..%2fhub.db", f"/api/inbox/../../hub.db", f"/api/inbox/{alice}/Film.mkv%00",
                       f"/api/inbox/{self.bob['id']}%2f..%2f{alice}/Film.mkv"):
            for row in (self.boss, self.alice):
                self.assertEqual(self.delete(row, target)[1].status, 404, (row["name"], target))
        self.assertEqual(sorted(self.everything()), sorted([f"{alice}/Film.mkv", f"{self.bob['id']}/Autre.mkv"]))
        self.assertTrue((self.root / "hub.db").exists())

    def test_without_a_session_it_is_a_401(self):
        self.assertEqual(call(self.hub, "DELETE", f"/api/inbox/{self.alice['id']}/Film.mkv")[1].status, 401)
        self.assertEqual(self.stored(self.alice), ["Film.mkv"])

    def test_a_body_sent_with_the_delete_is_thrown_away_so_the_connection_stays_clean(self):
        handler, reply = self.delete(self.alice, f"/api/inbox/{self.bob['id']}/Autre.mkv", b"x" * 20)
        self.assertEqual(reply.status, 403)
        self.assertEqual(handler.rfile.tell(), 20)
        handler, reply = self.delete(self.boss, f"/api/inbox/{self.alice['id']}/Film.mkv", b"x" * 20)
        self.assertEqual((reply.status, handler.rfile.tell()), (200, 20))

    def test_the_handler_answers_the_delete_method(self):
        from hub.server import app
        self.assertIs(app.Handler.do_DELETE, app.Handler.dispatch)


class TestOnARealServer(AccountsCase):
    """Le vrai serveur, de vraies connexions : un envoi complet, une coupure en cours d'envoi, un en-tête qui manque, un corps trop gros qu'on ne lit pas, et la méthode DELETE."""

    def setUp(self):
        super().setUp()
        self.alice = self.new_user("alice")
        self.assertEqual(self.admin.json(f"/api/user/{self.alice.account['id']}/upload", {"upload": True})[0], 200)
        self.inbox = self.hub.cfg.data / "inbox"

    def raw(self, path, headers, body=b"", token=None, close_after=None):
        """Une requête POST écrite à la main sur une socket ; rend (statut, JSON). `close_after` : n'envoie que ce nombre d'octets du corps puis ferme."""
        conn = http.client.HTTPConnection("127.0.0.1", self.run_.port, timeout=30)
        self.addCleanup(conn.close)
        conn.putrequest("POST", path, skip_accept_encoding=True)
        for key, value in {"Authorization": f"Bearer {token or self.alice.token}", **headers}.items():
            conn.putheader(key, value)
        conn.endheaders()
        conn.send(body if close_after is None else body[:close_after])
        if close_after is not None:
            conn.sock.shutdown(socket.SHUT_RDWR)
            return None
        response = conn.getresponse()
        answer = response.status, json.loads(response.read() or "null")
        conn.sock.settimeout(5)
        self.hung_up = conn.sock.recv(1) == b""                              # le serveur a fermé : rien d'autre ne viendra sur cette connexion
        return answer

    def folder(self):
        return self.inbox / self.alice.account["id"]

    def test_a_file_goes_in_comes_out_for_the_administrator_and_goes_away(self):
        data = bytes(range(256)) * 4096                                     # 1 Mo
        status, _, body = self.alice.request("POST", "/api/inbox?name=" + "Un%20Film%20%C3%A9t%C3%A9.mkv", body=data, headers={"Content-Type": "application/octet-stream"})
        self.assertEqual((status, json.loads(body)["name"]), (201, "Un Film été.mkv"))
        self.assertEqual((self.folder() / "Un Film été.mkv").read_bytes(), data)
        status, listing = self.admin.json("/api/inbox")
        self.assertEqual([(f["user"], f["name"], f["size"]) for f in listing["files"]], [("alice", "Un Film été.mkv", len(data))])
        status, headers, body = self.admin.get(f"/api/inbox/{self.alice.account['id']}/Un%20Film%20%C3%A9t%C3%A9.mkv")
        self.assertEqual((status, body, headers["Content-Disposition"].startswith("attachment;")), (200, data, True))
        self.assertEqual(self.alice.request("DELETE", f"/api/inbox/{self.alice.account['id']}/Un%20Film%20%C3%A9t%C3%A9.mkv")[0], 200)
        self.assertEqual(self.admin.json("/api/inbox")[1]["files"], [])
        self.assertEqual(sorted(p.name for p in self.folder().iterdir()), [])

    def test_a_client_that_hangs_up_half_way_leaves_no_part_and_frees_its_slot(self):
        self.hub.inbox.max_uploads = 1
        self.raw("/api/inbox?name=coupe.mkv", {"Content-Length": "1000000"}, b"x" * 1000000, close_after=5000)
        deadline, status = time.time() + 10, 429
        while status == 429 and time.time() < deadline:                              # tant que le serveur n'a pas vu la coupure, l'unique place est prise
            status = self.alice.request("POST", "/api/inbox?name=ok.mkv", body=b"abc")[0]
            time.sleep(0.05)
        self.assertEqual(status, 201)
        self.assertEqual(sorted(p.name for p in self.folder().iterdir()), ["ok.mkv"])

    def test_a_missing_content_length_is_a_411_and_a_chunked_body_too(self):
        self.assertEqual(self.raw("/api/inbox?name=a.mkv", {})[0], 411)
        self.assertEqual(self.raw("/api/inbox?name=a.mkv", {"Transfer-Encoding": "chunked"}, b"3\r\nabc\r\n0\r\n\r\n")[0], 411)
        self.assertEqual(list(self.inbox.rglob("*.mkv*")), [])

    def test_a_file_that_does_not_fit_on_the_disk_is_refused_before_its_body_is_sent(self):
        self.hub.inbox.keep_free = 10**30
        status, error = self.raw("/api/inbox?name=gros.mkv", {"Content-Length": str(10 * 1024 * 1024 * 1024)})            # 10 Go annoncés, rien envoyé
        self.assertEqual((status, "plus assez de place" in error["error"]), (507, True))
        self.assertEqual(self.alice.json("/api/state")[0], 200)

    def test_an_account_without_the_right_is_refused_before_its_body_is_sent(self):
        bob = self.new_user("bob")
        status, error = self.raw("/api/inbox?name=a.mkv", {"Content-Length": "10000000"}, token=bob.token)
        self.assertEqual((status, self.hung_up), (403, True))

    def test_a_doubled_content_length_cannot_smuggle_a_second_request(self):
        smuggled = b"GET /api/inbox HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + self.alice.token.encode() + b"\r\n\r\n"
        conn = http.client.HTTPConnection("127.0.0.1", self.run_.port, timeout=30)
        self.addCleanup(conn.close)
        conn.putrequest("POST", "/api/inbox?name=a.mkv", skip_accept_encoding=True)
        for key, value in (("Authorization", f"Bearer {self.alice.token}"), ("Content-Length", "3"), ("Content-Length", str(3 + len(smuggled)))):
            conn.putheader(key, value)
        conn.endheaders()
        conn.send(b"abc" + smuggled)
        response = conn.getresponse()
        self.assertEqual(response.status, 400)
        response.read()
        conn.sock.settimeout(5)
        self.assertEqual(conn.sock.recv(1), b"")                              # la connexion est fermée : rien ne lit la suite comme une requête
        self.assertEqual(list(self.inbox.rglob("*.mkv*")), [])

    def test_a_server_without_accounts_refuses_the_upload(self):
        self.hub.db.execute("DELETE FROM users")
        status, _, body = Client(self.run_.port).request("POST", "/api/inbox?name=a.mkv", body=b"abc")
        self.assertEqual((status, json.loads(body)["error"].startswith("pas d'envoi sans compte")), (403, True))

    def test_the_state_of_the_inbox_for_the_account(self):
        self.alice.request("POST", "/api/inbox?name=a.mkv", body=b"12345")
        status, mine = self.alice.json("/api/inbox?mine=1")
        self.assertEqual((status, mine["used"], [f["name"] for f in mine["files"]]), (200, 5, ["a.mkv"]))


if __name__ == "__main__":
    unittest.main()
