"""Où en sont les téléchargements d'un compte (`GET /api/downloads`), pour que l'interface les lance un par un : le registre seul (horloge injectée), puis un vrai serveur.

Une tentative est reconnue par le `dl` que l'interface ajoute au lien (`/media/<id>/file?download=1&dl=<id>`)."""

from __future__ import annotations

import http.client
import shutil
import socket
import tempfile
import time
import unittest

from tests.test_activity import Clock
from tests.test_server import AccountsCase, Client, Running
from hub import accounts
from hub.activity import ATTEMPTS_MAX, ATTEMPTS_PER_USER, KEEP_SECONDS, Activity, attempt_id

DL = "Abc123def456"
OTHER = "Zyx987wvu654"


class TestAttemptRegistry(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.registry = Activity(self.clock)

    def send(self, dl=DL, user="u1", start=0, size=10, count=None, fail=False):
        """Une requête de la tentative : `count` octets envoyés à partir de `start` (tout le reste par défaut), puis une coupure si `fail`."""
        count = size - start if count is None else count
        try:
            with self.registry.attempt(user, dl, start, size) as sent:
                sent(count)
                if fail:
                    raise BrokenPipeError("le client est parti")
        except BrokenPipeError:
            pass

    def state(self, dl=DL, user="u1"):
        return self.registry.attempts_of(user, [dl])[dl]

    def test_an_unseen_attempt_is_unknown(self):
        self.assertEqual(self.state(), {"state": "unknown"})

    def test_an_open_request_is_running_with_its_bytes_and_size(self):
        with self.registry.attempt("u1", DL, 0, 10) as sent:
            sent(4)
            self.assertEqual(self.state(), {"state": "running", "bytes": 4, "size": 10})

    def test_a_request_that_reaches_the_last_byte_is_done(self):
        self.send()
        self.assertEqual(self.state(), {"state": "done", "bytes": 10, "size": 10})

    def test_a_request_that_ends_early_without_error_is_not_done(self):
        self.send(count=6)                                                  # le fichier a raccourci en route : le dernier octet n'est jamais parti
        self.assertEqual(self.state(), {"state": "stopped", "bytes": 6, "size": 10})

    def test_a_cut_request_is_stopped(self):
        self.send(count=4, fail=True)
        self.assertEqual(self.state(), {"state": "stopped", "bytes": 4, "size": 10})

    def test_a_request_that_fails_after_the_last_byte_is_not_done(self):
        self.send(fail=True)
        self.assertEqual(self.state()["state"], "stopped")

    def test_a_resume_by_range_goes_back_to_running_then_done(self):
        self.send(count=4, fail=True)
        with self.registry.attempt("u1", DL, 4, 10) as sent:
            sent(3)
            self.assertEqual(self.state(), {"state": "running", "bytes": 7, "size": 10})
            sent(3)
        self.assertEqual(self.state(), {"state": "done", "bytes": 10, "size": 10})

    def test_a_partial_range_that_does_not_reach_the_end_is_not_done(self):
        self.send(start=0, count=5)
        self.assertEqual(self.state()["state"], "stopped")

    def test_running_while_one_request_is_still_open(self):
        with self.registry.attempt("u1", DL, 0, 10) as sent:
            self.send(start=5, count=2, fail=True)
            sent(1)
            self.assertEqual(self.state()["state"], "running")
        self.assertEqual(self.state()["state"], "stopped")

    def test_done_stays_done(self):
        self.send()
        self.send(count=2, fail=True)
        self.assertEqual(self.state()["state"], "done")

    def test_two_attempts_are_distinct(self):
        self.send(DL)
        self.send(OTHER, count=3, fail=True)
        self.assertEqual(self.registry.attempts_of("u1", [DL, OTHER]), {DL: {"state": "done", "bytes": 10, "size": 10}, OTHER: {"state": "stopped", "bytes": 3, "size": 10}})

    def test_another_account_sees_unknown(self):
        self.send()
        self.assertEqual(self.state(user="u2"), {"state": "unknown"})

    def test_the_outcome_is_kept_ten_minutes_and_dropped_after_the_delay(self):
        self.send()
        self.clock.now += 600
        self.assertEqual(self.state()["state"], "done")
        self.clock.now += KEEP_SECONDS
        self.assertEqual(self.state(), {"state": "unknown"})

    def test_the_outcome_is_dropped_exactly_at_the_delay(self):
        self.send()
        self.clock.now += KEEP_SECONDS - 1
        self.assertEqual(self.state()["state"], "done")
        self.clock.now += 1
        self.assertEqual(self.state(), {"state": "unknown"})

    def test_an_account_at_its_limit_when_the_registry_is_full_too(self):
        users = ATTEMPTS_MAX // ATTEMPTS_PER_USER
        for n in range(ATTEMPTS_MAX):
            self.clock.now += 0.1
            self.send(f"attempt{n:05d}", user=f"user{n % users}")
        self.send("attemptnew", user="user0")
        self.assertEqual(len(self.registry.attempts), ATTEMPTS_MAX)
        self.assertEqual(self.state("attemptnew", "user0")["state"], "done")
        self.assertEqual(self.state("attempt00000", "user0"), {"state": "unknown"})
        self.assertEqual(self.state("attempt00001", "user1")["state"], "done")       # la place libérée par le compte suffit : personne d'autre ne perd la sienne

    def test_an_open_attempt_is_never_dropped_for_age(self):
        with self.registry.attempt("u1", DL, 0, 10) as sent:
            self.clock.now += KEEP_SECONDS * 2
            sent(1)
            self.assertEqual(self.state()["state"], "running")

    def test_an_account_keeps_a_bounded_number_of_attempts_the_oldest_go_first(self):
        for n in range(ATTEMPTS_PER_USER + 1):
            self.clock.now += 1
            self.send(f"attempt{n:05d}")
        self.assertEqual(self.state("attempt00000"), {"state": "unknown"})
        self.assertEqual(self.state("attempt00001")["state"], "done")
        self.assertEqual(self.state(f"attempt{ATTEMPTS_PER_USER:05d}")["state"], "done")
        self.assertEqual(len(self.registry.attempts), ATTEMPTS_PER_USER)

    def test_the_whole_registry_is_bounded(self):
        for n in range(ATTEMPTS_MAX + 1):
            self.clock.now += 0.1                                           # toutes dans le délai : c'est le plafond qui retire, pas l'âge
            self.send(f"attempt{n:05d}", user=f"user{n % 50}")
        self.assertEqual(len(self.registry.attempts), ATTEMPTS_MAX)
        self.assertEqual(self.state("attempt00000", "user0"), {"state": "unknown"})
        self.assertEqual(self.state(f"attempt{ATTEMPTS_MAX:05d}", f"user{ATTEMPTS_MAX % 50}")["state"], "done")

    def test_open_attempts_are_never_dropped_for_the_limit_the_new_one_is_not_followed(self):
        managers = [self.registry.attempt("u1", f"attempt{n:05d}", 0, 10) for n in range(ATTEMPTS_PER_USER)]
        for manager in managers:
            manager.__enter__()
        self.send("attempt99999")
        self.assertEqual(self.state("attempt99999"), {"state": "unknown"})
        self.assertEqual(self.state("attempt00000")["state"], "running")
        for manager in managers:
            manager.__exit__(None, None, None)

    def test_the_identifier_is_8_to_32_ascii_letters_or_digits(self):
        for good in ("abcdEFGH", "a" * 32, "0123456789"):
            self.assertEqual(attempt_id(good), good)
        for bad in (None, 3, "", "abc", "a" * 33, "abcdefg!", "../../etc/passwd", "abcdéfgh", "abcd efgh", "١٢٣٤٥٦٧٨٩", "abcdefgh\n"):
            self.assertEqual(attempt_id(bad), "", bad)


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
