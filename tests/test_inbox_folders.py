"""La boîte de réception : le dossier d'un compte porte son nom (nettoyage, collisions, lien gardé dans la base), la migration des anciens dossiers (nommés d'après l'identifiant),
une boîte sur un autre volume que HUB_DATA (HUB_INBOX), et le confinement des chemins (traversées, liens, jonctions)."""

from __future__ import annotations

import io
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from hub import inbox
from hub.inbox import Inbox, InboxError
from tests.test_inbox import TempCase, add_user


class TestCleanFolder(unittest.TestCase):
    def test_a_plain_name_keeps_its_accents_its_case_and_its_spaces(self):
        self.assertEqual(inbox.clean_folder("Léa Dupont"), "Léa Dupont")
        self.assertEqual(inbox.clean_folder("Zoë (maman)"), "Zoë (maman)")

    def test_the_name_is_normalised_to_nfc(self):
        self.assertEqual(inbox.clean_folder("Léa"), "Léa")

    def test_separators_and_the_characters_windows_refuses_become_underscores(self):
        for raw in ("a/b", "a\\b", "a:b", "a?b", "a*b", 'a"b', "a<b", "a>b", "a|b", "a\x00b", "a\nb", "a b", "a‮b"):
            self.assertEqual(inbox.clean_folder(raw), "a_b", repr(raw))
        self.assertEqual(inbox.clean_folder("../../etc"), "_.._etc")           # un chemin n'est jamais interprété : le nom entier devient un seul élément

    def test_no_dot_or_space_at_the_start_or_the_end_and_so_never_a_hidden_name(self):
        for raw, wanted in ((".cache", "cache"), ("...x", "x"), ("x. . ", "x"), ("  x  ", "x"), (". x .", "x")):
            self.assertEqual(inbox.clean_folder(raw), wanted, raw)

    def test_a_name_that_is_nothing_once_cleaned_falls_back_to_a_neutral_name(self):
        for raw in ("", " ", ".", "..", "...", " . ", None, 12, b"x", ["a"]):
            self.assertEqual(inbox.clean_folder(raw), inbox.FOLDER_FALLBACK, repr(raw))

    def test_the_names_windows_reserves_are_prefixed(self):
        for raw in ("CON", "nul", "Aux", "PRN", "COM1", "lpt9", "con.fr", "NUL ."):
            cleaned = inbox.clean_folder(raw)
            self.assertTrue(cleaned.startswith("_"), raw)
            self.assertFalse(inbox._is_reserved(cleaned), raw)
        for raw in ("CONSOLE", "com10", "confiance", "Nulle"):
            self.assertEqual(inbox.clean_folder(raw), raw, raw)

    def test_a_long_name_is_cut_in_bytes_without_breaking_a_character(self):
        for raw in ("é" * 400, "a" * 400, "日本語" * 100, "😀" * 100):
            cleaned = inbox.clean_folder(raw)
            self.assertLessEqual(len(cleaned.encode("utf-8")), inbox.FOLDER_BYTES, raw[:3])
            self.assertGreater(len(cleaned), 10)

    def test_what_it_produces_is_always_a_valid_folder_name(self):
        for raw in ("Léa", "a/b", "CON", "..", "", "é" * 400, ".x", "x.", "a\x00b", "nul.txt", "  ", "😀", "Arthur Lagenebre", "a:b?c", "COM1"):
            self.assertTrue(inbox.is_folder_name(inbox.clean_folder(raw)), repr(raw))


class TestIsFolderName(unittest.TestCase):
    def test_what_is_a_single_clean_element_is_accepted(self):
        for name in ("Léa", "Léa (2)", "default", "3f9a1c0e5b7d2a46", "a b", "a.b", "compte", "_CON", "Nulle", "x" * 255):
            self.assertTrue(inbox.is_folder_name(name), name)

    def test_a_path_a_dot_a_hidden_a_reserved_or_a_doubtful_name_is_refused(self):
        for name in ("", ".", "..", "a/b", "a\\b", "/", "\\", "../x", "a\x00b", ".caché", ".hidden", "CON", "nul", "con.txt", "COM1", "a ", " a", "a.", "a:b", "a*b", "a b",
                     "x" * 256, "é" * 128, "a\udc80b", None, 5, b"x", ["a"]):
            self.assertFalse(inbox.is_folder_name(name), repr(name))

    def test_the_encodings_of_a_traversal_are_not_decoded_here_they_are_just_names(self):
        for name in ("%2e%2e", "%2f", "%2e%2e%2f", "%00"):
            self.assertTrue(inbox.is_folder_name(name), name)       # la route a déjà décodé l'URL : ces textes ne désignent que des dossiers qui n'existent pas


class FolderCase(TempCase):
    def user(self, name, uid, **kw):
        add_user(self.db, uid, name, **kw)
        return uid

    def dirs(self):
        root = self.tmp / "inbox"
        return sorted(p.name for p in root.iterdir()) if root.is_dir() else []

    def recorded(self, uid):
        return self.db.one("SELECT inbox_dir FROM users WHERE id=?", (uid,))["inbox_dir"]


class TestAccountFolder(FolderCase):
    def test_the_folder_carries_the_name_of_the_account_accents_included(self):
        uid = self.user("Léa Dupont", "9f8e7d6c5b4a3210")
        self.assertEqual(self.inbox.folder_for(uid), "Léa Dupont")
        self.assertEqual(self.dirs(), ["Léa Dupont"])
        self.assertEqual(self.recorded(uid), "Léa Dupont")

    def test_a_file_is_stored_in_it_and_listed_under_its_name(self):
        uid = self.user("Zoë", "9f8e7d6c5b4a3210")
        self.assertEqual(self.put("a.mkv", account=uid), {"name": "a.mkv", "size": 3, "account": "Zoë", "dir": ""})
        self.assertEqual((self.tmp / "inbox" / "Zoë" / "a.mkv").read_bytes(), b"abc")
        self.assertEqual(self.inbox.listing(uid), [{"account": "Zoë", "dir": "", "name": "a.mkv", "size": 3, "date": mock.ANY}])
        self.assertEqual(self.inbox.owners()["Zoë"], "Zoë")
        self.assertEqual(self.inbox.used(uid), 3)

    def test_it_is_fixed_at_the_first_use_and_a_new_name_does_not_move_it(self):
        uid = self.user("Léa", "9f8e7d6c5b4a3210")
        self.assertEqual(self.inbox.folder_for(uid), "Léa")
        self.db.execute("UPDATE users SET name='Marie' WHERE id=?", (uid,))      # le hub n'a pas de renommage de compte : si un jour il en a un, le lien est dans la base
        self.assertEqual(self.inbox.folder_for(uid), "Léa")
        self.assertEqual(self.put("a.mkv", account=uid)["account"], "Léa")
        self.assertEqual(self.dirs(), ["Léa"])
        self.assertEqual(self.inbox.owners(), {"Léa": "Marie"})

    def test_a_second_use_does_not_make_another_folder(self):
        uid = self.user("Léa", "9f8e7d6c5b4a3210")
        self.assertEqual({self.inbox.folder_for(uid) for _ in range(5)}, {"Léa"})
        self.assertEqual(self.dirs(), ["Léa"])

    def test_names_that_differ_only_by_case_get_two_folders_even_if_the_filesystem_would_merge_them(self):
        """Deux comptes en vie ne se distinguent jamais par la casse (le nom est unique sans en tenir compte) ; un dossier de compte supprimé ou posé à la main, si : NTFS et SMB le confondraient avec le nouveau."""
        (self.tmp / "inbox" / "Léa").mkdir(parents=True)                           # le dossier d'un compte supprimé
        (self.tmp / "inbox" / "LÉA (2)").mkdir()
        uid = self.user("léa", "1")
        self.assertEqual(self.inbox.folder_for(uid), "léa (3)")
        self.assertEqual(self.dirs(), ["LÉA (2)", "Léa", "léa (3)"])

    def test_a_name_still_given_by_the_base_is_not_given_again_to_a_name_that_differs_by_case_even_if_its_folder_is_gone(self):
        """Le dossier de « Léa: » (« Léa_ ») a été effacé à la main : la base le tient toujours pour sien. « léa_ » ne le reçoit pas, sinon, sur NTFS ou SMB, deux comptes se partageraient le même dossier le jour où le premier revient."""
        first, second = self.user("Léa:", "1"), self.user("léa_", "2")
        self.assertEqual(self.inbox.folder_for(first), "Léa_")
        shutil.rmtree(self.tmp / "inbox" / "Léa_")
        self.assertEqual(self.inbox.folder_for(second), "léa_ (2)")
        self.assertEqual(self.inbox.folder_for(first), "Léa_")
        self.assertEqual(self.dirs(), ["Léa_", "léa_ (2)"])

    def test_every_numbered_name_taken_is_a_409_and_leaves_no_new_folder(self):
        for name in ("Léa", "Léa (2)", "Léa (3)"):
            (self.tmp / "inbox" / name).mkdir(parents=True, exist_ok=True)
        uid = self.user("Léa", "1")
        with mock.patch.object(inbox, "SAME_NAME", 3), self.assertRaises(InboxError) as caught:
            self.inbox.folder_for(uid)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.dirs(), ["Léa", "Léa (2)", "Léa (3)"])
        self.assertIsNone(self.recorded(uid))

    def test_a_folder_name_the_base_holds_that_is_not_a_folder_is_replaced_before_any_folder_is_made_and_writes_nothing_outside(self):
        """La base a été touchée à la main (« ../dehors ») : le lien est réécrit proprement d'après le nom du compte, jamais une écriture hors de la boîte, jamais un dossier orphelin."""
        uid = self.user("Léa", "1", inbox_dir="../dehors")
        self.assertEqual(self.put("x.mkv", account=uid)["account"], "Léa")
        self.assertEqual(self.recorded(uid), "Léa")
        self.assertFalse((self.tmp / "dehors").exists())
        self.assertEqual(self.dirs(), ["Léa"])
        self.assertEqual([p.name for p in (self.tmp / "inbox" / "Léa").iterdir()], ["x.mkv"])

    def test_a_dubious_folder_name_in_the_base_does_not_make_a_new_empty_folder_at_each_attempt(self):
        for n, bad in enumerate(("../x", "a/b", ".caché", "CON", "a\b", "x" * 300)):
            uid = self.user("Léa" + str(n), str(n), inbox_dir=bad)
            for attempt in range(3):
                self.assertEqual(self.put(f"x{attempt}.mkv", account=uid)["account"], f"Léa{n}", bad)
            self.assertEqual(self.recorded(uid), f"Léa{n}", bad)
        self.assertEqual(self.dirs(), [f"Léa{n}" for n in range(6)])                     # un dossier par compte, pas un de plus

    def test_a_dubious_folder_name_in_the_base_does_not_hide_an_old_folder_named_after_the_identifier(self):
        uid = self.user("Arthur", "default", inbox_dir="../x")
        (self.tmp / "inbox" / "default").mkdir(parents=True)
        (self.tmp / "inbox" / "default" / "ancien.mkv").write_bytes(b"1")
        self.assertEqual(self.put("neuf.mkv", account=uid)["account"], "default")
        self.assertEqual(self.recorded(uid), "default")
        self.assertEqual(self.dirs(), ["default"])

    def test_a_folder_made_for_a_name_the_base_does_not_keep_is_removed_when_empty(self):
        uid = self.user("Léa", "1")
        with mock.patch.object(Inbox, "folder_of", return_value="Autre"):              # la base retient un autre nom que celui qui vient d'être créé
            self.assertEqual(self.inbox.folder_for(uid), "Autre")
        self.assertNotIn("Léa", self.dirs())

    def test_names_that_differ_only_by_the_form_of_their_accents_collide_too(self):
        one, two = self.user("Léa", "1"), self.user("Léa", "2")
        self.assertEqual(self.inbox.folder_for(one), "Léa")
        self.assertEqual(self.inbox.folder_for(two), "Léa (2)")

    def test_names_that_become_the_same_once_cleaned_get_their_own_folder(self):
        ids = [self.user(name, str(n)) for n, name in enumerate(("a:b", "a?b", "a*b", "a/b"))]
        self.assertEqual([self.inbox.folder_for(uid) for uid in ids], ["a_b", "a_b (2)", "a_b (3)", "a_b (4)"])

    def test_a_name_with_nothing_left_gets_the_neutral_name_then_numbers(self):
        ids = [self.user(name, str(n)) for n, name in enumerate(("...", " . ", ".."))]
        self.assertEqual([self.inbox.folder_for(uid) for uid in ids], ["compte", "compte (2)", "compte (3)"])

    def test_a_reserved_windows_name_is_prefixed(self):
        self.assertEqual(self.inbox.folder_for(self.user("NUL", "1")), "_NUL")
        self.assertEqual(self.inbox.folder_for(self.user("com1", "2")), "_com1")

    def test_a_folder_that_already_exists_is_never_taken_over_or_written_into(self):
        (self.tmp / "inbox" / "Léa").mkdir(parents=True)
        (self.tmp / "inbox" / "Léa" / "intact.mkv").write_bytes(b"avant")
        (self.tmp / "inbox" / "EVE").mkdir()                                       # un autre dossier, à la casse près
        lea, eve = self.user("Léa", "1"), self.user("Eve", "2")
        self.assertEqual((self.inbox.folder_for(lea), self.inbox.folder_for(eve)), ("Léa (2)", "Eve (2)"))
        self.assertEqual(sorted(p.name for p in (self.tmp / "inbox" / "Léa").iterdir()), ["intact.mkv"])
        self.assertEqual((self.tmp / "inbox" / "Léa" / "intact.mkv").read_bytes(), b"avant")

    def test_a_deleted_account_keeps_its_folder_and_its_files_and_nobody_inherits_them(self):
        old = self.user("Léa", "1")
        self.put("souvenir.mkv", b"12345", account=old)
        self.db.execute("DELETE FROM users WHERE id=?", (old,))
        self.assertEqual((self.tmp / "inbox" / "Léa" / "souvenir.mkv").read_bytes(), b"12345")
        self.assertEqual([(f["account"], f["name"]) for f in self.inbox.listing()], [("Léa", "souvenir.mkv")])
        self.assertNotIn("Léa", self.inbox.owners())                                # c'est ce qui le désigne, dans la liste de l'administrateur, comme celui d'un compte supprimé
        self.assertEqual(self.inbox.path_of("Léa", "souvenir.mkv").read_bytes(), b"12345")
        new = self.user("Léa", "2")                                                 # un nouveau compte du même nom : un autre dossier
        self.assertEqual(self.put("neuf.mkv", account=new)["account"], "Léa (2)")
        self.assertEqual([f["name"] for f in self.inbox.listing(new)], ["neuf.mkv"])
        self.assertEqual(sorted(p.name for p in (self.tmp / "inbox" / "Léa").iterdir()), ["souvenir.mkv"])

    def test_an_unknown_account_is_refused_and_leaves_nothing_behind(self):
        with self.assertRaises(InboxError) as caught:
            self.inbox.folder_for("personne")
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.dirs(), [])

    def test_an_account_that_has_sent_nothing_has_no_folder_and_nothing_to_list(self):
        uid = self.user("Léa", "1")
        self.assertEqual((self.inbox.listing(uid), self.inbox.used(uid), self.inbox.folder_of(uid), self.inbox.folder_of("personne")), ([], 0, None, None))

    def test_a_folder_removed_by_hand_comes_back_under_the_same_name(self):
        uid = self.user("Léa", "1")
        self.put("a.mkv", account=uid)
        shutil.rmtree(self.tmp / "inbox" / "Léa")
        self.assertEqual(self.put("b.mkv", account=uid)["account"], "Léa")
        self.assertEqual(self.dirs(), ["Léa"])

    def test_a_folder_named_after_the_identifier_is_taken_over_as_it_is(self):
        """Un dossier d'avant, que `migrate` n'a pas encore renommé : le compte continue d'y envoyer, rien n'est perdu ni dupliqué."""
        uid = self.user("Arthur", "default")
        (self.tmp / "inbox" / "default").mkdir(parents=True)
        (self.tmp / "inbox" / "default" / "ancien.mkv").write_bytes(b"1")
        self.assertEqual(self.put("neuf.mkv", account=uid)["account"], "default")
        self.assertEqual(self.dirs(), ["default"])
        self.assertEqual(self.recorded(uid), "default")
        self.assertEqual(sorted(f["name"] for f in self.inbox.listing(uid)), ["ancien.mkv", "neuf.mkv"])

    def test_an_old_folder_named_after_the_identifier_is_not_taken_when_another_account_holds_that_name(self):
        """L'administrateur d'identifiant `default` et un second compte NOMMÉ « default » : celui-ci envoie d'abord et obtient `default/` ; l'administrateur n'y est pas admis."""
        guest = self.user("default", "bb")
        self.assertEqual(self.put("a.mkv", account=guest)["account"], "default")
        boss = self.user("admin", "default", role="admin")
        self.assertEqual(self.put("b.mkv", account=boss)["account"], "admin")
        self.assertEqual((self.recorded(guest), self.recorded(boss)), ("default", "admin"))
        self.assertEqual(self.dirs(), ["admin", "default"])
        self.assertEqual([f["name"] for f in self.inbox.listing(guest)], ["a.mkv"])
        self.assertEqual([f["name"] for f in self.inbox.listing(boss)], ["b.mkv"])

    def test_an_old_folder_is_not_taken_either_when_the_other_account_holds_it_with_another_case(self):
        holder = self.user("DEFAULT", "bb", inbox_dir="DEFAULT")
        (self.tmp / "inbox" / "default").mkdir(parents=True)                            # sur NTFS ou SMB : le même dossier que « DEFAULT »
        boss = self.user("admin", "default", role="admin")
        self.assertEqual(self.inbox.folder_for(boss), "admin")
        self.assertEqual((self.recorded(holder), self.recorded(boss)), ("DEFAULT", "admin"))

    def test_a_hexadecimal_old_folder_is_not_taken_when_another_account_holds_that_name(self):
        uid = "3f9a1c0e5b7d2a46"
        holder = self.user(uid.upper(), "bb", inbox_dir=uid.upper())
        (self.tmp / "inbox" / uid).mkdir(parents=True)
        owner = self.user("Léa", uid)
        self.assertEqual(self.inbox.folder_for(owner), "Léa")
        self.assertEqual(self.recorded(holder), uid.upper())

    def test_accounts_cleaned_to_the_same_name_at_once_never_share_a_folder(self):
        ids = [self.user("a" + ch + "b", str(n)) for n, ch in enumerate(':?*|<>"/\\')]
        got, start = [], threading.Barrier(len(ids))

        def run(uid):
            start.wait(10)
            got.append(self.inbox.folder_for(uid))

        threads = [threading.Thread(target=run, args=(uid,)) for uid in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(len({inbox._same(name) for name in got}), len(ids), got)
        self.assertEqual(self.dirs(), sorted(got))
        self.assertEqual(sorted(self.recorded(uid) for uid in ids), sorted(got))

    def test_a_folder_that_is_a_file_is_never_written_into(self):
        uid = self.user("Léa", "1", inbox_dir="Léa")
        (self.tmp / "inbox").mkdir()
        (self.tmp / "inbox" / "Léa").write_text("un fichier à la place du dossier", encoding="utf-8")
        with mock.patch.object(sys, "stderr", io.StringIO()), self.assertRaises(InboxError) as caught:
            self.put("x.mkv", account=uid)
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn(str(self.tmp), caught.exception.message)

    def test_an_error_while_recording_the_folder_removes_the_empty_folder_it_made(self):
        uid = self.user("Léa", "1")
        with mock.patch.object(Inbox, "_record", side_effect=sqlite3.OperationalError("base verrouillée")), self.assertRaises(sqlite3.OperationalError):
            self.inbox.folder_for(uid)
        self.assertEqual(self.dirs(), [])
        self.assertIsNone(self.recorded(uid))
        self.assertEqual(self.inbox.folder_for(uid), "Léa")                         # et la fois suivante est la bonne


class TestMigration(FolderCase):
    def legacy(self, uid, name, files=("film.mkv",), parts=("en cours.mkv.part",)):
        add_user(self.db, uid, name)
        folder = self.tmp / "inbox" / uid
        folder.mkdir(parents=True, exist_ok=True)
        for f in files:
            (folder / f).write_bytes(f.encode())
        for f in parts:
            (folder / f).write_bytes(b"partiel")
        return folder

    def tree(self):
        return sorted(p.relative_to(self.tmp / "inbox").as_posix() + (":" + p.read_text("utf-8") if p.is_file() else "") for p in (self.tmp / "inbox").rglob("*"))

    def test_the_default_folder_is_renamed_after_the_first_account(self):
        self.legacy("default", "Arthur")
        self.assertEqual(self.inbox.migrate(), 1)
        self.assertEqual(self.dirs(), ["Arthur"])
        self.assertEqual(self.recorded("default"), "Arthur")
        self.assertEqual(self.tree(), ["Arthur", "Arthur/en cours.mkv.part:partiel", "Arthur/film.mkv:film.mkv"])        # les fichiers et le `.part` en cours ont suivi

    def test_a_hexadecimal_folder_is_renamed_after_its_account(self):
        self.legacy("3f9a1c0e5b7d2a46", "Léa Dupont")
        self.legacy("0123456789abcdef", "Zoë")
        self.assertEqual(self.inbox.migrate(), 2)
        self.assertEqual(self.dirs(), ["Léa Dupont", "Zoë"])
        self.assertEqual((self.recorded("3f9a1c0e5b7d2a46"), self.recorded("0123456789abcdef")), ("Léa Dupont", "Zoë"))
        self.assertEqual(self.inbox.listing("3f9a1c0e5b7d2a46")[0]["account"], "Léa Dupont")

    def test_running_it_again_changes_nothing(self):
        self.legacy("default", "Arthur")
        self.legacy("3f9a1c0e5b7d2a46", "Léa")
        self.inbox.migrate()
        before, recorded = self.tree(), (self.recorded("default"), self.recorded("3f9a1c0e5b7d2a46"))
        self.assertEqual((self.inbox.migrate(), self.inbox.migrate()), (0, 0))
        self.assertEqual(self.tree(), before)
        self.assertEqual((self.recorded("default"), self.recorded("3f9a1c0e5b7d2a46")), recorded)

    def test_a_new_inbox_is_not_a_migration(self):
        add_user(self.db, "default", "Arthur")
        self.assertEqual(self.inbox.migrate(), 0)
        self.assertFalse((self.tmp / "inbox").exists())
        self.assertIsNone(self.recorded("default"))                                 # rien envoyé : le dossier se fera au premier envoi

    def test_a_target_that_already_exists_is_not_overwritten_the_folder_comes_next_to_it(self):
        self.legacy("default", "Arthur")
        (self.tmp / "inbox" / "Arthur").mkdir()
        (self.tmp / "inbox" / "Arthur" / "film.mkv").write_text("un autre film, posé à la main", encoding="utf-8")
        (self.tmp / "inbox" / "ARTHUR (2)").mkdir()
        self.assertEqual(self.inbox.migrate(), 1)
        self.assertEqual((self.tmp / "inbox" / "Arthur" / "film.mkv").read_text("utf-8"), "un autre film, posé à la main")
        self.assertEqual(self.recorded("default"), "Arthur (3)")
        self.assertEqual((self.tmp / "inbox" / "Arthur (3)" / "film.mkv").read_bytes(), b"film.mkv")
        self.assertFalse((self.tmp / "inbox" / "default").exists())

    def test_a_target_that_appears_after_the_choice_is_still_not_overwritten(self):
        """Sous POSIX, renommer un dossier sur un dossier vide le remplace : un dossier apparu entre le choix du nom et le renommage ne doit pas être touché."""
        self.legacy("default", "Arthur")
        (self.tmp / "inbox" / "Arthur").mkdir()
        renamed = mock.Mock()
        with mock.patch.object(Inbox, "_taken", return_value=set()), mock.patch.object(inbox.os, "rename", renamed), mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(self.inbox.migrate(), 0)
        renamed.assert_not_called()
        self.assertEqual(self.dirs(), ["Arthur", "default"])
        self.assertEqual(self.recorded("default"), "default")

    def test_two_accounts_cleaned_to_the_same_name_each_keep_their_files(self):
        self.legacy("default", "a:b", files=("un.mkv",), parts=())
        self.legacy("3f9a1c0e5b7d2a46", "a?b", files=("deux.mkv",), parts=())
        self.assertEqual(self.inbox.migrate(), 2)
        self.assertEqual(self.tree(), ["a_b", "a_b (2)", "a_b (2)/deux.mkv:deux.mkv", "a_b/un.mkv:un.mkv"])

    def test_a_folder_that_will_not_move_stays_where_it_is_and_the_others_still_move(self):
        self.legacy("default", "Arthur")
        self.legacy("3f9a1c0e5b7d2a46", "Léa")
        real = os.rename

        def refuse_the_first(src, dst):
            if Path(src).name == "default":
                raise PermissionError(13, "Accès refusé")
            return real(src, dst)

        err = io.StringIO()
        with mock.patch.object(inbox.os, "rename", refuse_the_first), mock.patch.object(sys, "stderr", err):
            self.assertEqual(self.inbox.migrate(), 1)                               # ne lève rien : le démarrage continue
        self.assertEqual(self.dirs(), ["Léa", "default"])
        self.assertEqual((self.tmp / "inbox" / "default" / "film.mkv").read_bytes(), b"film.mkv")
        self.assertEqual(self.recorded("default"), "default")                       # l'ancien dossier reste celui du compte, dans la base aussi
        self.assertIn("Arthur", err.getvalue())
        self.assertIn("ancien nom", err.getvalue())
        self.assertEqual(self.inbox.listing("default")[0]["account"], "default")    # il sert toujours
        self.assertEqual(self.put("encore.mkv", account="default")["account"], "default")
        self.assertEqual(self.inbox.migrate(), 1)                                   # le démarrage suivant : le dossier cède, la suite se fait
        self.assertEqual(self.dirs(), ["Arthur", "Léa"])
        self.assertEqual(sorted(f["name"] for f in self.inbox.listing("default")), ["encore.mkv", "film.mkv"])

    def test_a_database_that_refuses_the_new_name_puts_the_folder_back(self):
        self.legacy("default", "Arthur")
        real = self.db.execute

        def refuse(sql, params=()):
            if sql == "UPDATE users SET inbox_dir=? WHERE id=?":
                raise sqlite3.OperationalError("disque plein")
            return real(sql, params)

        with mock.patch.object(self.db, "execute", refuse), mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(self.inbox.migrate(), 0)
        self.assertEqual(self.dirs(), ["default"])
        self.assertEqual(self.recorded("default"), "default")
        self.assertEqual((self.tmp / "inbox" / "default" / "film.mkv").read_bytes(), b"film.mkv")
        self.assertEqual(self.inbox.migrate(), 1)

    def test_a_folder_whose_account_is_gone_is_left_alone(self):
        folder = self.tmp / "inbox" / "3f9a1c0e5b7d2a46"
        folder.mkdir(parents=True)
        (folder / "orphelin.mkv").write_bytes(b"1")
        self.legacy("default", "Arthur")
        self.assertEqual(self.inbox.migrate(), 1)
        self.assertEqual(self.dirs(), ["3f9a1c0e5b7d2a46", "Arthur"])
        self.assertEqual((folder / "orphelin.mkv").read_bytes(), b"1")
        self.assertEqual([f["account"] for f in self.inbox.listing() if f["name"] == "orphelin.mkv"], ["3f9a1c0e5b7d2a46"])

    def test_a_folder_that_already_has_its_name_in_the_base_is_not_touched(self):
        add_user(self.db, "default", "Arthur", inbox_dir="Arthur")
        (self.tmp / "inbox" / "Arthur").mkdir(parents=True)
        (self.tmp / "inbox" / "Arthur" / "a.mkv").write_bytes(b"1")
        (self.tmp / "inbox" / "default").mkdir()                                    # un dossier étranger qui porte l'identifiant : ce n'est plus celui du compte
        (self.tmp / "inbox" / "default" / "autre.mkv").write_bytes(b"2")
        self.assertEqual(self.inbox.migrate(), 0)
        self.assertEqual(self.dirs(), ["Arthur", "default"])

    def test_an_account_named_like_its_identifier_keeps_its_folder(self):
        self.legacy("default", "default")
        self.assertEqual(self.inbox.migrate(), 0)
        self.assertEqual(self.dirs(), ["default"])
        self.assertEqual(self.recorded("default"), "default")
        self.assertEqual(self.inbox.migrate(), 0)

    def test_an_account_named_like_the_folder_of_another_does_not_swallow_it(self):
        self.legacy("default", "Arthur")
        add_user(self.db, "bb", "default")                                          # un compte nommé « default » : son dossier ne peut pas être celui de l'ancien « default »
        self.assertEqual(self.inbox.migrate(), 1)
        self.assertEqual(self.dirs(), ["Arthur"])
        self.assertEqual(self.inbox.folder_for("bb"), "default")
        self.assertEqual(self.dirs(), ["Arthur", "default"])
        self.assertEqual((self.tmp / "inbox" / "Arthur" / "film.mkv").read_bytes(), b"film.mkv")

    def test_the_folder_another_account_holds_in_the_base_is_not_renamed_after_the_account_whose_identifier_it_bears(self):
        """Le compte « default » (nom libre) tient `default/` dans la base ; l'administrateur d'identifiant `default` n'a rien : migrate ne déplace pas le dossier du premier chez le second."""
        self.legacy("default", "admin")
        add_user(self.db, "bb", "default", inbox_dir="default")
        self.assertEqual(self.inbox.migrate(), 0)
        self.assertEqual(self.dirs(), ["default"])
        self.assertEqual((self.tmp / "inbox" / "default" / "film.mkv").read_bytes(), b"film.mkv")
        self.assertEqual((self.recorded("bb"), self.recorded("default")), ("default", None))
        self.assertEqual(self.inbox.migrate(), 0)
        self.assertEqual([f["name"] for f in self.inbox.listing("bb")], ["film.mkv"])
        self.assertEqual(self.inbox.folder_for("default"), "admin")                  # l'administrateur aura son propre dossier au premier envoi

    def test_the_same_with_another_case_and_with_a_hexadecimal_identifier(self):
        self.legacy("3f9a1c0e5b7d2a46", "Léa")
        add_user(self.db, "bb", "3F9A1C0E5B7D2A46", inbox_dir="3F9A1C0E5B7D2A46")
        self.assertEqual(self.inbox.migrate(), 0)
        self.assertEqual(self.dirs(), ["3f9a1c0e5b7d2a46"])
        self.assertEqual((self.recorded("bb"), self.recorded("3f9a1c0e5b7d2a46")), ("3F9A1C0E5B7D2A46", None))

    def test_a_link_or_a_junction_named_like_an_identifier_is_not_followed_or_renamed(self):
        outside = self.tmp / "dehors"
        outside.mkdir()
        add_user(self.db, "default", "Arthur")
        (self.tmp / "inbox").mkdir()
        if sys.platform == "win32":
            import _winapi
            _winapi.CreateJunction(str(outside), str(self.tmp / "inbox" / "default"))
        else:
            os.symlink(outside, self.tmp / "inbox" / "default", target_is_directory=True)
        self.assertEqual(self.inbox.migrate(), 0)
        self.assertTrue(outside.is_dir())
        self.assertEqual(self.dirs(), ["default"])
        self.assertIsNone(self.recorded("default"))

    def test_a_base_that_cannot_be_read_does_not_stop_the_start(self):
        err = io.StringIO()
        with mock.patch.object(self.db, "query", side_effect=sqlite3.OperationalError("base illisible")), mock.patch.object(sys, "stderr", err):
            self.assertEqual(self.inbox.migrate(), 0)
        self.assertIn("migration impossible", err.getvalue())


class TestInboxOutsideTheDataFolder(FolderCase):
    """HUB_INBOX : la boîte est un autre volume que HUB_DATA ; l'espace libre et les `.part` se mesurent et se balaient là."""

    def setUp(self):
        super().setUp()
        self.volume = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.volume, True)
        self.box = self.at(self.volume / "HubReception")

    def at(self, path, **over):
        return Inbox(types.SimpleNamespace(**{"inbox": path, "inbox_keep_free": 0, "inbox_max_uploads": 2, **over}), self.db)

    def blocked(self):
        """Une boîte dont le chemin passe par un fichier : elle ne peut pas être créée."""
        (self.volume / "fichier").write_bytes(b"un fichier, pas un dossier")
        return self.at(self.volume / "fichier" / "boite")

    def test_the_files_the_folders_and_the_free_space_are_on_that_volume(self):
        usage = types.SimpleNamespace(total=1, used=0, free=10**9)
        with mock.patch.object(inbox.shutil, "disk_usage", return_value=usage) as measured:
            self.assertEqual(self.put("a.mkv", inbox_=self.box)["account"], "alice")
        self.assertEqual(Path(measured.call_args.args[0]), self.volume / "HubReception")
        self.assertTrue((self.volume / "HubReception" / "alice" / "a.mkv").is_file())
        self.assertFalse((self.tmp / "inbox").exists())                              # rien sous HUB_DATA

    def test_the_free_space_to_keep_is_the_one_of_the_inbox_volume_not_of_the_data_volume(self):
        box, asked = self.at(self.volume / "HubReception", inbox_keep_free=1000), []

        def usage(path):
            asked.append(Path(path))
            return types.SimpleNamespace(total=10**9, used=0, free=1005 if Path(path) == self.volume / "HubReception" else 10**12)

        with mock.patch.object(inbox.shutil, "disk_usage", usage):
            with self.assertRaises(InboxError) as caught:
                self.put("a.mkv", b"x" * 10, inbox_=box)
        self.assertEqual(caught.exception.status, 507)
        self.assertEqual(asked, [self.volume / "HubReception"])

    def test_the_old_parts_are_swept_on_that_volume_only(self):
        self.put("a.mkv", inbox_=self.box)
        old = self.volume / "HubReception" / "alice" / "perdu.mkv.part"
        old.write_bytes(b"x")
        near = self.tmp / "inbox" / "alice"
        near.mkdir(parents=True)
        (near / "ailleurs.mkv.part").write_bytes(b"x")                               # sous HUB_DATA : ce n'est pas la boîte
        self.assertEqual(self.box.sweep(time.time() + 2 * inbox.STALE), 1)
        self.assertFalse(old.exists())
        self.assertTrue((near / "ailleurs.mkv.part").exists())

    def test_a_missing_inbox_is_created_and_checked_as_usable(self):
        self.assertFalse((self.volume / "HubReception").exists())
        self.assertIsNone(self.box.check())
        self.assertTrue((self.volume / "HubReception").is_dir())
        self.assertEqual(list((self.volume / "HubReception").iterdir()), [])         # la sonde d'écriture ne laisse rien
        self.assertIsNone(self.box.check())

    def test_an_inbox_that_cannot_be_written_is_a_clear_message_at_startup(self):
        with mock.patch.object(inbox.tempfile, "TemporaryFile", side_effect=PermissionError(13, "Permission denied")):
            message = self.box.check()
        self.assertIn("HubReception", message)
        self.assertIn("HUB_INBOX", message)
        self.assertIn("Permission denied", message)

    def test_an_inbox_that_cannot_be_created_is_a_clear_message_too(self):
        self.assertIn("n'est pas utilisable", self.blocked().check())

    def test_an_upload_into_an_unusable_inbox_is_a_503_with_no_path_in_the_message_not_a_crash(self):
        box, err = self.blocked(), io.StringIO()
        with mock.patch.object(sys, "stderr", err), self.assertRaises(InboxError) as caught:
            self.put("a.mkv", inbox_=box)
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn(str(self.volume), caught.exception.message)
        self.assertIn("fichier", err.getvalue())                                     # le journal dit où
        self.assertEqual(box._active, 0)

    def test_the_free_space_of_a_volume_that_cannot_be_read_is_a_503_too(self):
        with mock.patch.object(inbox.shutil, "disk_usage", side_effect=OSError(5, "I/O error")), mock.patch.object(sys, "stderr", io.StringIO()), self.assertRaises(InboxError) as caught:
            self.put("a.mkv", inbox_=self.box)
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.box._active, 0)


class TestCheckOverlaps(FolderCase):
    """HUB_INBOX qui recoupe HUB_DATA ou une bibliothèque : `check` le dit au démarrage (ATTENTION, dans `announce`) ; le défaut HUB_DATA/inbox ne dit rien."""

    def setUp(self):
        super().setUp()
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, True)
        self.data, self.films = self.base / "data", self.base / "srv" / "Films"
        self.films.mkdir(parents=True)

    def check(self, box, libraries=None, mounted=True):
        """`mounted` : ce que `_is_mount` répond (vrai : un volume monté ; faux : un simple dossier du conteneur ; None : la plate-forme ne sait pas le dire), pour que les essais ne dépendent pas du système où ils tournent."""
        libs = [types.SimpleNamespace(label="Films", path=self.films)] if libraries is None else libraries
        cfg = types.SimpleNamespace(inbox=box, data=self.data, libraries=libs, inbox_keep_free=0, inbox_max_uploads=2)
        with mock.patch.object(inbox, "_is_mount", return_value=mounted):
            return Inbox(cfg, self.db).check()

    def test_the_default_inbox_under_the_data_folder_says_nothing(self):
        self.assertIsNone(self.check(self.data / "inbox"))

    def test_an_inbox_next_to_the_data_and_the_libraries_says_nothing(self):
        self.assertIsNone(self.check(self.base / "HubReception"))
        self.assertIsNone(self.check(self.base / "srv" / "Films2"))                    # un dossier voisin dont le nom commence pareil n'est pas un enfant

    def test_an_inbox_that_is_the_data_folder_warns_that_img_and_posters_would_be_account_folders(self):
        message = self.check(self.data)
        self.assertIn("HUB_DATA", message)
        self.assertIn("img", message)
        self.assertIn("posters", message)
        self.assertIn("HUB_INBOX", message)

    def test_an_inbox_that_contains_the_data_folder_warns_too(self):
        self.assertIn("HUB_DATA", self.check(self.base))

    def test_an_inbox_that_is_a_library_contains_one_or_sits_inside_one_warns_with_its_name(self):
        for box in (self.films, self.base / "srv", self.films / "Recus"):
            message = self.check(box)
            self.assertIn("Films", message, box)
            self.assertIn("HUB_INBOX", message, box)

    def test_the_spelling_of_the_path_does_not_hide_the_overlap(self):
        self.assertIn("Films", self.check(self.films / ".." / "Films"))
        self.assertIn("HUB_DATA", self.check(self.data / "inbox" / ".."))

    def test_an_inbox_that_overlaps_a_library_writes_nothing_neither_a_folder_nor_the_probe_file(self):
        """Invariant : le hub n'écrit jamais dans la médiathèque. Le recoupement se détecte AVANT le `mkdir` et le fichier d'essai."""
        before = sorted(p.name for p in self.films.rglob("*"))
        for box in (self.films, self.films / "Recus", self.films / "a" / "b", self.base / "srv"):
            with mock.patch.object(Path, "mkdir") as made, mock.patch.object(inbox.tempfile, "TemporaryFile") as probed:
                message = self.check(box)
            self.assertIn("Films", message, box)
            made.assert_not_called()
            probed.assert_not_called()
        self.assertEqual(sorted(p.name for p in self.films.rglob("*")), before)
        self.assertFalse((self.films / "Recus").exists())

    def test_an_inbox_inside_a_library_that_does_not_exist_yet_is_not_created(self):
        self.assertIn("Films", self.check(self.films / "Recus"))
        self.assertEqual(list(self.films.iterdir()), [])

    def test_an_unusable_inbox_that_overlaps_nothing_is_still_said(self):
        (self.base / "fichier").write_bytes(b"x")
        self.assertIn("n'est pas utilisable", self.check(self.base / "fichier" / "boite"))

    def test_an_explicit_inbox_outside_the_data_folder_that_is_not_a_mount_point_warns_that_uploads_would_be_lost(self):
        message = self.check(self.base / "HubReception", mounted=False)
        self.assertIn("HUB_INBOX", message)
        self.assertIn("point de montage", message)
        self.assertIn("redéploiement", message)
        self.assertTrue((self.base / "HubReception").is_dir())                         # un avertissement, pas un blocage : la boîte reste utilisable

    def test_a_mount_point_or_a_platform_that_cannot_tell_says_nothing(self):
        self.assertIsNone(self.check(self.base / "HubReception", mounted=True))
        self.assertIsNone(self.check(self.base / "HubReception", mounted=None))

    def test_the_default_inbox_under_the_data_folder_is_not_expected_to_be_a_mount_point(self):
        self.assertIsNone(self.check(self.data / "inbox", mounted=False))
        self.assertIsNone(self.check(self.data / "autre" / "boite", mounted=False))

    def test_a_config_without_data_does_not_ask_whether_the_inbox_is_a_mount_point(self):
        with mock.patch.object(inbox, "_is_mount", return_value=False):
            self.assertIsNone(Inbox(types.SimpleNamespace(inbox=self.base / "HubReception", inbox_keep_free=0, inbox_max_uploads=2), self.db).check())

    def test_the_not_mounted_warning_and_an_unusable_inbox_are_both_said(self):
        (self.base / "fichier").write_bytes(b"x")
        message = self.check(self.base / "fichier" / "boite", mounted=False)
        self.assertIn("n'est pas utilisable", message)
        self.assertIn("point de montage", message)

    def test_is_mount_asks_the_system_but_gives_no_answer_on_windows_or_when_the_system_fails(self):
        ismount = mock.Mock(return_value=True)
        posix = types.SimpleNamespace(name="posix", path=types.SimpleNamespace(ismount=ismount))
        with mock.patch.object(inbox, "os", posix):
            self.assertIs(inbox._is_mount(self.base), True)
            ismount.return_value = False
            self.assertIs(inbox._is_mount(self.base), False)
            ismount.side_effect = OSError(5, "I/O error")
            self.assertIsNone(inbox._is_mount(self.base))
        ismount.reset_mock(side_effect=True)
        windows = types.SimpleNamespace(name="nt", path=types.SimpleNamespace(ismount=ismount))
        with mock.patch.object(inbox, "os", windows):
            self.assertIsNone(inbox._is_mount(self.base))
        ismount.assert_not_called()

    def test_a_config_without_data_or_libraries_is_checked_as_before(self):
        self.assertIsNone(Inbox(types.SimpleNamespace(inbox=self.base / "HubReception", inbox_keep_free=0, inbox_max_uploads=2), self.db).check())


class TestSystemErrorsWhileStoring(FolderCase):
    """Une erreur système pendant l'écriture (droits, volume en lecture seule…) est un 503 en français sans chemin ; le chemin va au journal ; rien ne reste."""

    def refused(self, patched, error):
        err = io.StringIO()
        with patched, mock.patch.object(sys, "stderr", err), self.assertRaises(InboxError) as caught:
            self.put("film.mkv", account="alice")
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn(str(self.tmp), caught.exception.message)
        self.assertNotIn("Errno", caught.exception.message)
        self.assertIn("film.mkv", err.getvalue())                                  # le journal dit où
        self.assertEqual(self.inbox._active, 0)
        self.assertEqual(sorted(p.name for p in (self.tmp / "inbox" / "alice").iterdir()), [])        # ni `.part` ni fichier

    def test_a_part_file_that_cannot_be_created_is_a_503_without_the_path(self):
        error = PermissionError(13, "Permission denied", str(self.tmp / "inbox" / "alice" / "film.mkv.part"))
        self.refused(mock.patch("hub.inbox.open", side_effect=error, create=True), error)

    def test_a_rename_that_is_refused_is_a_503_and_the_part_file_is_removed(self):
        error = PermissionError(13, "Permission denied", str(self.tmp / "inbox" / "alice" / "film.mkv.part"))
        self.refused(mock.patch.object(inbox.os, "rename", side_effect=error), error)

    def test_a_part_file_that_already_exists_still_moves_on_to_the_next_name(self):
        real = open
        calls = []

        def first_one_exists(path, mode="r", *a, **kw):
            calls.append(Path(path).name)
            if len(calls) == 1:
                raise FileExistsError(17, "File exists", str(path))
            return real(path, mode, *a, **kw)

        with mock.patch("hub.inbox.open", first_one_exists, create=True):
            self.assertEqual(self.put("film.mkv")["name"], "film (2).mkv")
        self.assertEqual(calls, ["film.mkv.part", "film (2).mkv.part"])
        self.assertEqual(self.files(), ["film (2).mkv"])


class TestConfinement(FolderCase):
    """Une fois résolu, un chemin reste sous la boîte : en plus des contrôles de nom, des liens et des jonctions."""

    def setUp(self):
        super().setUp()
        self.put("Film.mkv", b"abc")
        self.secret = self.tmp / "secret.mkv"
        self.secret.write_bytes(b"secret")

    def refused(self, folder, name):
        with self.assertRaises(InboxError, msg=(folder, name)) as caught:
            self.inbox.path_of(folder, name)
        self.assertEqual(caught.exception.status, 404, (folder, name))

    def test_every_spelling_of_a_traversal_is_a_404(self):
        for folder in ("..", ".", "%2e%2e", "%2e%2e%2f", "..%2f", "a/b", "alice/..", "alice\\..", "..\\..", "\x00", "alice\x00", "", "/", "\\", "alice/", "C:", "alice:stream", "../inbox/alice", None, 5):
            self.refused(folder, "Film.mkv")
        for name in ("..", ".", "../secret.mkv", "..\\secret.mkv", "%2e%2e%2fsecret.mkv", "..%2fsecret.mkv", "a/../Film.mkv", "Film.mkv\x00", "Film.mkv/", "/Film.mkv", "", None, 5):
            self.refused("alice", name)

    def test_a_hidden_a_reserved_and_an_unknown_folder_are_a_404(self):
        (self.tmp / "inbox" / ".caché").mkdir()
        (self.tmp / "inbox" / ".caché" / "x.mkv").write_bytes(b"x")
        for folder in (".caché", ".", "CON", "nul", "COM1", "con.txt", "inconnu", "alice "):
            self.refused(folder, "x.mkv")
            self.refused(folder, "Film.mkv")
        self.assertEqual([f["name"] for f in self.inbox.listing()], ["Film.mkv"])      # le dossier caché n'est pas listé non plus

    def test_a_path_that_resolves_outside_the_inbox_is_refused_whatever_the_names_say(self):
        """Un lien ou un point de montage que les contrôles de nom ne voient pas : la résolution le montre."""
        real = Path.resolve
        folder = self.tmp / "inbox" / "alice"

        def folder_leads_out(path, *a, **kw):
            return self.tmp / "dehors" if path == folder else real(path, *a, **kw)

        def file_leads_out(path, *a, **kw):
            return self.secret if path == folder / "Film.mkv" else real(path, *a, **kw)

        with mock.patch.object(Path, "resolve", folder_leads_out):
            self.refused("alice", "Film.mkv")
            self.assertEqual(self.inbox.listing("alice"), [])
            self.assertEqual(self.inbox.listing(), [])
            with self.assertRaises(InboxError):
                self.inbox.remove("alice", "Film.mkv")
            with mock.patch.object(sys, "stderr", io.StringIO()), self.assertRaises(InboxError):
                self.put("b.mkv")
        with mock.patch.object(Path, "resolve", file_leads_out):
            self.refused("alice", "Film.mkv")
            with self.assertRaises(InboxError):
                self.inbox.remove("alice", "Film.mkv")
        self.assertEqual(self.secret.read_bytes(), b"secret")
        self.assertEqual(self.inbox.path_of("alice", "Film.mkv").read_bytes(), b"abc")         # sans cela, tout marche

    def test_a_resolution_that_fails_refuses_instead_of_trusting_the_name(self):
        with mock.patch.object(Path, "resolve", side_effect=OSError(5, "I/O error")):
            self.refused("alice", "Film.mkv")

    def test_a_symbolic_link_to_a_folder_or_a_file_outside_is_not_followed(self):
        outside = self.tmp / "dehors"
        outside.mkdir()
        (outside / "x.mkv").write_bytes(b"x")
        try:
            os.symlink(outside, self.tmp / "inbox" / "mallory", target_is_directory=True)
            os.symlink(self.secret, self.tmp / "inbox" / "alice" / "lien.mkv")
        except (OSError, NotImplementedError):
            self.skipTest("pas de liens symboliques ici (droit refusé sous Windows)")
        self.refused("mallory", "x.mkv")
        self.refused("alice", "lien.mkv")
        self.assertEqual([f["name"] for f in self.inbox.listing()], ["Film.mkv"])
        add_user(self.db, "mallory", "mallory", inbox_dir="mallory")
        with mock.patch.object(sys, "stderr", io.StringIO()), self.assertRaises(InboxError):
            self.put("y.mkv", account="mallory")
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["x.mkv"])

    def test_a_windows_junction_to_a_folder_outside_is_not_followed(self):
        if sys.platform != "win32":
            self.skipTest("les jonctions n'existent que sous Windows")
        import _winapi
        outside = self.tmp / "dehors"
        outside.mkdir()
        (outside / "x.mkv").write_bytes(b"x")
        _winapi.CreateJunction(str(outside), str(self.tmp / "inbox" / "mallory"))
        self.refused("mallory", "x.mkv")
        self.assertEqual([f["account"] for f in self.inbox.listing()], ["alice"])
        add_user(self.db, "mallory", "mallory", inbox_dir="mallory")
        with mock.patch.object(sys, "stderr", io.StringIO()), self.assertRaises(InboxError):
            self.put("y.mkv", account="mallory")
        with self.assertRaises(InboxError):
            self.inbox.remove("mallory", "x.mkv")
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["x.mkv"])

    def test_a_junction_inside_a_folder_is_not_a_file(self):
        if sys.platform != "win32":
            self.skipTest("les jonctions n'existent que sous Windows")
        import _winapi
        _winapi.CreateJunction(str(self.tmp / "inbox" / "alice"), str(self.tmp / "inbox" / "alice" / "dossier"))
        self.refused("alice", "dossier")
        self.assertEqual([f["name"] for f in self.inbox.listing()], ["Film.mkv"])


if __name__ == "__main__":
    unittest.main()
