"""Local ou distant : une seule définition de l'origine d'une connexion (`local`, `tailscale`, `internet`) et le droit « Télécharger à distance » d'un compte.

La fonction d'origine seule, puis le gestionnaire (la connexion, un en-tête `X-Forwarded-For` menteur), puis un vrai serveur (le refus 403, `/api/state`, la lecture qui reste permise), la migration de la base et le registre d'activité."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from email.message import Message
from pathlib import Path

from tests.rig import MiniHub
from tests.test_server import AccountsCase, entry_of
from tests.test_server_http import coming_from
from hub import accounts
from hub import db as database
from hub.activity import Activity
from hub.db import Database
from hub.origin import origin_of
from hub.server import app

LAN, PROXY, TAILNET = "192.168.0.50", "127.0.0.1", "100.101.102.103"
REFUSAL = "téléchargement non autorisé à distance"


class TestOriginOf(unittest.TestCase):
    def test_the_machine_itself_without_a_header_is_local(self):
        for peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            self.assertEqual(origin_of(peer), "local", peer)

    def test_a_private_address_is_local(self):
        for peer in ("192.168.1.20", "10.0.0.2", "172.16.5.5", "172.31.255.255", "::ffff:192.168.1.5", "fe80::1%eth0", "fd12:3456:789a::/64", "169.254.1.1"):
            self.assertEqual(origin_of(peer), "local", peer)

    def test_a_tailscale_address_connecting_directly_is_tailscale_not_local(self):
        for peer in ("100.101.102.103", "100.64.0.0", "100.127.255.255", "::ffff:100.101.102.103", "fd7a:115c:a1e0::1", "fd7a:115c:a1e0:ab12:4843:cd96:6200:1/128"):
            self.assertEqual(origin_of(peer), "tailscale", peer)

    def test_the_proxy_carries_the_real_address_of_the_client(self):
        for forwarded, origin in (("100.101.102.103", "tailscale"), ("fd7a:115c:a1e0::7", "tailscale"), ("::ffff:100.64.0.9", "tailscale"), ("8.8.8.8", "internet"), ("203.0.113.9", "internet"),
                                  ("2606:4700:4700::/64", "internet"), ("100.63.255.255", "internet"), ("100.128.0.0", "internet")):
            self.assertEqual(origin_of("127.0.0.1", forwarded), origin, forwarded)
            self.assertEqual(origin_of("::1", forwarded), origin, forwarded)

    def test_behind_the_proxy_nothing_is_local_even_with_a_private_address(self):
        self.assertEqual(origin_of("127.0.0.1", "192.168.0.5"), "internet")        # `local` se prouve par une connexion directe, jamais par ce que dit un en-tête
        self.assertEqual(origin_of("127.0.0.1", "127.0.0.1"), "internet")

    def test_a_public_address_is_internet(self):
        for peer in ("8.8.8.8", "::ffff:8.8.8.8", "2606:4700:4700::/64", "100.63.255.255", "100.128.0.0", "203.0.113.9"):
            self.assertEqual(origin_of(peer), "internet", peer)

    def test_an_unreadable_address_is_internet(self):
        for peer in ("n'importe quoi", "", None, "999.1.1.1"):
            self.assertEqual(origin_of(peer), "internet", repr(peer))
        for forwarded in ("pas-une-adresse", "203.0.113.9:443", "unknown", "999.1.1.1"):
            self.assertEqual(origin_of("127.0.0.1", forwarded), "internet", forwarded)

    def test_a_header_from_anything_but_the_machine_is_never_believed(self):
        self.assertEqual(origin_of(LAN, "8.8.8.8"), "local")                      # le réseau local qui écrit une adresse publique reste local
        self.assertEqual(origin_of(LAN, "100.101.102.103"), "local")
        self.assertEqual(origin_of(TAILNET, "192.168.0.5"), "tailscale")          # un appareil Tailscale qui se dit « à la maison » reste Tailscale
        self.assertEqual(origin_of(TAILNET, "127.0.0.1"), "tailscale")
        self.assertEqual(origin_of("8.8.8.8", "192.168.0.5"), "internet")


class TestHandlerOrigin(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.hub = MiniHub(root)
        self.boss = accounts.create(self.hub.db, "boss", "un-mot-de-passe-ok")            # le premier compte est l'administrateur
        self.row = accounts.create(self.hub.db, "alice", "un-mot-de-passe-ok")

    def rig(self, source, forwarded=None, user=None):
        rig = coming_from(source)(self.hub, headers={"X-Forwarded-For": forwarded} if forwarded is not None else None)
        rig.user, rig.granted = user, None
        return rig

    def test_the_origin_comes_from_the_connection_and_the_header_of_the_machine_only(self):
        for source, forwarded, origin in ((PROXY, None, "local"), (LAN, None, "local"), (TAILNET, None, "tailscale"), (PROXY, TAILNET, "tailscale"), (PROXY, "8.8.8.8", "internet"),
                                          (PROXY, "pas-une-adresse", "internet"), ("::ffff:127.0.0.1", "8.8.8.8", "internet"), ("8.8.8.8", None, "internet")):
            self.assertEqual(app.origin(self.rig(source, forwarded)), origin, (source, forwarded))

    def test_a_lie_in_the_header_changes_nothing(self):
        self.assertEqual(app.origin(self.rig(LAN, "8.8.8.8")), "local")
        self.assertEqual(app.origin(self.rig(LAN, "100.101.102.103")), "local")
        self.assertEqual(app.origin(self.rig(TAILNET, "192.168.0.5")), "tailscale")
        self.assertEqual(app.origin(self.rig(TAILNET, "127.0.0.1")), "tailscale")

    def test_the_last_entry_of_the_header_is_the_one_the_proxy_wrote(self):
        self.assertEqual(app.origin(self.rig(PROXY, "192.168.0.5, 8.8.8.8")), "internet")       # la première, le client l'a choisie
        self.assertEqual(app.origin(self.rig(PROXY, "8.8.8.8, 100.101.102.103")), "tailscale")

    def test_a_header_present_but_empty_or_unreadable_is_internet_and_only_its_absence_is_local(self):
        for forwarded in ("", " ", "8.8.8.8,", "8.8.8.8, ", "pas-une-adresse"):
            self.assertEqual(app.origin(self.rig(PROXY, forwarded)), "internet", repr(forwarded))
        two_lines = coming_from(PROXY)(self.hub)
        two_lines.headers = Message()
        two_lines.headers["X-Forwarded-For"] = "8.8.8.8"
        two_lines.headers["X-Forwarded-For"] = ""
        self.assertEqual(app.origin(two_lines), "internet")
        self.assertEqual(app.origin(self.rig(PROXY)), "local")

    def test_an_empty_header_does_not_break_the_address_kept_against_brute_force(self):
        for forwarded in ("", " ", "8.8.8.8,"):
            self.assertEqual(app.client_ip(self.rig(PROXY, forwarded)), PROXY, repr(forwarded))        # repli sur la connexion, comme avant

    def user(self, remote_download):
        accounts.set_remote_download(self.hub.db, self.row["id"], remote_download)
        return accounts.public(accounts.by_id(self.hub.db, self.row["id"]))

    def test_without_the_right_the_download_is_allowed_on_the_local_network_only(self):
        user = self.user(False)
        for source, forwarded, expected in ((LAN, None, True), (PROXY, None, True), (LAN, "8.8.8.8", True), (TAILNET, None, False), (PROXY, TAILNET, False), (PROXY, "8.8.8.8", False),
                                            (TAILNET, "192.168.0.5", False), (PROXY, "pas-une-adresse", False)):
            self.assertEqual(self.rig(source, forwarded, user).can("films", "download"), expected, (source, forwarded))

    def test_the_right_is_for_the_download_only(self):
        rig = self.rig(PROXY, TAILNET, self.user(False))
        self.assertTrue(rig.can("films", "view"))
        self.assertTrue(rig.can("films", "play"))
        self.assertFalse(rig.can("films", "download"))

    def test_with_the_right_everything_is_allowed_everywhere(self):
        user = self.user(True)
        for source, forwarded in ((LAN, None), (TAILNET, None), (PROXY, TAILNET), (PROXY, "8.8.8.8")):
            self.assertTrue(self.rig(source, forwarded, user).can("films", "download"), (source, forwarded))

    def test_an_administrator_can_always_download(self):
        boss = accounts.public(self.boss)
        for source, forwarded in ((LAN, None), (TAILNET, None), (PROXY, "8.8.8.8")):
            self.assertTrue(self.rig(source, forwarded, boss).can("films", "download"), (source, forwarded))

    def test_the_remote_refusal_says_why_and_the_other_refusals_keep_their_words(self):
        rig = self.rig(PROXY, "8.8.8.8", self.user(False))
        with self.assertRaises(app.HttpError) as caught:
            rig.require("films", "download")
        self.assertEqual((caught.exception.status, caught.exception.message), (403, REFUSAL))
        accounts.set_rights(self.hub.db, self.row["id"], {"films"}, {"films": {"download": False}})
        rig = self.rig(LAN, None, accounts.public(accounts.by_id(self.hub.db, self.row["id"])))
        with self.assertRaises(app.HttpError) as caught:
            rig.require("films", "download")
        self.assertEqual(caught.exception.message, "téléchargement non autorisé")           # refusé pour la bibliothèque, pas pour l'origine


class TestRemoteDownload(AccountsCase):
    """Un vrai serveur : tout ce qui arrive du test vient de 127.0.0.1, `X-Forwarded-For` dit alors d'où le proxy a reçu la requête."""

    def setUp(self):
        super().setUp()
        self.alice, self.bob = self.new_user("alice"), self.new_user("bob")
        self.inception, self.film_files = entry_of(self.admin, "films", "Inception")
        self.film = self.film_files[0]["id"]
        self.url = f"/media/{self.film}/file?download=1"

    def set_right(self, user, allowed, expect=200):
        status, body = self.admin.json(f"/api/user/{user.account['id']}/remote_download", {"remote_download": allowed})
        self.assertEqual(status, expect, body)
        return body

    def via(self, address):
        return {"X-Forwarded-For": address}

    def test_a_new_account_has_the_right_and_the_listing_says_so(self):
        self.assertTrue(self.alice.account["remote_download"])
        self.assertEqual({u["name"]: u["remote_download"] for u in self.admin.json("/api/users")[1]["users"]}, {"admin": True, "alice": True, "bob": True})
        self.assertEqual(self.alice.get(self.url, headers=self.via("8.8.8.8"))[0], 200)

    def test_the_administrator_changes_the_right_and_the_account_comes_back_with_it(self):
        made = self.set_right(self.alice, False)
        self.assertEqual((made["name"], made["remote_download"]), ("alice", False))
        self.assertEqual({u["name"]: u["remote_download"] for u in self.admin.json("/api/users")[1]["users"]}, {"admin": True, "alice": False, "bob": True})
        self.assertEqual(self.set_right(self.alice, True)["remote_download"], True)

    def test_only_a_boolean_and_only_for_a_user_by_an_administrator(self):
        for wrong in ("non", 0, None):
            self.assertEqual(self.admin.json(f"/api/user/{self.alice.account['id']}/remote_download", {"remote_download": wrong})[0], 400, wrong)
        self.assertEqual(self.admin.json("/api/user/default/remote_download", {"remote_download": False})[0], 400)       # l'administrateur peut toujours
        self.assertEqual(self.admin.json("/api/user/inconnu/remote_download", {"remote_download": False})[0], 404)
        self.assertEqual(self.alice.json(f"/api/user/{self.bob.account['id']}/remote_download", {"remote_download": False})[0], 403)
        self.assertTrue(self.admin.json("/api/users")[1]["users"][-1]["remote_download"])

    def test_without_the_right_a_download_through_the_proxy_is_refused_and_a_local_one_is_not(self):
        self.set_right(self.alice, False)
        for address in ("100.101.102.103", "fd7a:115c:a1e0::7", "8.8.8.8", "2606:4700:4700::1", "pas-une-adresse"):
            status, _, body = self.alice.get(self.url, headers=self.via(address))
            self.assertEqual((status, REFUSAL in body.decode()), (403, True), address)
        self.assertEqual(self.alice.get(self.url)[0], 200)                                                  # connexion directe, sans en-tête : local
        self.assertEqual(self.bob.get(self.url, headers=self.via("8.8.8.8"))[0], 200)                      # un autre compte garde le droit

    def test_the_refusal_is_a_403_with_the_reason_in_french(self):
        self.set_right(self.alice, False)
        status, _, body = self.alice.request("GET", self.url, headers=self.via("100.101.102.103"))
        self.assertEqual((status, body), (403, ('{"error":"%s"}' % REFUSAL).encode("utf-8")))

    def test_reading_stays_allowed_everywhere(self):
        self.set_right(self.alice, False)
        for address in ("100.101.102.103", "8.8.8.8"):
            self.assertEqual(self.alice.get(f"/media/{self.film}/file", headers={**self.via(address), "Range": "bytes=0-9"})[0], 206, address)
            self.assertEqual(self.alice.get(f"/media/{self.film}/file", headers=self.via(address))[0], 200, address)
            self.assertEqual(self.alice.json(f"/api/play/{self.film}", {})[0], 200)
            self.assertEqual(self.alice.get(f"/api/item/{self.inception}", headers=self.via(address))[0], 200)

    def test_the_administrator_downloads_from_anywhere(self):
        for address in ("100.101.102.103", "8.8.8.8"):
            self.assertEqual(self.admin.get(self.url, headers=self.via(address))[0], 200, address)

    def test_the_state_says_download_false_away_from_home_and_leaves_play_alone(self):
        self.set_right(self.alice, False)

        def flags(headers=None):
            status, _, body = self.alice.get("/api/state", headers=headers)
            self.assertEqual(status, 200)
            return [(lib["id"], lib["play"], lib["download"]) for lib in json.loads(body)["libraries"]]

        home = flags()
        self.assertTrue(all(play and download for _, play, download in home), home)
        for address in ("100.101.102.103", "8.8.8.8"):
            away = flags(self.via(address))
            self.assertEqual([lib for lib, *_ in away], [lib for lib, *_ in home])
            self.assertTrue(all(play and not download for _, play, download in away), away)

    def test_a_library_already_closed_to_download_says_so_in_its_own_words(self):
        self.admin.json(f"/api/user/{self.alice.account['id']}/rights", {"rights": {"films": {"download": False}}})
        self.set_right(self.alice, False)
        status, _, body = self.alice.get(self.url, headers=self.via("8.8.8.8"))
        self.assertEqual((status, b"non autoris\xc3\xa9\"" in body), (403, True))
        self.assertNotIn(REFUSAL, body.decode())


class TestMigration(unittest.TestCase):
    def test_a_base_in_version_10_keeps_every_account_and_gives_them_the_right(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        path = root / "hub.db"
        db = Database(path)
        boss = accounts.create(db, "boss", "un-mot-de-passe-ok", "admin")
        alice = accounts.create(db, "alice", "un-mot-de-passe-ok")
        accounts.set_can_upload(db, alice["id"], True)
        accounts.set_can_request(db, alice["id"], False)
        accounts.set_rights(db, alice["id"], {"films"}, {"films": {"download": False}})
        db.save_progress(alice["id"], "e1", 12.5, 100, False)
        db.connection().close()
        conn = sqlite3.connect(path)
        conn.executescript("ALTER TABLE users DROP COLUMN remote_download; UPDATE kv SET value='10' WHERE key='schema';")
        conn.commit()
        conn.close()
        db = Database(path)
        self.assertEqual(db.kv_get("schema"), database.SCHEMA_VERSION)
        self.assertEqual(database.SCHEMA_VERSION, 11)
        self.assertEqual([tuple(r) for r in db.query("SELECT name, can_upload, can_request, remote_download FROM users ORDER BY name")], [("alice", 1, 0, 1), ("boss", 0, 1, 1)])
        self.assertEqual(accounts.grants(db, alice["id"]), {"films": (True, True, False)})
        self.assertEqual([tuple(r) for r in db.query("SELECT profile, entry_id, position FROM progress")], [(alice["id"], "e1", 12.5)])
        self.assertTrue(accounts.public(accounts.by_id(db, alice["id"]))["remote_download"])
        self.assertEqual(Database(path).kv_get("schema"), database.SCHEMA_VERSION)          # rouverte : rien n'est rejoué
        self.assertIsNotNone(boss)

    def test_a_new_base_has_the_column_with_the_right_given(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        db = Database(root / "hub.db")
        db.execute("INSERT INTO users(id, name, pass_hash, created) VALUES('x', 'x', 'h', 1)")
        self.assertEqual(db.one("SELECT remote_download FROM users WHERE id='x'")["remote_download"], 1)


class TestActivityOrigin(unittest.TestCase):
    def test_the_registry_gives_the_three_values(self):
        registry = Activity()
        for address, origin in (("192.168.1.5", "local"), ("100.101.102.103", "tailscale"), ("8.8.8.8", "internet")):
            with registry.transfer("u1", "alice", f"e-{origin}", "direct", address, origin=origin) as count:
                count(1)
        self.assertEqual(sorted(line["origin"] for line in registry.report()["transfers"]), ["internet", "local", "tailscale"])

    def test_the_origin_is_never_guessed_from_the_address(self):
        with self.assertRaises(TypeError):
            Activity().transfer("u1", "alice", "e1", "direct", "192.168.1.5")

    def test_the_origin_of_the_request_wins_over_the_address_the_registry_keeps(self):
        registry = Activity()
        with registry.transfer("u1", "alice", "e1", "direct", "8.8.8.8", origin="tailscale") as count:
            count(1)
        self.assertEqual([line["origin"] for line in registry.report()["transfers"]], ["tailscale"])


if __name__ == "__main__":
    unittest.main()
