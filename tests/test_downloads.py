"""Où en sont les téléchargements d'un compte (`GET /api/downloads`), pour que l'interface les lance un par un : un vrai serveur (le registre seul est dans `test_activity`).

Une tentative est reconnue par le `dl` que l'interface ajoute au lien (`/media/<id>/file?download=1&dl=<id>`)."""

from __future__ import annotations

import http.client
import shutil
import socket
import tempfile
import time
import unittest

from tests.test_activity import DL, OTHER, Clock
from tests.test_server import AccountsCase, Client, Running
from hub import accounts
from hub.activity import KEEP_SECONDS, Activity


# ---------------------------------------------------------------------------
# Un vrai serveur
# ---------------------------------------------------------------------------
class LiveDownloads(unittest.TestCase):
    """Un serveur ouvert (personne n'est connecté : l'administrateur par défaut), l'horloge du registre à la main."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.run_ = Running(cls.tmp)
        cls.hub = cls.run_.hub
        cls.api = Client(cls.run_.port)
        found = next(i for i in cls.api.json("/api/library/musique?limit=600")[1]["items"] if i["title"] == "Discovery")
        cls.track = cls.api.json(f"/api/item/{found['id']}")[1]["entries"][0]["id"]
        cls.path = cls.hub.path_of(cls.hub.entry(cls.track))[1]
        cls.size = cls.path.stat().st_size

    @classmethod
    def tearDownClass(cls):
        cls.run_.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.clock = Clock(time.time())
        self.hub.activity = Activity(self.clock)

    def url(self, dl=DL):
        return f"/media/{self.track}/file?download=1&dl={dl}"

    def states(self, *ids):
        status, data = self.api.json("/api/downloads?dl=" + ",".join(ids))
        self.assertEqual(status, 200, data)
        return data["downloads"]

    def wait_for(self, check, what, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            if check():
                return
            time.sleep(0.05)
        self.fail(what)

    def big(self):
        """Le fichier grossi bien au-delà des tampons de la connexion : l'envoi reste en cours tant que le client ne lit pas. Rend la nouvelle taille."""
        original = self.path.read_bytes()
        self.addCleanup(self.path.write_bytes, original)
        with open(self.path, "ab") as handle:
            handle.write(b"\0" * (96 << 20))
        return self.path.stat().st_size

    def cut(self, dl=DL):
        """Un téléchargement lancé, vu en cours, puis coupé par le client au milieu ; rend les octets que le serveur dit avoir envoyés."""
        conn = http.client.HTTPConnection("127.0.0.1", self.run_.port, timeout=30)
        conn.request("GET", self.url(dl))
        response = conn.getresponse()
        response.read(1024)
        self.wait_for(lambda: self.states(dl)[dl]["state"] == "running", "le téléchargement n'est pas en cours")
        conn.sock.shutdown(socket.SHUT_RDWR)
        conn.close()
        self.wait_for(lambda: self.states(dl)[dl]["state"] == "stopped", "le téléchargement coupé n'est pas « stopped »")
        return self.states(dl)[dl]["bytes"]

    def test_a_finished_download_is_done(self):
        status, _, body = self.api.get(self.url())
        self.assertEqual((status, len(body)), (200, self.size))
        self.assertEqual(self.states(DL), {DL: {"state": "done", "bytes": self.size, "size": self.size}})

    def test_a_download_cut_in_the_middle_is_stopped(self):
        size = self.big()
        sent = self.cut()
        self.assertTrue(0 < sent < size, sent)
        self.assertEqual(self.states(DL)[DL]["size"], size)

    def test_a_cut_download_resumed_by_range_to_the_end_is_done(self):
        size = self.big()
        sent = self.cut()
        status, _, body = self.api.get(self.url(), headers={"Range": f"bytes={sent}-"})
        self.assertEqual((status, len(body)), (206, size - sent))
        self.assertEqual(self.states(DL), {DL: {"state": "done", "bytes": size, "size": size}})

    def test_two_attempts_of_the_same_file_are_distinct(self):
        self.big()
        self.cut(OTHER)
        self.api.get(self.url(DL))
        found = self.states(DL, OTHER)
        self.assertEqual((found[DL]["state"], found[OTHER]["state"]), ("done", "stopped"))

    def test_a_download_already_done_a_minute_ago_does_not_make_a_new_attempt_done(self):
        self.api.get(self.url(DL))
        self.clock.now += 60
        self.assertEqual(self.states(OTHER), {OTHER: {"state": "unknown"}})

    def test_the_outcome_is_still_there_after_ten_minutes_and_gone_after_the_delay(self):
        self.api.get(self.url())
        self.clock.now += 600
        self.assertEqual(self.states(DL)[DL]["state"], "done")
        self.clock.now += KEEP_SECONDS
        self.assertEqual(self.states(DL), {DL: {"state": "unknown"}})

    def test_a_hostile_dl_is_ignored_and_the_download_works(self):
        hostile = ("short", "a" * 33, "abcd%2F..%2Fefgh", "abcdefgh%00", "%3Cscript%3Eabc", "abcdefgh,ijklmnop")
        for dl in hostile:
            status, _, body = self.api.get(self.url(dl))
            self.assertEqual((status, len(body)), (200, self.size), dl)
        self.assertEqual(self.hub.activity.attempts, {})
        self.assertEqual(self.states("a" * 32, "abcdefgh", "ijklmnop"), {"a" * 32: {"state": "unknown"}, "abcdefgh": {"state": "unknown"}, "ijklmnop": {"state": "unknown"}})

    def test_the_route_drops_malformed_ids_and_takes_at_most_fifty(self):
        self.api.get(self.url())
        self.assertEqual(self.states(DL, "x", "../etc", ""), {DL: {"state": "done", "bytes": self.size, "size": self.size}})
        ids = [f"attempt{n:05d}" for n in range(50)]
        self.assertEqual(len(self.states(*ids)), 50)
        status, data = self.api.json("/api/downloads?dl=" + ",".join(ids + [DL]))
        self.assertEqual(status, 400)
        self.assertIn("50", data["error"])
        self.assertEqual(self.api.json("/api/downloads")[1], {"downloads": {}})

    def test_head_creates_nothing(self):
        self.assertEqual(self.api.request("HEAD", self.url())[0], 200)
        self.assertEqual(self.api.get(self.url(), headers={"Range": "bytes=99999999-"})[0], 416)
        self.assertEqual(self.api.get(f"/media/nope/file?download=1&dl={DL}")[0], 404)
        self.assertEqual(self.states(DL), {DL: {"state": "unknown"}})

    def test_without_dl_nothing_changes(self):
        status, headers, body = self.api.get(f"/media/{self.track}/file?download=1")
        self.assertEqual((status, len(body)), (200, self.size))
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(self.hub.activity.attempts, {})
        line, = self.api.json("/api/activity")[1]["transfers"]
        self.assertEqual((line["kind"], line["bytes"], line["connections"]), ("download", self.size, 0))

    def test_with_dl_the_activity_line_is_unchanged(self):
        self.api.get(self.url())
        line, = self.api.json("/api/activity")[1]["transfers"]
        self.assertEqual((line["kind"], line["bytes"], line["connections"]), ("download", self.size, 0))

    def test_a_dl_on_a_plain_read_is_not_followed(self):
        self.api.get(f"/media/{self.track}/file?dl={DL}", headers={"Range": "bytes=0-9"})
        self.assertEqual(self.states(DL), {DL: {"state": "unknown"}})


class TestDownloadsAccounts(AccountsCase):
    def test_each_account_sees_only_its_own_attempts_and_nothing_else(self):
        alice, bob = self.new_user("alice"), self.new_user("bob")
        found = next(i for i in self.admin.json("/api/library/musique?limit=600")[1]["items"] if i["title"] == "Discovery")
        track = self.admin.json(f"/api/item/{found['id']}")[1]["entries"][0]["id"]
        self.assertEqual(alice.get(f"/media/{track}/file?download=1&dl={DL}", headers={"X-Forwarded-For": "8.8.8.8"})[0], 200)
        self.assertEqual(bob.json(f"/api/downloads?dl={DL}"), (200, {"downloads": {DL: {"state": "unknown"}}}))
        status, _, body = alice.get(f"/api/downloads?dl={DL}")
        self.assertEqual(status, 200)
        self.assertIn('"state":"done"', body.decode())
        for secret in ("8.8.8.8", "127.0.0.1", str(self.run_.root), track, "Discovery", alice.token):
            self.assertNotIn(secret, body.decode())
        self.assertEqual(self.anon.request("GET", f"/api/downloads?dl={DL}")[0], 401)

    def test_the_same_id_in_two_accounts_is_two_attempts(self):
        alice, bob = self.new_user("alice"), self.new_user("bob")
        found = next(i for i in self.admin.json("/api/library/musique?limit=600")[1]["items"] if i["title"] == "Discovery")
        track = self.admin.json(f"/api/item/{found['id']}")[1]["entries"][0]["id"]
        alice.get(f"/media/{track}/file?download=1&dl={DL}")
        bob.get(f"/media/{track}/file?download=1&dl={DL}", headers={"Range": "bytes=0-9"})
        self.assertEqual(alice.json(f"/api/downloads?dl={DL}")[1]["downloads"][DL]["state"], "done")
        self.assertEqual(bob.json(f"/api/downloads?dl={DL}")[1]["downloads"][DL], {"state": "stopped", "bytes": 10, "size": alice.json(f"/api/downloads?dl={DL}")[1]["downloads"][DL]["size"]})

    def test_a_dl_does_not_grant_the_right_to_download(self):
        bob = self.new_user("bob")
        found = next(i for i in self.admin.json("/api/library/musique?limit=600")[1]["items"] if i["title"] == "Discovery")
        track = self.admin.json(f"/api/item/{found['id']}")[1]["entries"][0]["id"]
        accounts.set_rights(self.hub.db, bob.account["id"], {"musique"}, {"musique": {"download": False}})
        self.assertEqual(bob.get(f"/media/{track}/file?download=1&dl={DL}")[0], 403)
        self.assertEqual(bob.json(f"/api/downloads?dl={DL}")[1], {"downloads": {DL: {"state": "unknown"}}})


if __name__ == "__main__":
    unittest.main()
