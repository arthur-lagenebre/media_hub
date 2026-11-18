"""Ce que le hub fait en ce moment : qui lit, télécharge ou envoie (`GET /api/activity`), et la trace des décisions de lecture. Le registre seul (horloge injectée), puis les routes, puis un vrai serveur."""

from __future__ import annotations

import http.client
import io
import json
import shutil
import socket
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from tests import fixtures
from tests.rig import MiniHub, Rig, call
from tests.test_server import AccountsCase, Client, Running
from tests.test_server_views import entry, item
from hub import accounts, activity, config
from hub.activity import ACTIVE_SECONDS, Activity
from hub.server import app

FFMPEG = fixtures.FFMPEG
PASSWORD = "un-mot-de-passe-ok"


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.registry = Activity(self.clock)
        self.held = []

    def lines(self):
        return self.registry.report()["transfers"]

    def open(self, user="u1", ref="e1", kind="direct", address="192.168.1.5", name="alice", title="Film", library="films", agent="Mozilla/5.0 Firefox/120.0"):
        """Ouvre un transfert sans le fermer : (gestionnaire de contexte, fonction de comptage). Le gestionnaire reste tenu, sinon son ramasse-miettes fermerait le transfert."""
        manager = self.registry.transfer(user, name, ref, kind, address, title, library, agent)
        self.held.append(manager)
        return manager, manager.__enter__()

    def test_a_transfer_is_listed_while_it_is_open_and_for_a_minute_after(self):
        manager, _ = self.open()
        self.clock.now += 3600                                           # une pause d'une heure : la connexion reste ouverte, la ligne aussi
        self.assertEqual([(t["kind"], t["connections"]) for t in self.lines()], [("direct", 1)])
        manager.__exit__(None, None, None)
        self.assertEqual([t["connections"] for t in self.lines()], [0])
        self.clock.now += ACTIVE_SECONDS - 1
        self.assertEqual(len(self.lines()), 1)
        self.clock.now += 2
        self.assertEqual(self.lines(), [])

    def test_the_line_says_who_what_where_and_how(self):
        manager, count = self.open(library="films", agent="Mozilla/5.0 (Windows NT 10.0) Firefox/120.0")
        self.clock.now += 5
        count(1500)
        line, = self.lines()
        self.assertEqual(line, {"user": "alice", "title": "Film", "library": "films", "kind": "direct", "origin": "local", "agent": "Firefox 120 · Windows",
                                "started": 1000, "last": 1005, "bytes": 1500, "connections": 1})
        manager.__exit__(None, None, None)

    def test_the_requests_of_one_device_share_a_line(self):
        for _ in range(3):
            with self.registry.transfer("u1", "alice", "e1", "direct", "192.168.1.5", "Film", "films", "") as count:
                count(10)
            self.clock.now += 2
        line, = self.lines()
        self.assertEqual((line["bytes"], line["connections"], line["started"], line["last"]), (30, 0, 1000, 1004))

    def test_overlapping_requests_count_as_open_connections(self):
        first, _ = self.open()
        second, _ = self.open()
        self.assertEqual([t["connections"] for t in self.lines()], [2])
        first.__exit__(None, None, None)
        self.assertEqual([t["connections"] for t in self.lines()], [1])
        second.__exit__(None, None, None)

    def test_two_devices_of_one_account_are_two_lines_and_the_address_is_never_given(self):
        self.open(address="192.168.1.5")
        self.open(address="203.0.113.77")
        self.open(ref="e2", address="192.168.1.5")                  # un autre fichier : une autre ligne
        self.open(kind="download", address="192.168.1.5")           # une autre nature aussi
        self.open(user="u2", name="bob", address="192.168.1.5")
        report = self.registry.report()
        self.assertEqual(len(report["transfers"]), 5)
        text = json.dumps(report)
        self.assertNotIn("192.168.1.5", text)
        self.assertNotIn("203.0.113.77", text)

    def test_the_origin_follows_the_address(self):
        for address, origin in (("127.0.0.1", "local"), ("::1", "local"), ("192.168.1.20", "local"), ("10.0.0.2", "local"), ("172.16.5.5", "local"), ("::ffff:192.168.1.5", "local"),
                                ("fe80::1%eth0", "local"), ("fd12:3456:789a::/64", "local"), ("8.8.8.8", "remote"), ("::ffff:8.8.8.8", "remote"), ("2606:4700:4700::/64", "remote"),
                                ("100.101.102.103", "local"), ("100.64.0.0", "local"), ("100.127.255.255", "local"), ("::ffff:100.101.102.103", "local"), ("100.63.255.255", "remote"), ("100.128.0.0", "remote"),
                                ("fd7a:115c:a1e0::1", "local"), ("n'importe quoi", "remote"), ("", "remote")):
            self.assertEqual(activity.origin_of(address), origin, address)

    def test_a_progress_report_prolongs_an_existing_line_and_never_creates_one(self):
        with self.registry.transfer("u1", "alice", "e1", "direct", "192.168.1.5", "Film", "films", "") as count:
            count(1)
        self.clock.now += 50
        self.registry.touch("u1", "e1")                                   # le navigateur lit depuis sa mémoire tampon : il n'envoie plus de Range
        self.clock.now += 50
        self.assertEqual([t["last"] for t in self.lines()], [1050])
        self.clock.now += 20
        self.assertEqual(self.lines(), [])
        self.registry.touch("u1", "e1")                                   # la ligne a disparu : rien ne la recrée
        self.registry.touch("u2", "e9")
        self.assertEqual(self.lines(), [])
        self.assertEqual(self.registry.lines, {})

    def test_a_progress_report_of_another_account_or_file_prolongs_nothing(self):
        with self.registry.transfer("u1", "alice", "e1", "direct", "192.168.1.5", "Film", "films", ""):
            pass
        self.clock.now += 50
        self.registry.touch("u2", "e1")
        self.registry.touch("u1", "e2")
        self.clock.now += 20
        self.assertEqual(self.lines(), [])

    def test_the_expired_lines_are_dropped_so_the_registry_does_not_grow(self):
        for n in range(50):
            with self.registry.transfer("u1", "alice", f"e{n}", "direct", "192.168.1.5", "Film", "films", ""):
                pass
        self.assertEqual(len(self.registry.lines), 50)
        self.clock.now += ACTIVE_SECONDS + 1
        with self.registry.transfer("u1", "alice", "neuf", "direct", "192.168.1.5", "Film", "films", ""):      # une écriture suffit, sans lecture
            pass
        self.assertEqual(len(self.registry.lines), 1)

    def test_a_transfer_that_fails_before_sending_anything_leaves_nothing(self):
        with self.assertRaises(RuntimeError):
            with self.registry.transfer("u1", "alice", "e1", "upload", "192.168.1.5", "a.mkv", "", ""):
                raise RuntimeError("refusé")
        self.assertEqual(self.lines(), [])
        with self.assertRaises(BrokenPipeError):                          # un client coupé après des octets reste visible : il vient de lire
            with self.registry.transfer("u1", "alice", "e2", "direct", "192.168.1.5", "Film", "films", "") as count:
                count(5)
                raise BrokenPipeError()
        self.assertEqual([(t["bytes"], t["connections"]) for t in self.lines()], [(5, 0)])

    def test_the_counts(self):
        for kind, n in (("direct", 2), ("audio", 1), ("remux", 1), ("transcode", 2), ("download", 3), ("upload", 1), ("backup", 1)):
            for i in range(n):
                self.open(ref=f"{kind}{i}", kind=kind)
        self.assertEqual(self.registry.report()["counts"], {"playing": 6, "downloads": 3, "uploads": 1, "remux": 1, "transcodes": 2, "backups": 1})

    def test_the_title_and_the_agent_are_bounded_and_clean(self):
        self.open(title="Film\n\x00" + "x" * 500, agent="Bot\r\n" + "y" * 500)
        line, = self.lines()
        self.assertEqual((len(line["title"]) <= 200, "\n" in line["title"], "\x00" in line["title"]), (True, False, False))
        self.assertEqual((len(line["agent"]) <= activity.AGENT_MAX, "\n" in line["agent"]), (True, False))

    def test_the_device_comes_from_the_latest_decision_of_the_same_account_file_and_client(self):
        self.open(address="192.168.1.5")
        self.open(address="192.168.1.9", ref="e1")
        self.registry.decide("u1", "alice", "192.168.1.5", "UA", "tv", "e1", "Film", {}, "direct", "ok")
        self.registry.decide("u1", "alice", "192.168.1.9", "UA", "phone", "e2", "Autre", {}, "direct", "ok")      # une autre entrée
        self.registry.decide("u2", "bob", "192.168.1.9", "UA", "pc", "e1", "Film", {}, "direct", "ok")            # un autre compte
        self.assertEqual([t.get("device") for t in self.lines()], ["tv", None])                                   # l'autre client n'a pas de décision à lui : rien n'est deviné
        self.registry.decide("u1", "alice", "192.168.1.5", "UA", "pc", "e1", "Film", {}, "direct", "ok")          # la plus récente l'emporte
        self.assertEqual([t.get("device") for t in self.lines()], ["pc", None])
        self.registry.decide("u1", "alice", "192.168.1.5", "UA", "", "e1", "Film", {}, "direct", "ok")            # sans appareil : la clé est absente
        self.assertNotIn("device", self.lines()[0])


class TestDecisions(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.registry = Activity(self.clock)

    def decide(self, n=1, **over):
        args = {"user": "u1", "name": "alice", "address": "192.168.1.5", "agent": "UA", "device": "tv", "entry": f"e{n}", "title": f"Film {n}", "caps": {}, "mode": "direct", "why": "ok", **over}
        self.registry.decide(*args.values())

    def test_a_decision_keeps_the_date_the_account_the_device_the_file_the_caps_the_mode_and_the_reasons(self):
        self.decide(caps={"video": ["h264"], "channels": 2}, mode="remux", why="le conteneur ne passe pas")
        decision, = self.registry.report()["decisions"]
        self.assertEqual(decision, {"at": 1000, "user": "alice", "device": "tv", "agent": "UA", "entry": "e1", "title": "Film 1", "caps": {"video": ["h264"], "channels": 2},
                                    "mode": "remux", "why": "le conteneur ne passe pas"})
        self.assertNotIn("192.168.1.5", json.dumps(decision))

    def test_the_most_recent_comes_first_and_the_queue_is_bounded(self):
        for n in range(activity.DECISIONS_KEPT + 20):
            self.clock.now += 1
            self.decide(n)
        decisions = self.registry.report()["decisions"]
        self.assertEqual(len(decisions), activity.DECISIONS_KEPT)
        self.assertEqual((decisions[0]["entry"], decisions[-1]["entry"]), (f"e{activity.DECISIONS_KEPT + 19}", "e20"))

    def test_the_device_is_one_of_the_four_or_empty(self):
        self.assertEqual([activity.clean_device(v) for v in ("pc", "phone", "tablet", "tv")], ["pc", "phone", "tablet", "tv"])
        for value in ("TV", "tv ", "<script>", "", None, 5, ["tv"], {"a": 1}, "x" * 1000):
            self.assertEqual(activity.clean_device(value), "", repr(value))

    def test_the_caps_keep_only_what_the_decision_reads(self):
        caps = {"video": ["h264", "hevc"], "audio": ["aac"], "containers": ["mp4"], "channels": 6, "height": 2160, "hdr": True, "secret": "x", "path": "/srv/Films", "__proto__": {}}
        self.assertEqual(activity.clean_caps(caps), {"video": ["h264", "hevc"], "audio": ["aac"], "containers": ["mp4"], "channels": 6, "height": 2160, "hdr": True})

    def test_hostile_caps_are_cleaned(self):
        cleaned = activity.clean_caps({"video": ["h264", 5, None, ["x"], {"a": 1}, "a" * 100, "ok\n", "<b>", ""] + ["v%d" % n for n in range(100)],
                                      "audio": "aac", "containers": {"mp4": 1}, "channels": "6", "height": True, "hdr": "oui"})
        self.assertEqual(cleaned["video"][:2], ["h264", "v0"])
        self.assertLessEqual(len(cleaned["video"]), activity.CAPS_ITEMS)
        self.assertEqual(set(cleaned), {"video"})                                      # le reste n'avait pas le bon type : écarté
        for caps in (None, "texte", 5, [], [1, 2], {}, {"channels": 10 ** 9, "height": -3}, {"channels": float("nan")}):
            self.assertIsInstance(activity.clean_caps(caps), dict, repr(caps))
        self.assertEqual(activity.clean_caps({"channels": 10 ** 9, "height": -3}), {})
        self.assertEqual(activity.clean_caps({"channels": 2.0, "height": 1080.5}), {"channels": 2})

    def test_the_text_of_a_decision_is_bounded(self):
        self.decide(agent="Mozilla " + "z" * 1000, title="T" * 1000, why="w" * 1000)
        decision, = self.registry.report()["decisions"]
        self.assertTrue(len(decision["agent"]) <= activity.AGENT_MAX and len(decision["title"]) <= activity.TITLE_MAX)

    def test_the_agent_is_shortened(self):
        chrome = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        edge = chrome + " Edg/120.0.2210.91"
        phone = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
        safari = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1"
        self.assertEqual([activity.short_agent(a) for a in (chrome, edge, phone, safari)], ["Chrome 120 · Windows", "Edge 120 · Windows", "Chrome 120 · Android", "Safari 17 · iPhone"])
        self.assertEqual(activity.short_agent("ExoPlayerLib/2.18.7"), "ExoPlayerLib/2.18.7")
        self.assertEqual((activity.short_agent(""), activity.short_agent(None)), ("", ""))


# ---------------------------------------------------------------------------
# Les routes, sans réseau
# ---------------------------------------------------------------------------
class Spy(io.BytesIO):
    """La réponse d'une requête : à chaque écriture elle relève l'activité du hub, et casse comme un client qui part une fois `break_after` octets du transfert partis."""

    def __init__(self, hub, break_after=None):
        super().__init__()
        self.hub, self.seen, self.break_after = hub, [], break_after

    def write(self, data):
        self.seen.append(self.hub.activity.report()["transfers"])
        sent = self.seen[-1][0]["bytes"] if self.seen[-1] else 0
        if self.break_after is not None and sent >= self.break_after:
            raise BrokenPipeError("le client est parti")
        return super().write(data)


def spying(spy):
    class Spied(Rig):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            self.wfile = spy
    return Spied


class ActivityCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "web").mkdir()
        self.lib = config.Library("lib", "Bibliothèque", "movies", self.root / "lib")
        self.lib.path.mkdir()
        self.hub = MiniHub(self.root, [self.lib])
        self.clock = Clock(time.time())
        self.hub.activity = Activity(self.clock)
        (self.lib.path / "Film (2020).mkv").write_bytes(b"0123456789")
        (self.lib.path / "piste.flac").write_bytes(b"abcdefghij")
        (self.lib.path / "Livre.cbz").write_bytes(b"PK0123456789")
        self.hub.db.replace_library("lib", [item("mv", title="Film"), item("al", "album", title="Album"), item("bk", "book", title="Livre")],
                                    [dict(entry("f1", "mv"), rel="Film (2020).mkv", title="Le Film"), dict(entry("k1", "al", kind="audio"), rel="piste.flac", title="La Piste"),
                                     dict(entry("b1", "bk", kind="book"), rel="Livre.cbz", title="Le Livre")])

    def get(self, target, **kw):
        return call(self.hub, "GET", target, **kw)[1]

    def transfers(self):
        return self.hub.activity.report()["transfers"]


class TestWhatIsCounted(ActivityCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(app, "CHUNK", 4)                       # un fichier de dix octets en trois écritures
        patcher.start()
        self.addCleanup(patcher.stop)

    def watch(self, target, headers=None, break_after=None):
        spy = Spy(self.hub, break_after)
        _, reply = call(self.hub, "GET", target, headers=headers, rig=spying(spy))
        return spy, reply

    def test_a_range_read_of_a_film_is_a_direct_line_while_it_is_sent(self):
        spy, reply = self.watch("/media/f1/file", {"Range": "bytes=0-9"})
        self.assertEqual(reply.status, 206)
        during = spy.seen[-1]
        self.assertEqual([(t["kind"], t["title"], t["library"], t["connections"], t["origin"]) for t in during], [("direct", "Le Film", "lib", 1, "local")])
        after, = self.transfers()
        self.assertEqual((after["connections"], after["bytes"]), (0, 10))

    def test_a_track_is_an_audio_line(self):
        self.get("/media/k1/file", headers={"Range": "bytes=0-3"})
        self.assertEqual([(t["kind"], t["title"], t["bytes"]) for t in self.transfers()], [("audio", "La Piste", 4)])

    def test_a_download_is_a_download_line_while_it_is_sent(self):
        spy, _ = self.watch("/media/f1/file?download=1")
        self.assertEqual([(t["kind"], t["connections"]) for t in spy.seen[-1]], [("download", 1)])
        self.assertEqual([t["kind"] for t in self.transfers()], ["download"])

    def test_a_book_that_is_read_is_not_followed_but_one_that_is_downloaded_is(self):
        self.get("/media/b1/file")
        self.assertEqual(self.transfers(), [])
        self.get("/media/b1/file?download=1")
        self.assertEqual([t["kind"] for t in self.transfers()], ["download"])

    def test_two_range_reads_make_one_line(self):
        self.get("/media/f1/file", headers={"Range": "bytes=0-3"})
        self.get("/media/f1/file", headers={"Range": "bytes=4-9"})
        line, = self.transfers()
        self.assertEqual((line["kind"], line["bytes"], line["connections"]), ("direct", 10, 0))

    def test_the_line_disappears_after_the_delay(self):
        self.get("/media/f1/file", headers={"Range": "bytes=0-3"})
        self.clock.now += ACTIVE_SECONDS + 1
        self.assertEqual(self.transfers(), [])

    def test_a_head_a_404_and_a_416_leave_nothing(self):
        self.assertEqual(call(self.hub, "HEAD", "/media/f1/file")[1].status, 200)
        self.assertEqual(self.get("/media/nope/file").status, 404)
        (self.lib.path / "Film (2020).mkv").rename(self.lib.path / "ailleurs.mkv")
        self.assertEqual(self.get("/media/f1/file").status, 404)                                # le fichier a disparu du disque
        (self.lib.path / "ailleurs.mkv").rename(self.lib.path / "Film (2020).mkv")
        self.assertEqual(self.get("/media/f1/file", headers={"Range": "bytes=999-"}).status, 416)
        self.assertEqual(call(self.hub, "HEAD", "/media/f1/file?download=1")[1].status, 200)
        self.assertEqual(self.transfers(), [])

    def test_a_client_that_leaves_in_the_middle_closes_its_connection(self):
        spy, _ = self.watch("/media/f1/file?download=1", break_after=5)
        self.assertEqual([(t["kind"], t["connections"]) for t in self.transfers()], [("download", 0)])

    def test_a_download_refused_for_lack_of_the_right_leaves_nothing(self):
        accounts.create(self.hub.db, "boss", PASSWORD)                   # le premier compte est l'administrateur
        bob = accounts.create(self.hub.db, "bob", PASSWORD)
        accounts.set_rights(self.hub.db, bob["id"], {"lib"}, {"lib": {"download": False}})
        reply = self.get("/media/f1/file?download=1", headers=self.hub.cookie(accounts.by_id(self.hub.db, bob["id"])))
        self.assertEqual(reply.status, 403)
        self.assertEqual(self.transfers(), [])


class TestProgress(ActivityCase):
    def test_a_progress_report_prolongs_the_line_of_the_same_account_and_file(self):
        self.get("/media/f1/file", headers={"Range": "bytes=0-3"})
        self.clock.now += 50
        reply = call(self.hub, "POST", "/api/progress", {"entry": "f1", "position": 5, "duration": 100})[1]
        self.assertEqual(reply.status, 200)
        self.clock.now += 50
        self.assertEqual([t["last"] for t in self.transfers()], [int(self.clock.now - 50)])

    def test_a_progress_report_never_creates_a_line(self):
        call(self.hub, "POST", "/api/progress", {"entry": "f1", "position": 5, "duration": 100})
        self.assertEqual(self.transfers(), [])


class TestInboxAndBackup(ActivityCase):
    def setUp(self):
        super().setUp()
        self.boss = accounts.create(self.hub.db, "boss", PASSWORD, role="admin")
        self.alice = accounts.create(self.hub.db, "alice", PASSWORD)
        accounts.set_can_upload(self.hub.db, self.alice["id"], True)
        self.hub.activity = Activity(self.clock)

    def cookie(self, row):
        return self.hub.cookie(accounts.by_id(self.hub.db, row["id"]))

    def test_an_upload_is_visible_while_it_lasts_and_counts_its_bytes(self):
        seen = []

        class Slow(Rig):
            def __init__(self, *args, **kw):
                super().__init__(*args, **kw)
                data, hub = io.BytesIO(self.rfile.getvalue()), self.hub

                class Reader:
                    def read(self, n):
                        seen.append(hub.activity.report()["transfers"])
                        return data.read(min(n, 4))
                self.rfile = Reader()

        _, reply = call(self.hub, "POST", "/api/inbox?name=Film.mkv", b"0123456789", self.cookie(self.alice), Slow)
        self.assertEqual(reply.status, 201)
        self.assertTrue(seen)
        self.assertEqual([(t["kind"], t["user"], t["title"], t["connections"]) for t in seen[0]], [("upload", "alice", "Film.mkv", 1)])
        after, = self.transfers()
        self.assertEqual((after["connections"], after["bytes"]), (0, 10))

    def test_a_refused_upload_leaves_nothing(self):
        bob = accounts.create(self.hub.db, "bob", PASSWORD)
        self.hub.activity = Activity(self.clock)
        reply = call(self.hub, "POST", "/api/inbox?name=Film.mkv", b"x" * 20, self.cookie(bob))[1]
        self.assertEqual(reply.status, 403)
        self.assertEqual(self.transfers(), [])

    def test_an_admin_download_from_the_inbox_is_a_download_line(self):
        call(self.hub, "POST", "/api/inbox?name=Film.mkv", b"0123456789", self.cookie(self.alice))
        self.hub.activity = Activity(self.clock)
        spy = Spy(self.hub)
        _, reply = call(self.hub, "GET", "/api/inbox/alice/Film.mkv", headers=self.cookie(self.boss), rig=spying(spy))
        self.assertEqual(reply.status, 200)
        self.assertEqual([(t["kind"], t["user"], t["title"], t["connections"]) for t in spy.seen[-1]], [("download", "boss", "Film.mkv", 1)])

    def test_a_backup_is_a_backup_line_and_the_archive_path_is_not_given(self):
        def make(db_path, data, out):
            Path(out).write_bytes(b"zip" * 10)
        spy = Spy(self.hub)
        with mock.patch.object(app.backup, "create", make):
            _, reply = call(self.hub, "GET", "/api/backup", headers=self.cookie(self.boss), rig=spying(spy))
        self.assertEqual(reply.status, 200)
        self.assertEqual([(t["kind"], t["user"], t["connections"]) for t in spy.seen[-1]], [("backup", "boss", 1)])
        self.assertNotIn(str(self.root), json.dumps(self.hub.activity.report()))


class FakeProc:
    """Un ffmpeg : `chunks` puis la fin du flux (ou, avec `dies`, la fin avant l'heure, comme un processus tué)."""

    def __init__(self, chunks, hub, seen):
        self.chunks, self.hub, self.seen, self.killed = list(chunks), hub, seen, False
        self.stdout = types.SimpleNamespace(read1=self.read1, close=lambda: None)
        self.stderr = types.SimpleNamespace(read=lambda: b"", close=lambda: None)

    def read1(self, n):
        self.seen.append((self.hub.active, [t["kind"] for t in self.hub.activity.report()["transfers"]]))
        return self.chunks.pop(0) if self.chunks else b""

    def poll(self):
        return 0 if not self.chunks else None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


class TestStreams(ActivityCase):
    def stream(self, query, chunks=(b"aaaa", b"bbbb", b"cccc"), break_after=None):
        self.hub.cfg.ffmpeg = "ffmpeg-bidon"
        seen = []
        proc = FakeProc(chunks, self.hub, seen)
        spy = Spy(self.hub, break_after)
        with mock.patch.object(app, "probe_of", lambda hub, e: {"audio": [], "subs": []}), mock.patch.object(app, "stream_command", lambda *a: ["ffmpeg-bidon"]), \
                mock.patch.object(app.subprocess, "Popen", lambda *a, **kw: proc):
            _, reply = call(self.hub, "GET", f"/media/f1/stream?{query}", rig=spying(spy))
        return proc, seen, spy, reply

    def free(self):
        """Les places de conversion sont toutes libres : on peut les reprendre toutes."""
        taken = [self.hub.streams.acquire(blocking=False) for _ in range(self.hub.cfg.max_streams)]
        for ok in taken:
            if ok:
                self.hub.streams.release()
        return all(taken)

    def test_a_remux_and_a_transcode_are_lines_while_they_are_sent(self):
        for query, kind in (("m=remux", "remux"), ("m=transcode", "transcode"), ("", "remux")):
            self.hub.activity = Activity(self.clock)
            proc, seen, spy, reply = self.stream(query)
            self.assertEqual(reply.status, 200, query)
            self.assertIn((1, [kind]), seen, query)
            self.assertEqual([(t["kind"], t["title"], t["bytes"], t["connections"]) for t in self.transfers()], [(kind, "Le Film", 12, 0)], query)
            self.assertEqual((self.hub.active, self.free()), (0, True), query)

    def test_the_counters_come_back_to_zero_when_the_client_leaves(self):
        proc, _, _, _ = self.stream("m=transcode", chunks=[b"a" * 4] * 10, break_after=6)
        self.assertEqual((self.hub.active, self.free(), proc.killed), (0, True, True))
        self.assertEqual([t["connections"] for t in self.transfers()], [0])

    def test_the_counters_come_back_to_zero_when_ffmpeg_dies(self):
        class Dying(FakeProc):
            def read1(self, n):
                if len(self.chunks) == 1:
                    raise OSError("tube cassé")
                return super().read1(n)
        self.hub.cfg.ffmpeg = "ffmpeg-bidon"
        proc = Dying([b"aaaa", b"bbbb"], self.hub, [])
        with mock.patch.object(app, "probe_of", lambda hub, e: {"audio": [], "subs": []}), mock.patch.object(app, "stream_command", lambda *a: ["ffmpeg-bidon"]), \
                mock.patch.object(app.subprocess, "Popen", lambda *a, **kw: proc):
            _, reply = call(self.hub, "GET", "/media/f1/stream?m=remux")
        self.assertEqual(reply.status, 200)
        self.assertEqual((self.hub.active, self.free()), (0, True))
        self.assertEqual([t["connections"] for t in self.transfers()], [0])

    def test_a_head_or_an_ffmpeg_that_produces_nothing_leaves_no_line(self):
        proc, _, _, reply = self.stream("m=remux", chunks=[])
        self.assertEqual(reply.status, 500)
        self.hub.cfg.ffmpeg = "ffmpeg-bidon"
        proc = FakeProc([b"aaaa"], self.hub, [])
        with mock.patch.object(app, "probe_of", lambda hub, e: {"audio": [], "subs": []}), mock.patch.object(app, "stream_command", lambda *a: ["ffmpeg-bidon"]), \
                mock.patch.object(app.subprocess, "Popen", lambda *a, **kw: proc):
            self.assertEqual(call(self.hub, "HEAD", "/media/f1/stream?m=remux")[1].status, 200)
        self.assertEqual(self.transfers(), [])
        self.assertEqual((self.hub.active, self.free()), (0, True))


class TestRoute(ActivityCase):
    def setUp(self):
        super().setUp()
        self.boss = accounts.create(self.hub.db, "boss", PASSWORD, role="admin")
        self.bob = accounts.create(self.hub.db, "bob", PASSWORD)
        self.hub.activity = Activity(self.clock)
        self.hub.started = time.time() - 90

    def cookie(self, row):
        return self.hub.cookie(accounts.by_id(self.hub.db, row["id"]))

    def read(self, row=None):
        return call(self.hub, "GET", "/api/activity", headers=self.cookie(row or self.boss))[1]

    def test_a_plain_account_gets_a_403_and_nobody_a_401(self):
        self.assertEqual((self.read(self.bob).status, self.read(self.bob).json()), (403, {"error": "réservé à l'administrateur"}))
        self.assertEqual(call(self.hub, "GET", "/api/activity")[1].status, 401)

    def test_the_shape_of_the_answer(self):
        self.hub.jobs.busy.return_value = True
        reply = self.read()
        self.assertEqual(reply.status, 200)
        out = reply.json()
        self.assertEqual(set(out), {"version", "uptime", "busy", "streams", "max_streams", "counts", "transfers", "decisions"})
        self.assertEqual((out["version"], out["busy"], out["streams"], out["max_streams"]), (app.__version__, True, 0, 2))
        self.assertTrue(90 <= out["uptime"] <= 95)
        self.assertEqual(out["counts"], {"playing": 0, "downloads": 0, "uploads": 0, "remux": 0, "transcodes": 0, "backups": 0})
        self.assertEqual((out["transfers"], out["decisions"]), ([], []))

    def test_a_transfer_shows_with_its_origin_behind_the_proxy_and_never_its_address(self):
        call(self.hub, "GET", "/media/f1/file", headers={**self.cookie(self.bob), "Range": "bytes=0-3", "X-Forwarded-For": "8.8.8.8", "User-Agent": "Mozilla/5.0 (Windows NT 10.0) Firefox/120.0"})
        call(self.hub, "GET", "/media/f1/file", headers={**self.cookie(self.bob), "Range": "bytes=0-3"})
        body = self.read().body.decode()
        lines = json.loads(body)["transfers"]
        self.assertEqual(sorted((t["user"], t["origin"], t["kind"]) for t in lines), [("bob", "local", "direct"), ("bob", "remote", "direct")])
        for text in ("8.8.8.8", "127.0.0.1", str(self.root), "Film (2020)", "token"):
            self.assertNotIn(text, body)

    def test_a_decision_gives_its_device_to_the_transfers_of_the_same_client(self):
        with mock.patch.object(app, "probe_of", lambda hub, e: {"audio": [], "subs": [], "duration": 0, "video": {}}), \
                mock.patch.object(app.prober, "plan", lambda *a: {"mode": "direct", "why": "lisible tel quel", "audio": None, "copy_audio": True}):
            play = call(self.hub, "POST", "/api/play/f1", {"device": "tv", "caps": {"video": ["h264"], "x": 1}}, {**self.cookie(self.bob), "User-Agent": "Mozilla/5.0 (Linux; Android 11) Chrome/110.0"})[1]
        self.assertEqual(play.status, 200)
        call(self.hub, "GET", "/media/f1/file", headers={**self.cookie(self.bob), "Range": "bytes=0-3", "User-Agent": "Mozilla/5.0 (Linux; Android 11) Chrome/110.0"})
        out = self.read().json()
        self.assertEqual([(t["user"], t["device"]) for t in out["transfers"]], [("bob", "tv")])
        decision, = out["decisions"]
        self.assertEqual({k: decision[k] for k in ("user", "device", "agent", "entry", "title", "caps", "mode", "why")},
                         {"user": "bob", "device": "tv", "agent": "Chrome 110 · Android", "entry": "f1", "title": "Le Film", "caps": {"video": ["h264"]}, "mode": "direct", "why": "lisible tel quel"})

    def test_a_play_without_a_device_is_valid_and_hostile_values_are_cleaned(self):
        with mock.patch.object(app, "probe_of", lambda hub, e: {"audio": [], "subs": [], "duration": 0, "video": {}}), \
                mock.patch.object(app.prober, "plan", lambda *a: {"mode": "direct", "why": "ok", "audio": None, "copy_audio": True}):
            self.assertEqual(call(self.hub, "POST", "/api/play/f1", {}, self.cookie(self.bob))[1].status, 200)
            self.assertEqual(call(self.hub, "POST", "/api/play/f1", {"device": "<img src=x onerror=1>", "caps": {"video": ["h264", "x" * 999, 7], "path": "/srv"}}, self.cookie(self.bob))[1].status, 200)
        newest, oldest = self.read().json()["decisions"]
        self.assertEqual((oldest["device"], oldest["caps"]), ("", {}))
        self.assertEqual((newest["device"], newest["caps"]), ("", {"video": ["h264"]}))

    def test_a_refused_play_leaves_no_decision(self):
        self.assertEqual(call(self.hub, "POST", "/api/play/k1", {}, self.cookie(self.bob))[1].status, 400)          # une piste n'est pas une vidéo
        self.assertEqual(call(self.hub, "POST", "/api/play/nope", {}, self.cookie(self.bob))[1].status, 404)
        self.assertEqual(self.read().json()["decisions"], [])


# ---------------------------------------------------------------------------
# Un vrai serveur
# ---------------------------------------------------------------------------
class Live(unittest.TestCase):
    """Un serveur ouvert (personne n'est connecté : l'administrateur par défaut), l'horloge du registre à la main."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.run_ = Running(cls.tmp)
        cls.hub = cls.run_.hub
        cls.api = Client(cls.run_.port)

    @classmethod
    def tearDownClass(cls):
        cls.run_.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.clock = Clock(time.time())
        self.hub.activity = Activity(self.clock)

    def activity(self):
        status, data = self.api.json("/api/activity")
        self.assertEqual(status, 200, data)
        return data

    def track(self):
        found = next(i for i in self.api.json("/api/library/musique?limit=600")[1]["items"] if i["title"] == "Discovery")
        return self.api.json(f"/api/item/{found['id']}")[1]["entries"][0]["id"]

    def wait_for(self, check, what, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            if check():
                return
            time.sleep(0.05)
        self.fail(what)


class TestLiveReading(Live):
    def test_two_range_reads_are_one_line_that_disappears_after_the_delay(self):
        track = self.track()
        for rng in ("bytes=0-9", "bytes=10-19"):
            self.assertEqual(self.api.get(f"/media/{track}/file", headers={"Range": rng, "X-Forwarded-For": "8.8.8.8"})[0], 206)
        line, = self.activity()["transfers"]
        self.assertEqual((line["kind"], line["origin"], line["bytes"], line["connections"]), ("audio", "remote", 20, 0))
        self.clock.now += ACTIVE_SECONDS + 1
        self.assertEqual(self.activity()["transfers"], [])

    def test_a_progress_report_keeps_the_line_alive(self):
        track = self.track()
        self.api.get(f"/media/{track}/file", headers={"Range": "bytes=0-9"})
        self.clock.now += 50
        self.assertEqual(self.api.json("/api/progress", {"entry": track, "position": 5, "duration": 200})[0], 200)
        self.clock.now += 50
        self.assertEqual(len(self.activity()["transfers"]), 1)

    def test_head_and_416_leave_nothing(self):
        track = self.track()
        self.assertEqual(self.api.request("HEAD", f"/media/{track}/file")[0], 200)
        self.assertEqual(self.api.request("HEAD", f"/media/{track}/file?download=1")[0], 200)
        self.assertEqual(self.api.get(f"/media/{track}/file", headers={"Range": "bytes=99999999-"})[0], 416)
        self.assertEqual(self.activity()["transfers"], [])

    def test_a_download_shows_while_it_is_sent_and_a_client_that_leaves_closes_it(self):
        track = self.track()
        path = self.hub.path_of(self.hub.entry(track))[1]
        original = path.read_bytes()
        self.addCleanup(path.write_bytes, original)
        with open(path, "ab") as handle:
            handle.write(b"\0" * (96 << 20))                                      # bien plus que les tampons de la connexion : l'envoi reste en cours tant que le client ne lit pas
        conn = http.client.HTTPConnection("127.0.0.1", self.run_.port, timeout=30)
        conn.request("GET", f"/media/{track}/file?download=1")
        response = conn.getresponse()
        response.read(1024)
        self.wait_for(lambda: [(t["kind"], t["connections"]) for t in self.activity()["transfers"]] == [("download", 1)], "le téléchargement n'apparaît pas pendant l'envoi")
        self.assertEqual(self.activity()["counts"]["downloads"], 1)
        conn.sock.shutdown(socket.SHUT_RDWR)
        conn.close()
        self.wait_for(lambda: [t["connections"] for t in self.activity()["transfers"]] == [0], "la connexion coupée reste ouverte")


@unittest.skipUnless(FFMPEG, "ffmpeg absent : les flux convertis ne sont pas essayés")
class TestLiveStreams(Live):
    def play(self):
        found = next(i for i in self.api.json("/api/library/films?limit=600")[1]["items"] if i["title"] == "Vrai Film")
        entry = self.api.json(f"/api/item/{found['id']}")[1]["entries"][0]["id"]
        caps = {"video": ["vp9"], "audio": ["aac"], "containers": ["mp4"], "channels": 2, "height": 1080}
        status, plan = self.api.json(f"/api/play/{entry}", {"caps": caps, "max_height": 180, "device": "tv"})
        self.assertEqual((status, plan["mode"]), (200, "transcode"))
        return plan

    def settled(self):
        data = self.activity()
        return data["streams"] == 0 and all(t["connections"] == 0 for t in data["transfers"])

    def test_the_counters_come_back_to_zero_after_the_client_leaves(self):
        plan = self.play()
        conn = http.client.HTTPConnection("127.0.0.1", self.run_.port, timeout=30)
        conn.request("GET", plan["url"])
        conn.getresponse().read(2048)
        conn.close()
        self.wait_for(self.settled, "les compteurs ne reviennent pas à zéro")
        data = self.activity()
        self.assertEqual(data["streams"], 0)
        self.assertEqual([(t["kind"], t["device"]) for t in data["transfers"]], [("transcode", "tv")])
        self.assertEqual(data["decisions"][0]["mode"], "transcode")

    def test_the_counters_come_back_to_zero_when_ffmpeg_is_killed(self):
        plan = self.play()
        started = []
        real = app.subprocess.Popen

        def recording(*args, **kw):
            proc = real(*args, **kw)
            started.append(proc)
            return proc

        with mock.patch.object(app.subprocess, "Popen", recording):
            conn = http.client.HTTPConnection("127.0.0.1", self.run_.port, timeout=30)
            conn.request("GET", plan["url"])
            response = conn.getresponse()
            response.read(1024)
            self.wait_for(lambda: self.activity()["streams"] == 1 or started and started[0].poll() is not None, "le flux ne démarre pas")
            started[0].kill()
            try:
                response.read()
            except (http.client.HTTPException, OSError):
                pass
            conn.close()
        self.wait_for(self.settled, "les compteurs ne reviennent pas à zéro")


class TestLiveRights(AccountsCase):
    def test_only_an_administrator_reads_the_activity_and_no_address_comes_out(self):
        alice = self.new_user("alice")
        self.assertEqual(alice.json("/api/activity")[0], 403)
        self.assertEqual(self.anon.request("GET", "/api/activity")[0], 401)
        found = next(i for i in self.admin.json("/api/library/musique?limit=600")[1]["items"] if i["title"] == "Discovery")
        track = self.admin.json(f"/api/item/{found['id']}")[1]["entries"][0]["id"]
        self.assertEqual(alice.request("GET", f"/media/{track}/file", headers={"Range": "bytes=0-9", "X-Forwarded-For": "8.8.8.8"})[0], 206)
        status, _, body = self.admin.request("GET", "/api/activity")
        self.assertEqual(status, 200)
        text = body.decode()
        self.assertEqual([(t["user"], t["origin"], t["kind"]) for t in json.loads(text)["transfers"]], [("alice", "remote", "audio")])
        for secret in ("8.8.8.8", "127.0.0.1", str(self.run_.root), self.admin.token, alice.token, self.hub.secret):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
