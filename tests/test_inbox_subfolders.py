"""La boîte de réception avec des sous-dossiers, sans réseau : nettoyage du chemin (`clean_dir`), création au moment où un fichier arrive, fusion à la casse près, plafond, liens et jonctions,
parcours récursifs (`listing`, `used`, `sweep`), lecture et suppression d'un fichier d'un sous-dossier."""

from __future__ import annotations

import io
import os
import shutil
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from hub import inbox
from hub.inbox import Inbox, InboxError
from tests.test_inbox import TempCase, add_user, make


def tree(root):
    """Tout ce qui est sous `root`, en chemins relatifs : dossiers terminés par « / »."""
    return sorted(p.relative_to(root).as_posix() + ("/" if p.is_dir() else "") for p in Path(root).rglob("*"))


class SubCase(TempCase):
    def send(self, name="Film.mkv", sub=None, data=b"abc", account="alice", box=None):
        return (box or self.inbox).receive(account, name, io.BytesIO(data), len(data), sub)

    @property
    def alice(self):
        return self.tmp / "inbox" / "alice"

    def status(self, call, *args, **kw):
        with self.assertRaises(InboxError) as caught:
            call(*args, **kw)
        return caught.exception.status

    def link(self, target, where):
        """Un lien symbolique, ou à défaut (Windows sans droit) une jonction ; saute le test si rien n'est possible."""
        try:
            os.symlink(target, where, target_is_directory=True)
        except (OSError, NotImplementedError):
            if sys.platform != "win32":
                self.skipTest("pas de liens symboliques ici")
            import _winapi
            _winapi.CreateJunction(str(target), str(where))


class TestCleanDir(unittest.TestCase):
    def test_nothing_or_an_empty_text_is_the_account_folder_itself(self):
        self.assertEqual(inbox.clean_dir(None), ())
        self.assertEqual(inbox.clean_dir(""), ())

    def test_one_to_four_levels_are_kept_as_segments(self):
        self.assertEqual(inbox.clean_dir("a"), ("a",))
        self.assertEqual(inbox.clean_dir("a/b"), ("a", "b"))
        self.assertEqual(inbox.clean_dir("a/b/c"), ("a", "b", "c"))
        self.assertEqual(inbox.clean_dir("a/b/c/d"), ("a", "b", "c", "d"))

    def test_a_fifth_level_is_a_400(self):
        with self.assertRaises(InboxError) as caught:
            inbox.clean_dir("a/b/c/d/e")
        self.assertEqual((caught.exception.status, caught.exception.message), (400, "dossiers trop profonds : 4 niveaux au plus"))

    def test_an_empty_segment_a_dot_or_two_dots_are_refused_never_skipped(self):
        for raw in ("a//b", "/a", "a/", "/", "//", ".", "..", "a/./b", "a/../b", "../a", " ", "...", "a/ /b", ". .", " . ", "a/..."):
            with self.assertRaises(InboxError, msg=raw) as caught:
                inbox.clean_dir(raw)
            self.assertEqual(caught.exception.status, 400, raw)
            self.assertIn("segment", caught.exception.message, raw)

    def test_a_backslash_is_refused_not_turned_into_a_name(self):
        for raw in ("a\\b", "..\\..\\x", "\\", "a/b\\c"):
            with self.assertRaises(InboxError, msg=raw) as caught:
                inbox.clean_dir(raw)
            self.assertEqual(caught.exception.status, 400, raw)
            self.assertIn("\\", caught.exception.message)

    def test_something_that_is_not_a_text_is_a_400(self):
        for raw in (5, b"a", ["a"], True):
            with self.assertRaises(InboxError, msg=raw) as caught:
                inbox.clean_dir(raw)
            self.assertEqual(caught.exception.status, 400)

    def test_every_segment_gets_the_rules_of_a_folder_name(self):
        self.assertEqual(inbox.clean_dir("a:b/c?d"), ("a_b", "c_d"))                  # caractères que Windows refuse
        self.assertEqual(inbox.clean_dir("a\x00b/c\nd"), ("a_b", "c_d"))              # octet nul, contrôle
        self.assertEqual(inbox.clean_dir("CON/nul.fr"), ("_CON", "_nul.fr"))          # noms réservés de Windows
        self.assertEqual(inbox.clean_dir(".caché/ x /y. "), ("caché", "x", "y"))      # jamais caché, ni point ni espace au bout
        self.assertEqual(inbox.clean_dir("Café/Léa"), ("Café", "Léa"))          # NFC, accents gardés

    def test_an_encoded_traversal_is_only_a_name_the_text_is_not_decoded_here(self):
        self.assertEqual(inbox.clean_dir("%2e%2e/%2f"), ("%2e%2e", "%2f"))

    def test_a_long_segment_is_cut_in_bytes_without_breaking_a_character(self):
        for raw in ("é" * 400, "日本語" * 100, "😀" * 100):
            (segment,) = inbox.clean_dir(raw)
            self.assertLessEqual(len(segment.encode("utf-8")), inbox.FOLDER_BYTES)
            self.assertGreater(len(segment), 10)

    def test_a_huge_path_is_refused_before_it_is_cut_into_segments(self):
        with self.assertRaises(InboxError):
            inbox.clean_dir("a/" * 100000)

    def test_what_it_produces_is_always_a_valid_folder_name(self):
        for raw in ("a/b", "CON", "x:y/z*", "é" * 400, "a\x00b", "😀/😀", ".x/..y"):
            for segment in inbox.clean_dir(raw):
                self.assertTrue(inbox.is_folder_name(segment), repr(segment))


class TestReceiveInSubfolders(SubCase):
    def test_a_file_goes_into_a_subfolder_created_for_it_at_one_to_four_levels(self):
        for sub in ("a", "b/c", "d/e/f", "g/h/i/j"):
            reply = self.send("x.mkv", sub, b"1234")
            self.assertEqual(reply, {"name": "x.mkv", "size": 4, "account": "alice", "dir": sub})
            self.assertEqual((self.alice / sub / "x.mkv").read_bytes(), b"1234")
        self.assertEqual(self.send("y.mkv")["dir"], "")
        self.assertTrue((self.alice / "y.mkv").is_file())

    def test_a_fifth_level_is_refused_and_nothing_is_created(self):
        self.assertEqual(self.status(self.send, "x.mkv", "a/b/c/d/e"), 400)
        self.assertFalse((self.tmp / "inbox").exists())

    def test_a_bad_path_is_refused_before_any_folder_is_made(self):
        for sub in ("a//b", ".", "..", "a/../b", "a\\b", " ", "/a", "a/", 5):
            self.assertEqual(self.status(self.send, "x.mkv", sub), 400, sub)
        self.assertFalse((self.tmp / "inbox").exists())

    def test_a_path_cannot_climb_out_of_the_account_folder_whatever_the_spelling(self):
        for sub in ("..", "../bob", "a/../../bob", "%2e%2e", "..%2fbob", "....//bob", "a/b/../../../bob"):
            try:
                self.send("x.mkv", sub)
            except InboxError:
                pass
        everything = [p for p in (self.tmp / "inbox").rglob("*") if p.is_file()]
        self.assertTrue(everything)                                      # « %2e%2e » reste un nom de dossier ordinaire
        for p in everything:
            self.assertEqual(p.relative_to(self.tmp / "inbox").parts[0], "alice", p)
        self.assertFalse((self.tmp / "inbox" / "bob").exists())

    def test_the_same_folder_spelt_with_another_case_or_unicode_form_is_the_same_folder(self):
        self.send("a.mkv", "Dossier/Sous")
        self.send("b.mkv", "dossier/SOUS")
        self.send("c.mkv", "DOSSIER/sous")
        self.assertEqual(tree(self.alice), ["Dossier/", "Dossier/Sous/", "Dossier/Sous/a.mkv", "Dossier/Sous/b.mkv", "Dossier/Sous/c.mkv"])
        self.send("d.mkv", "Café")
        self.send("e.mkv", "café")
        self.assertEqual(sorted(p.name for p in self.alice.iterdir() if p.is_dir()), ["Café", "Dossier"])

    def test_the_same_folder_sent_twice_is_merged_and_same_named_files_get_a_number(self):
        self.send("Film.mkv", "Vacances/2025", b"un")
        self.send("Film.mkv", "Vacances/2025", b"deux")
        self.send("Autre.mkv", "Vacances/2025", b"trois")
        self.assertEqual(tree(self.alice), ["Vacances/", "Vacances/2025/", "Vacances/2025/Autre.mkv", "Vacances/2025/Film (2).mkv", "Vacances/2025/Film.mkv"])
        self.assertEqual((self.alice / "Vacances" / "2025" / "Film (2).mkv").read_bytes(), b"deux")

    def test_a_subfolder_is_never_numbered(self):
        for _ in range(3):
            self.send("a.mkv", "x")
        self.assertEqual(sorted(p.name for p in self.alice.iterdir()), ["x"])

    def test_two_names_that_become_the_same_once_cleaned_are_the_same_folder(self):
        self.send("a.mkv", "a:b")
        self.send("b.mkv", "a?b")
        self.send("c.mkv", "A*B")
        self.assertEqual(sorted(p.name for p in self.alice.iterdir()), ["a_b"])
        self.assertEqual(len(list((self.alice / "a_b").iterdir())), 3)

    def test_a_refused_file_never_creates_a_folder(self):
        for name in ("x.exe", "x", "script.sh", "", None, ".."):
            with self.assertRaises(InboxError, msg=name):
                self.send(name, "a/b/c")
        self.assertFalse((self.tmp / "inbox").exists())

    def test_a_refusal_of_the_door_never_creates_a_folder(self):
        self.send("premier.mkv")                                          # le dossier du compte existe déjà
        before = tree(self.tmp / "inbox")
        full = make(self.tmp, self.db, inbox_keep_free=1 << 60)
        self.assertEqual(self.status(self.send, "x.mkv", "a/b", box=full), 507)
        busy = make(self.tmp, self.db, inbox_max_uploads=0)
        self.assertEqual(self.status(self.send, "x.mkv", "a/b", box=busy), 429)
        self.assertEqual(self.status(self.send, "x.mkv", "a/b", account="inconnu"), 400)
        self.assertEqual(tree(self.tmp / "inbox"), before)

    def test_a_broken_upload_takes_back_the_folders_it_made_and_only_those(self):
        self.send("premier.mkv", "existe")
        with self.assertRaises(InboxError):
            self.inbox.receive("alice", "x.mkv", io.BytesIO(b"ab"), 10, "existe/neuf/profond")
        self.assertEqual(tree(self.alice), ["existe/", "existe/premier.mkv"])

    def test_a_full_disk_while_writing_takes_back_the_folders_too(self):
        class Full:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def write(self, data):
                raise OSError(28, "No space left on device")

        with mock.patch.object(Inbox, "_open_part", return_value=("x.mkv", Full())), self.assertRaises(InboxError) as caught:
            self.inbox.receive("alice", "x.mkv", io.BytesIO(b"abc"), 3, "a/b")
        self.assertEqual(caught.exception.status, 507)
        self.assertEqual(tree(self.alice), [])

    def test_the_account_folder_itself_is_never_taken_back(self):
        with self.assertRaises(InboxError):
            self.inbox.receive("alice", "x.mkv", io.BytesIO(b"ab"), 10, "a")
        self.assertTrue(self.alice.is_dir())

    def test_a_subfolder_removed_by_hand_during_the_upload_is_a_clean_failure(self):
        real = Inbox._publish

        def hurt(box, folder, final):
            shutil.rmtree(folder)
            return real(box, folder, final)

        with mock.patch.object(Inbox, "_publish", hurt), mock.patch.object(sys, "stderr", io.StringIO()), self.assertRaises(InboxError) as caught:
            self.send("x.mkv", "a/b")
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn(str(self.tmp), caught.exception.message)
        self.assertEqual(self.inbox.listing(), [])
        self.assertEqual(self.send("y.mkv", "a/b")["dir"], "a/b")          # le suivant recrée ce qui manque

    def test_a_subfolder_removed_by_hand_between_two_uploads_is_made_again(self):
        self.send("x.mkv", "a/b")
        shutil.rmtree(self.alice / "a")
        self.send("y.mkv", "a/b")
        self.assertEqual(tree(self.alice), ["a/", "a/b/", "a/b/y.mkv"])

    def test_a_file_in_the_way_of_a_subfolder_is_a_409_not_an_overwrite(self):
        self.send("Vacances.mkv")
        self.assertEqual(self.status(self.send, "x.mkv", "Vacances.mkv"), 409)
        self.assertEqual(self.status(self.send, "x.mkv", "vacances.MKV/b"), 409)
        self.assertEqual((self.alice / "Vacances.mkv").read_bytes(), b"abc")

    def test_a_file_named_like_a_folder_of_the_same_level_is_numbered(self):
        self.send("Film.mkv", "Film.mkv")
        self.assertEqual(self.send("Film.mkv")["name"], "Film (2).mkv")
        self.assertEqual(tree(self.alice), ["Film (2).mkv", "Film.mkv/", "Film.mkv/Film.mkv"])


class TestLimits(unittest.TestCase):
    def test_the_depth_is_four_and_the_ceiling_a_thousand_folders(self):
        self.assertEqual((inbox.DEPTH, inbox.MAX_SUBFOLDERS), (4, 1000))


class TestSubfolderCeiling(SubCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(inbox, "MAX_SUBFOLDERS", 3)
        patch.start()
        self.addCleanup(patch.stop)

    def test_beyond_the_ceiling_a_new_folder_is_refused_in_french_and_nothing_is_created(self):
        for sub in ("a", "b", "c"):
            self.send("x.mkv", sub)
        with self.assertRaises(InboxError) as caught:
            self.send("x.mkv", "d")
        self.assertEqual((caught.exception.status, caught.exception.message), (409, "trop de sous-dossiers dans ton dossier (3 au plus)"))
        self.assertEqual(sorted(p.name for p in self.alice.iterdir()), ["a", "b", "c"])

    def test_a_file_for_an_existing_folder_is_not_concerned(self):
        for sub in ("a", "b", "c"):
            self.send("x.mkv", sub)
        self.assertEqual(self.send("y.mkv", "B")["dir"], "b")
        self.assertEqual(self.send("z.mkv")["dir"], "")
        self.assertEqual(sorted(p.name for p in (self.alice / "b").iterdir()), ["x.mkv", "y.mkv"])

    def test_the_levels_to_create_all_count_and_none_is_made_when_they_do_not_fit(self):
        self.send("x.mkv", "a")
        self.assertEqual(self.status(self.send, "y.mkv", "a/b/c/d"), 409)           # 3 de plus : 4 au total
        self.assertEqual(tree(self.alice), ["a/", "a/x.mkv"])
        self.assertEqual(self.send("y.mkv", "a/b/c")["dir"], "a/b/c")               # 2 de plus : 3 au total

    def test_the_folders_of_the_other_accounts_do_not_count(self):
        for sub in ("a", "b", "c"):
            self.send("x.mkv", sub, account="bob")
        self.assertEqual(self.send("x.mkv", "a")["dir"], "a")

    def test_the_ceiling_counts_folders_of_every_depth(self):
        self.send("x.mkv", "a/b/c")
        self.assertEqual(self.status(self.send, "x.mkv", "d"), 409)


class TestLinksInTheWay(SubCase):
    def setUp(self):
        super().setUp()
        self.send("secret.mkv", account="bob")
        self.send("x.mkv")
        self.bob = self.tmp / "inbox" / "bob"

    def test_a_link_or_a_junction_standing_for_a_subfolder_is_not_written_through(self):
        self.link(self.bob, self.alice / "sub")
        with mock.patch.object(sys, "stderr", io.StringIO()), self.assertRaises(InboxError) as caught:
            self.send("y.mkv", "sub")
        self.assertEqual(caught.exception.status, 409)
        with mock.patch.object(sys, "stderr", io.StringIO()), self.assertRaises(InboxError):
            self.send("y.mkv", "SUB/deeper")
        self.assertEqual(sorted(p.name for p in self.bob.iterdir()), ["secret.mkv"])

    def test_the_listing_does_not_enter_a_link_or_a_junction(self):
        self.link(self.bob, self.alice / "sub")
        self.assertEqual([(f["account"], f["dir"], f["name"]) for f in self.inbox.listing()], [("alice", "", "x.mkv"), ("bob", "", "secret.mkv")])
        self.assertEqual(self.inbox.used("alice"), 3)

    def test_a_file_behind_a_link_is_neither_read_nor_removed(self):
        self.link(self.bob, self.alice / "sub")
        self.assertEqual(self.status(self.inbox.path_of, "alice", "secret.mkv", "sub"), 404)
        self.assertEqual(self.status(self.inbox.remove, "alice", "secret.mkv", "sub"), 404)
        self.assertEqual((self.bob / "secret.mkv").read_bytes(), b"abc")

    def test_the_sweep_does_not_enter_a_link_or_a_junction(self):
        (self.bob / "vieux.mkv.part").write_bytes(b"x")
        self.link(self.bob, self.alice / "sub")
        self.assertEqual(self.inbox.sweep(now=time.time() + 3 * inbox.STALE), 1)      # celui de bob, une fois, par son propre dossier
        self.assertFalse((self.bob / "vieux.mkv.part").exists())

    def test_when_the_link_is_not_recognised_the_resolved_path_must_still_stay_under_the_account_folder(self):
        """Un point de montage ou une redirection que `is_symlink` et `is_junction` ne voient pas : le chemin résolu mène chez `bob`, sous la boîte mais hors du dossier du compte."""
        self.link(self.bob, self.alice / "sub")
        blind = staticmethod(lambda path: path.is_dir())
        with mock.patch.object(Inbox, "_is_real_folder", blind), mock.patch.object(sys, "stderr", io.StringIO()):
            with self.assertRaises(InboxError):
                self.send("y.mkv", "sub")
            self.assertEqual(self.status(self.inbox.path_of, "alice", "secret.mkv", "sub"), 404)
            self.assertEqual(self.status(self.inbox.remove, "alice", "secret.mkv", "sub"), 404)
            self.assertEqual([f["dir"] for f in self.inbox.listing("alice")], [""])
            self.assertEqual(self.inbox.sweep(now=time.time() + 3 * inbox.STALE), 0)
        self.assertEqual(sorted(p.name for p in self.bob.iterdir()), ["secret.mkv"])

    def test_a_directory_made_by_hand_with_a_doubtful_name_is_not_entered(self):
        for name in (".caché", "CON", "con.txt"):
            try:
                (self.alice / name).mkdir()
                (self.alice / name / "z.mkv").write_bytes(b"z")
            except OSError:
                pass
        self.assertEqual([(f["dir"], f["name"]) for f in self.inbox.listing("alice")], [("", "x.mkv")])


class TestWalks(SubCase):
    def test_the_listing_has_a_dir_field_and_goes_into_every_subfolder(self):
        self.send("a.mkv", data=b"1")
        self.send("b.mkv", "un", data=b"22")
        self.send("c.mkv", "un/deux/trois", data=b"333")
        self.send("d.mkv", "autre", account="bob", data=b"4444")
        found = {(f["account"], f["dir"], f["name"], f["size"]) for f in self.inbox.listing()}
        self.assertEqual(found, {("alice", "", "a.mkv", 1), ("alice", "un", "b.mkv", 2), ("alice", "un/deux/trois", "c.mkv", 3), ("bob", "autre", "d.mkv", 4)})
        for f in self.inbox.listing():
            self.assertEqual(set(f), {"account", "dir", "name", "size", "date"})
        self.assertEqual({f["dir"] for f in self.inbox.listing("alice")}, {"", "un", "un/deux/trois"})

    def test_the_most_recent_comes_first_and_ties_are_ordered_by_account_dir_and_name(self):
        for sub, name, when in (("b", "z.mkv", 100), ("a", "y.mkv", 100), ("", "x.mkv", 50), ("a", "a.mkv", 100)):
            self.send(name, sub)
            os.utime((self.alice / sub / name) if sub else (self.alice / name), (when, when))
        self.assertEqual([(f["dir"], f["name"]) for f in self.inbox.listing()], [("a", "a.mkv"), ("a", "y.mkv"), ("b", "z.mkv"), ("", "x.mkv")])

    def test_used_adds_up_the_files_of_every_level_of_the_account_only(self):
        self.send("a.mkv", data=b"1")
        self.send("b.mkv", "x/y", data=b"22")
        self.send("c.mkv", "x", account="bob", data=b"333")
        (self.alice / "x" / "y" / "fin.mkv.part").write_bytes(b"9999")                # un envoi en cours ne compte pas
        self.assertEqual(self.inbox.used("alice"), 3)
        self.assertEqual(self.inbox.used("bob"), 3)

    def test_the_walk_stops_at_four_levels(self):
        self.send("racine.mkv", data=b"x")
        deep = self.alice / "a" / "b" / "c" / "d" / "e"
        deep.mkdir(parents=True)
        (deep / "trop.mkv").write_bytes(b"x")
        (deep.parent / "limite.mkv").write_bytes(b"x")
        self.assertEqual(sorted((f["dir"], f["name"]) for f in self.inbox.listing()), [("", "racine.mkv"), ("a/b/c/d", "limite.mkv")])
        self.assertEqual(self.inbox.used("alice"), 2)
        self.assertEqual(self.status(self.inbox.path_of, "alice", "trop.mkv", "a/b/c/d/e"), 404)

    def test_a_part_a_hidden_file_and_a_folder_are_not_listed(self):
        self.send("a.mkv", "x")
        (self.alice / "x" / "b.mkv.part").write_bytes(b"x")
        (self.alice / "x" / ".cache.mkv").write_bytes(b"x")
        self.assertEqual([f["name"] for f in self.inbox.listing()], ["a.mkv"])

    def test_the_sweep_removes_an_abandoned_part_in_a_subfolder_and_the_folder_it_empties(self):
        self.send("a.mkv", "x")
        (self.alice / "x" / "y").mkdir()
        (self.alice / "x" / "y" / "vieux.mkv.part").write_bytes(b"x")
        (self.alice / "x" / "vieux2.mkv.part").write_bytes(b"x")
        (self.alice / "x" / "recent.mkv.part").write_bytes(b"x")
        old = time.time() - 2 * inbox.STALE
        for p in ((self.alice / "x" / "y" / "vieux.mkv.part"), (self.alice / "x" / "vieux2.mkv.part")):
            os.utime(p, (old, old))
        self.assertEqual(self.inbox.sweep(), 2)
        self.assertEqual(tree(self.alice), ["x/", "x/a.mkv", "x/recent.mkv.part"])

    def test_the_sweep_never_removes_the_account_folder(self):
        (self.alice).mkdir(parents=True)
        (self.alice / "vieux.mkv.part").write_bytes(b"x")
        self.assertEqual(self.inbox.sweep(now=time.time() + 3 * inbox.STALE), 1)
        self.assertTrue(self.alice.is_dir())

    def test_the_sweep_leaves_a_folder_it_did_not_empty_and_a_dir_that_was_already_empty(self):
        (self.alice / "vide").mkdir(parents=True)
        self.send("a.mkv", "x")
        (self.alice / "x" / "vieux.mkv.part").write_bytes(b"x")
        self.assertEqual(self.inbox.sweep(now=time.time() + 3 * inbox.STALE), 1)
        self.assertEqual(tree(self.alice), ["vide/", "x/", "x/a.mkv"])


class TestReadAndRemove(SubCase):
    def test_a_file_of_a_subfolder_is_found_by_its_dir(self):
        self.send("a.mkv", "x/y", data=b"contenu")
        self.assertEqual(self.inbox.path_of("alice", "a.mkv", "x/y").read_bytes(), b"contenu")
        self.assertEqual(self.status(self.inbox.path_of, "alice", "a.mkv"), 404)
        self.assertEqual(self.status(self.inbox.path_of, "alice", "a.mkv", "x"), 404)
        self.assertEqual(self.status(self.inbox.path_of, "bob", "a.mkv", "x/y"), 404)

    def test_every_spelling_of_a_traversal_in_dir_is_a_404(self):
        self.send("a.mkv", "x")
        self.send("s.mkv", account="bob")
        for sub in ("..", ".", "../bob", "x/..", "x/../x", "%2e%2e", "..%2f", "x\\..", "\\", "/", "/x", "x/", "x//y", "\x00", "x\x00", "C:", "x:y", ".caché", "CON", " ", "a/b/c/d/e", 5, ["x"]):
            self.assertEqual(self.status(self.inbox.path_of, "alice", "a.mkv", sub), 404, sub)
            self.assertEqual(self.status(self.inbox.path_of, "alice", "s.mkv", sub), 404, sub)
            self.assertEqual(self.status(self.inbox.remove, "alice", "a.mkv", sub), 404, sub)
        self.assertEqual(self.inbox.path_of("alice", "a.mkv", "x").name, "a.mkv")        # sans cela, tout marche
        self.assertEqual(self.files("bob"), ["s.mkv"])

    def test_an_empty_dir_is_the_account_folder(self):
        self.send("a.mkv")
        self.assertEqual(self.inbox.path_of("alice", "a.mkv", "").name, "a.mkv")
        self.assertEqual(self.inbox.path_of("alice", "a.mkv", None).name, "a.mkv")

    def test_a_resolution_that_fails_refuses(self):
        self.send("a.mkv", "x")
        with mock.patch.object(Path, "resolve", side_effect=OSError(5, "I/O error")):
            self.assertEqual(self.status(self.inbox.path_of, "alice", "a.mkv", "x"), 404)

    def test_removing_the_last_file_removes_the_folders_it_empties_and_not_the_account_folder(self):
        self.send("a.mkv", "x/y/z")
        self.inbox.remove("alice", "a.mkv", "x/y/z")
        self.assertEqual(tree(self.alice), [])
        self.assertTrue(self.alice.is_dir())

    def test_a_folder_that_still_holds_something_stays(self):
        self.send("a.mkv", "x/y")
        self.send("b.mkv", "x/y")
        self.send("c.mkv", "x")
        self.inbox.remove("alice", "a.mkv", "x/y")
        self.assertEqual(tree(self.alice), ["x/", "x/c.mkv", "x/y/", "x/y/b.mkv"])
        self.inbox.remove("alice", "b.mkv", "x/y")
        self.assertEqual(tree(self.alice), ["x/", "x/c.mkv"])                           # x/y part, x reste (c.mkv)

    def test_a_folder_with_a_part_in_it_or_a_sibling_folder_stays(self):
        self.send("a.mkv", "x/y")
        (self.alice / "x" / "y" / "b.mkv.part").write_bytes(b"x")
        (self.alice / "x" / "autre").mkdir()
        self.inbox.remove("alice", "a.mkv", "x/y")
        self.assertEqual(tree(self.alice), ["x/", "x/autre/", "x/y/", "x/y/b.mkv.part"])

    def test_removing_a_file_of_the_account_folder_keeps_the_account_folder(self):
        self.send("a.mkv")
        self.inbox.remove("alice", "a.mkv")
        self.assertTrue(self.alice.is_dir())
        self.assertEqual(self.status(self.inbox.remove, "alice", "a.mkv"), 404)

    def test_an_emptied_folder_is_made_again_by_the_next_upload(self):
        self.send("a.mkv", "x/y")
        self.inbox.remove("alice", "a.mkv", "x/y")
        self.send("b.mkv", "x/y")
        self.assertEqual(tree(self.alice), ["x/", "x/y/", "x/y/b.mkv"])


class TestUploadsAtTheSameTime(SubCase):
    def test_many_uploads_into_new_folders_at_once_all_keep_their_file_and_share_one_folder(self):
        import threading
        errors = []

        box = make(self.tmp, self.db, inbox_max_uploads=50)

        def upload(n):
            try:
                self.send(f"f{n}.mkv", "Commun/Sous" if n % 2 else "commun/sous", data=b"x" * (n + 1), box=box)
            except Exception as e:           # pragma: no cover - rapporté ci-dessous
                errors.append(e)

        threads = [threading.Thread(target=upload, args=(n,)) for n in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        folders = [p for p in self.alice.rglob("*") if p.is_dir()]
        self.assertEqual(len(folders), 2, folders)
        self.assertEqual(len([p for p in self.alice.rglob("*.mkv")]), 12)

    def test_a_failed_upload_does_not_take_back_a_folder_another_upload_is_using(self):
        self.send("premier.mkv", "x")
        self.assertEqual(self.status(self.inbox.receive, "alice", "y.mkv", io.BytesIO(b"ab"), 10, "x"), 400)
        self.assertEqual(tree(self.alice), ["x/", "x/premier.mkv"])


if __name__ == "__main__":
    unittest.main()
