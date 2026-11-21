"""La création d'un compte : nom, rôle, mot de passe, et ce qui change pour le premier."""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from hub import accounts, prefs
from hub.accounts import AccountError
from hub.db import Database


class AccountsDb(unittest.TestCase):
    """Une base neuve par test, et `refused` : l'appel doit lever une AccountError de ce statut, dont le message est rendu."""

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.db = Database(root / "hub.db")

    def refused(self, call, status=400):
        with self.assertRaises(AccountError) as caught:
            call()
        self.assertEqual(caught.exception.status, status)
        return caught.exception.message


class TestCreate(AccountsDb):

    def test_revoking_the_sessions_of_an_account_keeps_its_password_and_spares_the_others(self):
        alice = accounts.create(self.db, "alice", "un-mot-de-passe")
        bob = accounts.create(self.db, "bob", "un-mot-de-passe")
        old = accounts.session_token("s", alice)
        bobs = accounts.session_token("s", bob)
        row = accounts.revoke_sessions(self.db, alice["id"])
        self.assertIsNone(accounts.user_for_token(self.db, "s", old))
        self.assertEqual(accounts.user_for_token(self.db, "s", accounts.session_token("s", row))["id"], alice["id"])
        self.assertEqual(accounts.user_for_token(self.db, "s", bobs)["id"], bob["id"])
        self.assertEqual(accounts.authenticate(self.db, "alice", "un-mot-de-passe")["id"], alice["id"])
        self.assertEqual(self.refused(lambda: accounts.revoke_sessions(self.db, "inconnu"), 404), "compte inconnu")

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

    def test_a_password_needs_twelve_characters_to_be_created_or_changed(self):
        self.assertEqual(accounts.MIN_PASSWORD, 12)
        self.assertIn("doit faire de 12 à 200", self.refused(lambda: accounts.create(self.db, "alice", "a" * 11)))
        self.assertEqual(accounts.rows(self.db), [])
        alice = accounts.create(self.db, "alice", "a" * 12)
        self.assertIn("doit faire de 12 à 200", self.refused(lambda: accounts.set_password(self.db, alice["id"], "b" * 11)))
        self.assertEqual(accounts.authenticate(self.db, "alice", "a" * 12)["id"], alice["id"])        # le refus n'a rien changé
        accounts.set_password(self.db, alice["id"], "b" * 12)
        self.assertEqual(accounts.authenticate(self.db, "alice", "b" * 12)["id"], alice["id"])
        self.assertIn("doit faire de 12 à 200", self.refused(lambda: accounts.create(self.db, "bob", "c" * 201)))

    def test_an_older_password_of_eight_characters_still_opens_the_door(self):
        old = accounts.create(self.db, "alice", "huitcars", check=False)               # un compte d'avant la longueur de douze (ou HUB_PASSWORD)
        self.assertEqual(accounts.authenticate(self.db, "alice", "huitcars")["id"], old["id"])
        self.assertEqual(self.refused(lambda: accounts.authenticate(self.db, "alice", "huitcar"), 401), "mot de passe incorrect")

    def test_without_the_check_a_password_must_still_be_a_non_empty_text(self):
        for password in ("", None, 1234, b"octets"):
            self.assertIn("mot de passe vide", self.refused(lambda: accounts.create(self.db, "alice", password, check=False)), repr(password))
        self.assertEqual(accounts.rows(self.db), [])


class TestSessionTokens(unittest.TestCase):
    """Le jeton de session porte sa date d'émission : il se périme après `SESSION_DAYS` jours et se renouvelle passé la moitié."""
    DAY = 86400

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.db = Database(root / "hub.db")
        self.alice = accounts.create(self.db, "alice", "un-mot-de-passe")
        self.issued = 1_800_000_000
        self.token = accounts.session_token("s", self.alice, self.issued)

    def at(self, days, token=None):
        return accounts.user_for_token(self.db, "s", token or self.token, now=self.issued + days * self.DAY)

    def test_the_token_names_its_account_and_its_date(self):
        uid, issued, mac = self.token.split(".")
        self.assertEqual((uid, issued), (self.alice["id"], str(self.issued)))
        self.assertRegex(mac, r"^[0-9a-f]{64}$")
        self.assertRegex(accounts.session_token("s", self.alice), rf"^{self.alice['id']}\.[0-9]{{10}}\.[0-9a-f]{{64}}$")       # sans date donnée : maintenant

    def test_the_token_is_valid_until_it_is_session_days_old(self):
        self.assertEqual(self.at(0)["id"], self.alice["id"])
        self.assertEqual(accounts.user_for_token(self.db, "s", self.token, now=self.issued + accounts.SESSION_DAYS * self.DAY - 1)["id"], self.alice["id"])
        self.assertIsNone(self.at(accounts.SESSION_DAYS))
        self.assertIsNone(self.at(accounts.SESSION_DAYS + 400))

    def test_the_date_is_signed(self):
        uid, issued, mac = self.token.split(".")
        later = f"{uid}.{int(issued) + 100 * self.DAY}.{mac}"                    # repousser la date pour faire durer le jeton
        self.assertIsNone(self.at(1, later))
        self.assertIsNone(self.at(1, f"{uid}.{mac}"))                          # ni enlever la date
        self.assertIsNone(self.at(1, f"{uid}.{issued}.{issued}.{mac}"))

    def test_the_token_is_due_for_renewal_after_half_of_its_life(self):
        half = accounts.SESSION_DAYS * self.DAY / 2
        self.assertFalse(accounts.session_due(self.token, now=self.issued))
        self.assertFalse(accounts.session_due(self.token, now=self.issued + half - 1))
        self.assertTrue(accounts.session_due(self.token, now=self.issued + half))

    def test_the_old_form_without_a_date_is_still_accepted_and_due(self):
        legacy = f"{self.alice['id']}.{accounts._mac('s', self.alice['id'], self.alice['token_version'])}"
        self.assertEqual(self.at(0, legacy)["id"], self.alice["id"])
        self.assertTrue(accounts.session_due(legacy))
        self.assertIsNone(self.at(0, legacy.replace(legacy[-1], "0" if legacy[-1] != "0" else "1")))
        with mock.patch.object(accounts, "LEGACY_SESSIONS", False):
            self.assertIsNone(self.at(0, legacy))
        accounts.revoke_sessions(self.db, self.alice["id"])
        self.assertIsNone(self.at(0, legacy))                                   # révoquée comme l'autre

    def test_the_media_token_has_no_date_and_does_not_expire(self):
        media = accounts.media_token("s", self.alice)
        self.assertRegex(media, rf"^{self.alice['id']}\.[0-9a-f]{{64}}$")
        self.assertEqual(accounts.user_for_token(self.db, "s", media, "media", now=self.issued + 1000 * self.DAY)["id"], self.alice["id"])
        self.assertIsNone(accounts.user_for_token(self.db, "s", self.token, "media"))          # un jeton de session n'y vaut rien, daté ou non


class TestUploadRight(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.db = Database(root / "hub.db")
        self.boss = accounts.create(self.db, "boss", "un-mot-de-passe")
        self.alice = accounts.create(self.db, "alice", "un-mot-de-passe")

    def refused(self, call, status=400):
        with self.assertRaises(AccountError) as caught:
            call()
        self.assertEqual(caught.exception.status, status)
        return caught.exception.message

    def test_a_new_account_may_not_send_and_the_administrator_always_may(self):
        self.assertEqual((accounts.public(self.alice)["can_upload"], accounts.public(self.boss)["can_upload"]), (False, True))
        self.assertEqual(self.boss["can_upload"], 0)                    # la colonne ne dit rien de l'administrateur : le rôle suffit

    def test_the_right_is_given_and_taken_back(self):
        row = accounts.set_can_upload(self.db, self.alice["id"], True)
        self.assertEqual((row["id"], accounts.public(row)["can_upload"], accounts.listing(row)["can_upload"]), (self.alice["id"], True, True))
        self.assertTrue(accounts.public(accounts.by_id(self.db, self.alice["id"]))["can_upload"])
        self.assertFalse(accounts.public(accounts.set_can_upload(self.db, self.alice["id"], False))["can_upload"])

    def test_it_changes_nothing_else_and_nobody_else(self):
        accounts.set_can_upload(self.db, self.alice["id"], True)
        self.assertEqual(accounts.public(accounts.by_id(self.db, self.boss["id"])), {"id": "default", "name": "boss", "role": "admin", "can_upload": True, "can_request": True, "remote_download": True})
        self.assertEqual(accounts.by_id(self.db, self.alice["id"])["token_version"], 1)                # la session ne tombe pas

    def test_an_administrator_a_stranger_and_a_value_that_is_not_a_boolean_are_refused(self):
        self.assertEqual(self.refused(lambda: accounts.set_can_upload(self.db, self.boss["id"], False)), "un administrateur peut toujours envoyer")
        self.assertEqual(self.refused(lambda: accounts.set_can_upload(self.db, "inconnu", True), 404), "compte inconnu")
        for value in ("oui", 1, 0, None, [True]):
            self.refused(lambda: accounts.set_can_upload(self.db, self.alice["id"], value))
        self.assertFalse(accounts.public(accounts.by_id(self.db, self.alice["id"]))["can_upload"])


class TestRemoteDownloadRight(unittest.TestCase):
    """« Télécharger à distance » : donné par défaut, retiré par l'administrateur, jamais à un administrateur."""

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.db = Database(root / "hub.db")
        self.boss = accounts.create(self.db, "boss", "un-mot-de-passe")
        self.alice = accounts.create(self.db, "alice", "un-mot-de-passe")

    def test_a_new_account_may_download_remotely_until_the_right_is_taken_back(self):
        self.assertTrue(accounts.can_download_remotely(self.alice))
        row = accounts.set_remote_download(self.db, self.alice["id"], False)
        self.assertFalse(accounts.can_download_remotely(row))
        self.assertFalse(accounts.public(accounts.by_id(self.db, self.alice["id"]))["remote_download"])

    def test_the_administrator_always_may_whatever_the_column_says(self):
        self.assertTrue(accounts.can_download_remotely({**self.boss, "remote_download": 0}))


class TestRename(unittest.TestCase):
    """Un compte change de nom : l'identifiant reste, donc tout ce qui lui est attaché aussi."""

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.db = Database(root / "hub.db")
        self.boss = accounts.create(self.db, "boss", "un-mot-de-passe")
        self.alice = accounts.create(self.db, "alice", "un-mot-de-passe")

    def refused(self, call, status):
        with self.assertRaises(AccountError) as caught:
            call()
        self.assertEqual(caught.exception.status, status)
        return caught.exception.message

    def test_the_new_name_opens_the_door_and_the_old_one_no_longer_does(self):
        row = accounts.rename(self.db, self.alice["id"], "  Alice   Martin ")
        self.assertEqual((row["id"], row["name"]), (self.alice["id"], "Alice Martin"))
        self.assertEqual(accounts.authenticate(self.db, "alice martin", "un-mot-de-passe")["id"], self.alice["id"])
        self.assertEqual(self.refused(lambda: accounts.authenticate(self.db, "alice", "un-mot-de-passe"), 401), "mot de passe incorrect")

    def test_a_name_taken_by_another_account_is_refused_whatever_its_case_or_spaces(self):
        accounts.create(self.db, "Jean Paul", "un-mot-de-passe")
        for name in ("BOSS", "Boss", " boss ", "jean   paul", "JEAN PAUL"):
            self.assertEqual(self.refused(lambda: accounts.rename(self.db, self.alice["id"], name), 409), "ce nom est déjà pris", name)
        self.assertEqual(accounts.by_id(self.db, self.alice["id"])["name"], "alice")

    def test_a_name_that_differs_only_beyond_ascii_case_is_taken_too(self):
        accounts.create(self.db, "Élodie", "un-mot-de-passe")           # NOCASE de SQLite ne plie que l'ASCII : la vérification, elle, plie tout
        self.assertEqual(self.refused(lambda: accounts.rename(self.db, self.alice["id"], "éLODIE"), 409), "ce nom est déjà pris")

    def test_its_own_name_in_another_case_is_accepted(self):
        self.assertEqual(accounts.rename(self.db, self.alice["id"], "ALICE")["name"], "ALICE")
        self.assertEqual(accounts.rename(self.db, self.alice["id"], "ALICE")["name"], "ALICE")      # deux fois : même état

    def test_an_unknown_account_and_a_bad_name_are_refused(self):
        self.assertEqual(self.refused(lambda: accounts.rename(self.db, "inconnu", "zoé"), 404), "compte inconnu")
        for name in ("", "   ", None, 5, "x" * (accounts.MAX_NAME + 1), "a\nb\x00"):
            self.refused(lambda: accounts.rename(self.db, self.alice["id"], name), 400)
        self.assertEqual(accounts.by_id(self.db, self.alice["id"])["name"], "alice")

    def test_everything_attached_to_the_account_stays(self):
        uid = self.alice["id"]
        self.db.save_progress(uid, "e1", 42.0, 100.0, False)
        self.db.execute("INSERT INTO playlists(id, profile, name, created, updated) VALUES('p1', ?, 'Route', 1, 1)", (uid,))
        self.db.set_rating(uid, "i1", "up")
        accounts.set_rights(self.db, uid, {"films"}, {"films": {"download": False}})
        accounts.set_can_upload(self.db, uid, True)
        self.db.kv_set(prefs.KEY + uid, {"music_view": "artists"})
        self.db.execute("UPDATE users SET inbox_dir='alice' WHERE id=?", (uid,))
        before = dict(accounts.by_id(self.db, uid))
        row = accounts.rename(self.db, uid, "Zoé")
        self.assertEqual({**dict(row), "name": "alice"}, before)                  # token_version, inbox_dir, droits globaux : rien d'autre que le nom
        self.assertEqual(self.db.progress_for(uid, ["e1"])["e1"]["position"], 42.0)
        self.assertEqual([r["name"] for r in self.db.query("SELECT name FROM playlists WHERE profile=?", (uid,))], ["Route"])
        self.assertEqual(self.db.ratings_for(uid), {"i1": "up"})
        self.assertEqual(accounts.grants(self.db, uid), {"films": (True, True, False)})
        self.assertEqual(self.db.kv_get(prefs.KEY + uid), {"music_view": "artists"})
        self.assertEqual(accounts.by_id(self.db, self.boss["id"])["name"], "boss")

    def test_a_session_token_survives_the_rename(self):
        token = accounts.session_token("s", self.alice)
        accounts.rename(self.db, self.alice["id"], "Zoé")
        self.assertEqual(accounts.user_for_token(self.db, "s", token)["name"], "Zoé")


class TestInterfaceKnowsTheMinimum(unittest.TestCase):
    def test_the_form_checks_and_announces_the_length_the_server_demands(self):
        """Le contrôle côté client et les libellés suivent `MIN_PASSWORD` : sinon la personne tape un mot de passe « assez long » et le serveur le refuse après l'envoi."""
        web = Path(__file__).resolve().parent.parent / "hub" / "web" / "js"
        views = (web / "views.js").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"pw\.length < (\d+)", views), [str(accounts.MIN_PASSWORD)])
        for name, label in (("views.js", r"\((\d+) caractères au moins\)"), ("lang-en.js", r"at least (\d+) characters")):
            found = set(re.findall(label, (web / name).read_text(encoding="utf-8")))
            self.assertEqual(found, {str(accounts.MIN_PASSWORD)}, name)


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


class TestRights(unittest.TestCase):
    """Voir et télécharger, par compte et par bibliothèque : sans ligne tout est permis, l'administrateur n'a jamais de ligne."""

    LIBS = {"films", "livres"}

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.db = Database(root / "hub.db")
        self.admin = accounts.create(self.db, "chef", "un-mot-de-passe")
        self.alice = accounts.create(self.db, "alice", "un-mot-de-passe")

    def refused(self, call, status=400):
        with self.assertRaises(AccountError) as caught:
            call()
        self.assertEqual(caught.exception.status, status)
        return caught.exception.message

    def test_without_a_row_everything_is_allowed_and_an_administrator_needs_none(self):
        granted = accounts.grants(self.db, self.alice["id"])
        self.assertEqual(granted, {})
        self.assertTrue(all(accounts.allows("user", granted, lib, perm) for lib in self.LIBS for perm in ("view", "download")))
        self.assertTrue(accounts.allows("admin", {"films": (False, False, False)}, "films", "download"))           # même avec une ligne égarée, l'administrateur peut tout

    def test_the_rights_are_stored_per_library_and_read_back(self):
        accounts.set_rights(self.db, self.alice["id"], self.LIBS, {"films": {"download": False}, "livres": {"view": False}})
        granted = accounts.grants(self.db, self.alice["id"])
        self.assertEqual(granted, {"films": (True, True, False), "livres": (False, False, False)})
        self.assertEqual(accounts.rights_json("user", granted, ["films", "livres", "musique"]),
                         {"films": {"view": True, "play": True, "download": False}, "livres": {"view": False, "play": False, "download": False}, "musique": {"view": True, "play": True, "download": True}})
        self.assertEqual(accounts.grants(self.db, self.admin["id"]), {})              # l'autre compte n'a rien reçu

    def test_a_stored_play_or_download_never_outlives_view(self):
        self.db.execute("INSERT INTO rights(user_id, lib, view, play, download) VALUES(?,?,0,1,1)", (self.alice["id"], "films"))      # une ligne incohérente (édition à la main)
        granted = accounts.grants(self.db, self.alice["id"])
        self.assertEqual(granted["films"], (False, False, False))
        self.assertFalse(accounts.allows("user", granted, "films", "play"))
        self.assertFalse(accounts.allows("user", granted, "films", "download"))

    def test_play_and_download_are_independent_of_each_other(self):
        accounts.set_rights(self.db, self.alice["id"], self.LIBS, {"films": {"play": False}, "livres": {"download": False}})
        granted = accounts.grants(self.db, self.alice["id"])
        self.assertEqual(granted, {"films": (True, False, True), "livres": (True, True, False)})
        self.assertEqual([accounts.allows("user", granted, "films", p) for p in accounts.RIGHTS], [True, False, True])
        self.assertEqual([accounts.allows("user", granted, "livres", p) for p in accounts.RIGHTS], [True, True, False])

    def test_giving_everything_back_deletes_the_row(self):
        accounts.set_rights(self.db, self.alice["id"], self.LIBS, {"films": {"view": False}})
        accounts.set_rights(self.db, self.alice["id"], self.LIBS, {"films": {"view": True, "play": True, "download": True}})
        self.assertEqual(self.db.query("SELECT * FROM rights"), [])

    def test_a_bad_request_changes_nothing(self):
        for bad in (None, [], {}, {"inconnue": {"view": False}}, {"films": None}, {"films": {}}, {"films": {"edit": True}}, {"films": {"play": "oui"}}, {"films": {"view": 1}}, {"films": {"view": "oui"}},
                    {"films": {"view": False}, "inconnue": {"view": False}}):
            self.refused(lambda: accounts.set_rights(self.db, self.alice["id"], self.LIBS, bad))
        self.assertEqual(self.db.query("SELECT * FROM rights"), [])

    def test_an_administrator_and_an_unknown_account_are_refused(self):
        self.assertEqual(self.refused(lambda: accounts.set_rights(self.db, self.admin["id"], self.LIBS, {"films": {"view": False}})), "un administrateur a tous les droits")
        self.refused(lambda: accounts.set_rights(self.db, "inconnu", self.LIBS, {"films": {"view": False}}), 404)

    def test_deleting_the_account_deletes_its_rights(self):
        accounts.set_rights(self.db, self.alice["id"], self.LIBS, {"films": {"view": False}})
        accounts.delete(self.db, self.alice["id"])
        self.assertEqual(self.db.query("SELECT * FROM rights"), [])

    def test_a_media_token_is_not_a_session_token_and_the_other_way_round(self):
        session, media = accounts.session_token("s", self.alice), accounts.media_token("s", self.alice)
        self.assertNotEqual(session, media)
        self.assertEqual(accounts.user_for_token(self.db, "s", session)["id"], self.alice["id"])
        self.assertEqual(accounts.user_for_token(self.db, "s", media, "media")["id"], self.alice["id"])
        self.assertIsNone(accounts.user_for_token(self.db, "s", media))
        self.assertIsNone(accounts.user_for_token(self.db, "s", session, "media"))
        accounts.revoke_sessions(self.db, self.alice["id"])
        self.assertIsNone(accounts.user_for_token(self.db, "s", media, "media"))              # révoqué avec les sessions


class TestLimitsAndLogin(AccountsDb):
    """Les bornes d'un nom et d'un mot de passe, et la connexion sans nom quand il n'y a qu'un compte."""

    def test_a_name_and_a_password_of_exactly_the_longest_size_are_accepted_one_more_is_not(self):
        name = "n" * accounts.MAX_NAME
        alice = accounts.create(self.db, name, "p" * accounts.MAX_PASSWORD)
        self.assertEqual(alice["name"], name)
        self.assertEqual(accounts.authenticate(self.db, name, "p" * accounts.MAX_PASSWORD)["id"], alice["id"])
        self.assertIn("de 1 à 40 caractères", self.refused(lambda: accounts.create(self.db, "n" * (accounts.MAX_NAME + 1), "p" * 12)))
        self.assertIn("doit faire de 12 à 200", self.refused(lambda: accounts.create(self.db, "bob", "p" * (accounts.MAX_PASSWORD + 1))))

    def test_the_only_account_opens_without_a_name_but_two_accounts_need_one(self):
        alice = accounts.create(self.db, "alice", "un-mot-de-passe")
        for nameless in (None, ""):
            self.assertEqual(accounts.authenticate(self.db, nameless, "un-mot-de-passe")["id"], alice["id"])
            self.assertEqual(self.refused(lambda: accounts.authenticate(self.db, nameless, "autre-mot-de-passe"), 401), "mot de passe incorrect")
        accounts.create(self.db, "bob", "un-mot-de-passe")
        for nameless in (None, ""):
            self.assertEqual(self.refused(lambda: accounts.authenticate(self.db, nameless, "un-mot-de-passe"), 401), "nom d'utilisateur requis")
        self.assertEqual(accounts.authenticate(self.db, "bob", "un-mot-de-passe")["name"], "bob")


class TestVerifyPassword(unittest.TestCase):
    """Les deux formes de haché que le hub sait relire, et tout le reste refusé."""

    def pbkdf2(self, password, rounds=1000):
        salt = b"sel-de-test-1234"
        return f"pbkdf2${rounds}${salt.hex()}${hashlib.pbkdf2_hmac('sha256', password.encode(), salt, rounds, 32).hex()}"

    def test_a_pbkdf2_hash_opens_for_its_password_and_for_no_other(self):
        stored = self.pbkdf2("le-bon-mot-de-passe")
        self.assertTrue(accounts.verify_password("le-bon-mot-de-passe", stored))
        self.assertFalse(accounts.verify_password("un-autre-mot-de-passe", stored))
        rounds, salt, digest = stored.split("$")[1:]
        self.assertFalse(accounts.verify_password("le-bon-mot-de-passe", f"pbkdf2${int(rounds) + 1}${salt}${digest}"))       # le nombre de tours compte

    def test_a_hash_that_is_not_readable_never_opens(self):
        for stored in ("", "inconnu", "bcrypt$10$sel$hach", "scrypt$16384$8$1$zz$zz", "scrypt$16384", "pbkdf2$abc$00$00", "pbkdf2$1000$00", "pbkdf2$1000$00$00", None):
            self.assertFalse(accounts.verify_password("le-bon-mot-de-passe", stored), repr(stored))


class TestVlcToken(unittest.TestCase):
    """Le jeton VLC : un compte, un seul fichier, `VLC_SECONDS` de vie."""
    NOW = 1_800_000_000

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.db = Database(root / "hub.db")
        self.alice = accounts.create(self.db, "alice", "un-mot-de-passe")
        self.token = accounts.vlc_token("s", self.alice, "e1", now=self.NOW)

    def opened(self, token=None, entry="e1", at=None):
        return accounts.user_for_vlc_token(self.db, "s", token or self.token, entry, now=self.NOW if at is None else at)

    def test_it_expires_after_vlc_seconds_to_the_second(self):
        uid, entry, expires, mac = self.token.split(".")
        self.assertEqual((uid, entry, expires), (self.alice["id"], "e1", str(self.NOW + accounts.VLC_SECONDS)))
        self.assertEqual(self.opened()["id"], self.alice["id"])
        self.assertEqual(self.opened(at=self.NOW + accounts.VLC_SECONDS - 1)["id"], self.alice["id"])
        self.assertIsNone(self.opened(at=self.NOW + accounts.VLC_SECONDS))

    def test_it_opens_its_own_file_only(self):
        self.assertIsNone(self.opened(entry="e2"))

    def test_a_forged_mac_an_unknown_account_and_a_deleted_one_open_nothing(self):
        uid, entry, expires, mac = self.token.split(".")
        forged = f"{uid}.{entry}.{expires}.{'0' * 64}"
        self.assertIsNone(self.opened(forged))
        self.assertIsNone(self.opened(f"{uid}.{entry}.{int(expires) + 1}.{mac}"))                    # repousser l'expiration
        self.assertIsNone(self.opened(f"inconnu.{entry}.{expires}.{mac}"))
        self.assertIsNone(self.opened("pas-un-jeton"))
        bob = accounts.create(self.db, "bob", "un-mot-de-passe")
        self.assertIsNone(self.opened(self.token.replace(uid, bob["id"], 1)))                       # le jeton d'un autre compte
        accounts.delete(self.db, bob["id"])
        self.assertIsNone(self.opened(accounts.vlc_token("s", bob, "e1", now=self.NOW)))
        accounts.revoke_sessions(self.db, self.alice["id"])
        self.assertIsNone(self.opened())


class TestAdministrators(AccountsDb):
    def test_an_administrator_can_be_deleted_while_another_remains_but_never_the_last(self):
        first = accounts.create(self.db, "chef", "un-mot-de-passe")
        second = accounts.create(self.db, "adjoint", "un-mot-de-passe", "admin")
        accounts.delete(self.db, first["id"])
        self.assertEqual([r["name"] for r in accounts.rows(self.db)], ["adjoint"])
        self.assertEqual(self.refused(lambda: accounts.delete(self.db, second["id"])), "impossible de supprimer le dernier administrateur")
        self.assertEqual([r["name"] for r in accounts.rows(self.db)], ["adjoint"])


class TestSeed(AccountsDb):
    """Au démarrage, `HUB_PASSWORD` crée l'administrateur d'un hub sans compte, et plus rien ensuite."""

    def cfg(self, password, admin="chef"):
        return types.SimpleNamespace(password=password, admin=admin)

    def test_the_password_of_the_settings_creates_the_administrator_even_when_it_is_short(self):
        accounts.seed(self.db, self.cfg("court"))
        chef, = accounts.rows(self.db)
        self.assertEqual((chef["name"], chef["role"]), ("chef", "admin"))
        self.assertEqual(accounts.authenticate(self.db, "chef", "court")["id"], chef["id"])

    def test_without_a_password_or_with_accounts_already_there_nothing_is_created(self):
        accounts.seed(self.db, self.cfg(""))
        self.assertEqual(accounts.rows(self.db), [])
        accounts.seed(self.db, self.cfg("premier-mot-de-passe"))
        accounts.seed(self.db, self.cfg("second-mot-de-passe", admin="autre"))
        self.assertEqual([r["name"] for r in accounts.rows(self.db)], ["chef"])
        self.assertEqual(accounts.authenticate(self.db, "chef", "premier-mot-de-passe")["name"], "chef")


if __name__ == "__main__":
    unittest.main()
