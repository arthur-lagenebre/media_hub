"""La création d'un compte : nom, rôle, mot de passe, et ce qui change pour le premier."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from hub import accounts, prefs
from hub.accounts import AccountError
from hub.db import Database


class TestCreate(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.db = Database(root / "hub.db")

    def refused(self, call, status=400):
        with self.assertRaises(AccountError) as caught:
            call()
        self.assertEqual(caught.exception.status, status)
        return caught.exception.message

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

    def test_without_the_check_a_password_must_still_be_a_non_empty_text(self):
        for password in ("", None, 1234, b"octets"):
            self.assertIn("mot de passe vide", self.refused(lambda: accounts.create(self.db, "alice", password, check=False)), repr(password))
        self.assertEqual(accounts.rows(self.db), [])


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
        self.assertTrue(accounts.allows("admin", {"films": (False, False)}, "films", "download"))           # même avec une ligne égarée, l'administrateur peut tout

    def test_the_rights_are_stored_per_library_and_read_back(self):
        accounts.set_rights(self.db, self.alice["id"], self.LIBS, {"films": {"download": False}, "livres": {"view": False}})
        granted = accounts.grants(self.db, self.alice["id"])
        self.assertEqual(granted, {"films": (True, False), "livres": (False, False)})
        self.assertEqual(accounts.rights_json("user", granted, ["films", "livres", "musique"]),
                         {"films": {"view": True, "download": False}, "livres": {"view": False, "download": False}, "musique": {"view": True, "download": True}})
        self.assertEqual(accounts.grants(self.db, self.admin["id"]), {})              # l'autre compte n'a rien reçu

    def test_a_stored_download_never_outlives_view(self):
        self.db.execute("INSERT INTO rights(user_id, lib, view, download) VALUES(?,?,0,1)", (self.alice["id"], "films"))      # une ligne incohérente (édition à la main)
        granted = accounts.grants(self.db, self.alice["id"])
        self.assertEqual(granted["films"], (False, False))
        self.assertFalse(accounts.allows("user", granted, "films", "download"))

    def test_giving_everything_back_deletes_the_row(self):
        accounts.set_rights(self.db, self.alice["id"], self.LIBS, {"films": {"view": False}})
        accounts.set_rights(self.db, self.alice["id"], self.LIBS, {"films": {"view": True, "download": True}})
        self.assertEqual(self.db.query("SELECT * FROM rights"), [])

    def test_a_bad_request_changes_nothing(self):
        for bad in (None, [], {}, {"inconnue": {"view": False}}, {"films": None}, {"films": {}}, {"films": {"edit": True}}, {"films": {"view": 1}}, {"films": {"view": "oui"}},
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


if __name__ == "__main__":
    unittest.main()
