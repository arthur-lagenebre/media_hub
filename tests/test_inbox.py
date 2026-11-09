"""La boîte de réception, sans réseau : nettoyage des noms, réception d'un fichier (espace libre, envois simultanés, coupures), liste, chemins et nettoyage des `.part`."""

from __future__ import annotations

import io
import os
import shutil
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

MB = 1 << 20


def make(root, **over):
    cfg = {"data": Path(root), "inbox_keep_free": 0, "inbox_max_uploads": 2, **over}
    return Inbox(types.SimpleNamespace(**cfg))


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.inbox = make(self.tmp)

    def files(self, account="alice"):
        folder = self.tmp / "inbox" / account
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []

    def put(self, name, data=b"abc", account="alice", inbox_=None):
        return (inbox_ or self.inbox).receive(account, name, io.BytesIO(data), len(data))


class TestCleanName(unittest.TestCase):
    def test_a_plain_name_is_kept_with_its_accents_and_its_case(self):
        self.assertEqual(inbox.clean_name("Amélie (2001).MKV"), "Amélie (2001).MKV")

    def test_only_the_last_part_of_a_path_is_kept_whatever_the_separator(self):
        for raw in ("../../etc/x.mkv", "..\\..\\x.mkv", "C:\\Users\\moi\\x.mkv", "/abs/olu/x.mkv", "a/b\\c/x.mkv", "../..\\x.mkv"):
            self.assertEqual(inbox.clean_name(raw), "x.mkv", raw)

    def test_control_characters_and_the_characters_windows_refuses_become_underscores(self):
        self.assertEqual(inbox.clean_name("a\x00b\nc\td\x7fe.mkv"), "a_b_c_d_e.mkv")
        self.assertEqual(inbox.clean_name('a:b?c*d"e<f>g|h.mkv'), "a_b_c_d_e_f_g_h.mkv")

    def test_the_unicode_line_and_paragraph_separators_are_replaced_too(self):
        self.assertEqual(inbox.clean_name("a\u2028b\u2029c.mkv"), "a_b_c.mkv")

    def test_no_dot_at_the_start_and_no_dot_or_space_at_the_end(self):
        self.assertEqual(inbox.clean_name(".cache.mkv"), "cache.mkv")
        self.assertEqual(inbox.clean_name("...x.mkv"), "x.mkv")
        self.assertEqual(inbox.clean_name("x.mkv. . "), "x.mkv")
        self.assertEqual(inbox.clean_name("x .mkv"), "x.mkv")

    def test_nothing_left_of_the_name_is_a_400(self):
        for raw in ("", " ", ".", "..", "/", "../", "\\", "...", None, 12, b"x.mkv"):
            with self.assertRaises(InboxError, msg=raw) as caught:
                inbox.clean_name(raw)
            self.assertEqual(caught.exception.status, 400, raw)

    def test_the_names_windows_reserves_are_prefixed(self):
        for raw in ("CON.mkv", "nul.srt", "Aux.mp3", "prn.mkv", "COM1.mkv", "lpt9.epub", "con.fr.srt"):
            self.assertEqual(inbox.clean_name(raw), "_" + raw, raw)
        for raw in ("CONSOLE.mkv", "com10.mkv", "confiance.mkv"):
            self.assertEqual(inbox.clean_name(raw), raw, raw)

    def test_a_very_long_name_is_cut_in_bytes_and_keeps_its_extension(self):
        for stem in ("é" * 400, "a" * 400, "日本語" * 100):
            name = inbox.clean_name(stem + ".mkv")
            self.assertTrue(name.endswith(".mkv"), name)
            self.assertLessEqual(len(name.encode("utf-8")), inbox.NAME_BYTES)
            self.assertGreater(len(name), 10)

    def test_the_extensions_of_a_video_a_song_a_book_a_subtitle_and_a_zip_are_accepted_in_any_case(self):
        for ext in (".mkv", ".mp4", ".avi", ".flac", ".mp3", ".opus", ".epub", ".cbz", ".pdf", ".srt", ".ass", ".vtt", ".zip", ".ZIP", ".Mkv"):
            self.assertEqual(inbox.clean_name("x" + ext), "x" + ext)

    def test_programs_scripts_pages_and_names_without_extension_are_refused_with_a_415(self):
        for raw in ("x.exe", "x.sh", "x.bat", "x.cmd", "x.ps1", "x.js", "x.py", "x.html", "x.htm", "x.svg", "x.jar", "x.apk", "x.msi", "x.dll", "x.php", "x.ts",
                    "x", "x.", "x.mkv.exe", "x.exe .", ".mkv", "x.rar", "x.jpg"):
            with self.assertRaises(InboxError, msg=raw) as caught:
                inbox.clean_name(raw)
            self.assertEqual(caught.exception.status, 415, raw)
            self.assertIn("refus", caught.exception.message)

    def test_an_extension_in_the_middle_of_the_name_does_not_matter(self):
        self.assertEqual(inbox.clean_name("x.exe.mkv"), "x.exe.mkv")


class TestStoredName(unittest.TestCase):
    def test_the_length_limits_are_the_filesystem_ones(self):
        self.assertTrue(inbox.is_stored_name("a" * 251 + ".mkv"))                 # 255 octets
        self.assertFalse(inbox.is_stored_name("a" * 252 + ".mkv"))                # 256
        self.assertFalse(inbox.is_stored_name(""))
        self.assertTrue(inbox.is_stored_name("a"))

    def test_the_names_clean_name_never_produces_are_refused(self):
        for name in (".a.mkv", "a.mkv.", " a.mkv", "a.mkv ", "a.mkv.part", "a/b.mkv", "a\\b.mkv", "a:b.mkv", "a\x00b.mkv", "a\u2028b.mkv", None, 5, "a\udc80b.mkv"):
            self.assertFalse(inbox.is_stored_name(name), repr(name))
        for name in ("Film (2).mkv", "Amélie.mkv", "a.b.c.mkv"):
            self.assertTrue(inbox.is_stored_name(name), name)


class TestSmallHelpers(TempCase):
    def test_discard_says_whether_it_removed_something(self):
        path = self.tmp / "x.part"
        path.write_bytes(b"x")
        self.assertIs(inbox._discard(path), True)
        self.assertIs(inbox._discard(path), False)
        self.assertIs(inbox._discard(self.tmp), False)                            # un dossier non vide ou non effaçable : au mieux, sans erreur

    def test_a_link_to_a_file_is_not_a_file_of_the_inbox(self):
        self.put("a.mkv")
        folder = self.tmp / "inbox" / "alice"
        try:
            os.symlink(folder / "a.mkv", folder / "lien.mkv")
        except (OSError, NotImplementedError):
            self.skipTest("pas de liens symboliques ici")
        self.assertEqual([f["name"] for f in self.inbox.listing()], ["a.mkv"])
        self.assertEqual(self.inbox.used("alice"), 3)
        entry = next(e for e in os.scandir(folder) if e.name == "lien.mkv")
        self.assertIsNone(inbox._stat_file(entry))
        self.inbox.sweep(10**12)                                                  # le nettoyage ne touche pas non plus à un lien


class TestReceive(TempCase):
    def test_a_file_is_stored_under_its_account_with_its_bytes_and_no_part_left(self):
        out = self.put("Film.mkv", b"0123456789")
        self.assertEqual(out, {"name": "Film.mkv", "size": 10})
        self.assertEqual((self.tmp / "inbox" / "alice" / "Film.mkv").read_bytes(), b"0123456789")
        self.assertEqual(self.files(), ["Film.mkv"])

    def test_the_name_is_cleaned_before_anything_is_written(self):
        self.assertEqual(self.put("../../x.mkv")["name"], "x.mkv")
        self.assertEqual(self.put("NUL.mkv")["name"], "_NUL.mkv")
        self.assertEqual(self.files(), ["_NUL.mkv", "x.mkv"])
        self.assertEqual(sorted(p.name for p in (self.tmp / "inbox").iterdir()), ["alice"])

    def test_a_refused_extension_writes_nothing_and_reads_nothing(self):
        source = mock.Mock()
        with self.assertRaises(InboxError) as caught:
            self.inbox.receive("alice", "x.exe", source, 3)
        self.assertEqual(caught.exception.status, 415)
        source.read.assert_not_called()
        self.assertEqual(self.files(), [])

    def test_an_existing_file_is_never_overwritten(self):
        self.assertEqual(self.put("Film.mkv", b"un")["name"], "Film.mkv")
        self.assertEqual(self.put("Film.mkv", b"deux")["name"], "Film (2).mkv")
        self.assertEqual(self.put("Film.mkv", b"trois")["name"], "Film (3).mkv")
        folder = self.tmp / "inbox" / "alice"
        self.assertEqual([(folder / n).read_bytes() for n in ("Film.mkv", "Film (2).mkv", "Film (3).mkv")], [b"un", b"deux", b"trois"])

    def test_a_name_taken_by_an_upload_still_running_is_taken(self):
        folder = self.tmp / "inbox" / "alice"
        folder.mkdir(parents=True)
        (folder / "Film.mkv.part").write_bytes(b"en cours")
        self.assertEqual(self.put("Film.mkv")["name"], "Film (2).mkv")
        self.assertEqual((folder / "Film.mkv.part").read_bytes(), b"en cours")

    def test_two_accounts_have_their_own_folder_and_their_own_names(self):
        self.assertEqual(self.put("Film.mkv", account="alice")["name"], "Film.mkv")
        self.assertEqual(self.put("Film.mkv", account="bob")["name"], "Film.mkv")

    def test_the_body_is_read_in_pieces_of_at_most_one_megabyte(self):
        sizes, data = [], io.BytesIO(os.urandom(2 * MB + 5))

        class Spy:
            def read(self, n):
                sizes.append(n)
                return data.read(n)

        self.inbox.receive("alice", "gros.mkv", Spy(), 2 * MB + 5)
        self.assertEqual(sizes, [MB, MB, 5])
        self.assertEqual((self.tmp / "inbox" / "alice" / "gros.mkv").stat().st_size, 2 * MB + 5)

    def test_a_body_shorter_than_announced_leaves_nothing_behind(self):
        with self.assertRaises(InboxError) as caught:
            self.inbox.receive("alice", "Film.mkv", io.BytesIO(b"abcd"), 10)
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("4", caught.exception.message)
        self.assertIn("10", caught.exception.message)
        self.assertEqual(self.files(), [])

    def test_a_connection_that_breaks_half_way_leaves_nothing_behind_and_lets_the_error_through(self):
        for error in (ConnectionResetError, TimeoutError):
            source = mock.Mock()
            source.read.side_effect = [b"x" * 5, error("coupé")]
            with self.assertRaises(error):
                self.inbox.receive("alice", "Film.mkv", source, 50)
            self.assertEqual(self.files(), [])

    def test_a_full_disk_while_writing_is_a_507_and_leaves_nothing_behind(self):
        class Full:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def write(self, data):
                raise OSError(28, "No space left on device")

        with mock.patch.object(Inbox, "_open_part", return_value=("Film.mkv", Full())), self.assertRaises(InboxError) as caught:
            self.inbox.receive("alice", "Film.mkv", io.BytesIO(b"abc"), 3)
        self.assertEqual(caught.exception.status, 507)
        self.assertNotIn(str(self.tmp), caught.exception.message)

    def test_an_account_that_is_not_a_plain_identifier_is_refused(self):
        for account in ("..", "a/b", "a\\b", "", "é", "x" * 65, ".", "a b"):
            with self.assertRaises(InboxError, msg=account):
                self.put("x.mkv", account=account)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [])


class TestSameName(TempCase):
    def test_a_file_that_appears_under_the_chosen_name_during_the_upload_is_not_overwritten(self):
        folder = self.tmp / "inbox" / "alice"

        class Meanwhile:
            def read(self, n):
                (folder / "Film.mkv").write_bytes("posé par l'administrateur".encode())
                return b"x" * n

        with self.assertRaises(InboxError) as caught:
            self.inbox.receive("alice", "Film.mkv", Meanwhile(), 3)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual((folder / "Film.mkv").read_bytes(), "posé par l'administrateur".encode())
        self.assertEqual(self.files(), ["Film.mkv"])

    def test_the_numbering_goes_up_to_999_then_stops(self):
        folder = self.tmp / "inbox" / "alice"
        folder.mkdir(parents=True)
        (folder / "Film.mkv").write_bytes(b"1")
        for number in range(2, 999):
            (folder / f"Film ({number}).mkv").write_bytes(b"1")
        self.assertEqual(self.put("Film.mkv")["name"], "Film (999).mkv")
        with self.assertRaises(InboxError) as caught:
            self.put("Film.mkv")
        self.assertEqual(caught.exception.status, 409)
        self.assertFalse(list(folder.glob("*.part")))

    def test_a_part_that_another_upload_creates_right_after_the_publication_is_left_alone(self):
        folder = self.tmp / "inbox" / "alice"
        real = Inbox._publish

        def publish_then_another_upload_starts(box, where, final):
            real(box, where, final)
            (where / (final + inbox.PART)).write_bytes(b"un autre envoi en cours")

        with mock.patch.object(Inbox, "_publish", publish_then_another_upload_starts):
            self.assertEqual(self.put("Film.mkv")["name"], "Film.mkv")
        self.assertEqual((folder / "Film.mkv.part").read_bytes(), b"un autre envoi en cours")

    def test_many_uploads_of_the_same_name_at_once_all_keep_their_own_file(self):
        many = make(self.tmp, inbox_max_uploads=64)
        for round_ in range(3):
            names, errors, start = [], [], threading.Barrier(16)

            def run(n):
                start.wait(10)
                try:
                    names.append(many.receive("alice", "Film.mkv", io.BytesIO(bytes([n]) * 5), 5)["name"])
                except Exception as e:                                  # noqa: BLE001 : tout échec doit se voir
                    errors.append(e)

            threads = [threading.Thread(target=run, args=(n,)) for n in range(16)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
            self.assertEqual(errors, [])
            self.assertEqual(len(set(names)), 16, (round_, names))
            folder = self.tmp / "inbox" / "alice"
            self.assertEqual(sorted(bytes([n]) * 5 for n in range(16)), sorted((folder / name).read_bytes() for name in names))
            shutil.rmtree(folder)


class TestLimits(TempCase):
    def test_the_folders_above_the_inbox_are_created_when_they_are_missing(self):
        fresh = make(self.tmp / "pas" / "encore" / "la")
        self.assertEqual(fresh.receive("alice", "a.mkv", io.BytesIO(b"abc"), 3)["name"], "a.mkv")
        self.assertEqual((self.tmp / "pas" / "encore" / "la" / "inbox" / "alice" / "a.mkv").read_bytes(), b"abc")

    def test_an_unexpected_system_error_before_reading_gives_the_slot_back(self):
        one = make(self.tmp, inbox_max_uploads=1)
        with mock.patch.object(Inbox, "_open_part", side_effect=OSError(24, "Too many open files")), self.assertRaises(OSError):
            one.receive("alice", "x.mkv", io.BytesIO(b"abc"), 3)
        self.assertEqual(self.put("y.mkv", inbox_=one)["name"], "y.mkv")
        self.assertEqual(self.files(), ["y.mkv"])

    def test_the_free_space_to_keep_is_respected(self):
        keep = make(self.tmp, inbox_keep_free=1000)
        usage = types.SimpleNamespace(total=10**9, used=0, free=1010)
        with mock.patch.object(inbox.shutil, "disk_usage", return_value=usage):
            with self.assertRaises(InboxError) as caught:
                self.put("a.mkv", b"x" * 11, inbox_=keep)
            self.assertEqual(caught.exception.status, 507)
            self.assertNotIn(str(self.tmp), caught.exception.message)
            self.assertEqual(self.put("b.mkv", b"x" * 10, inbox_=keep)["size"], 10)
        self.assertEqual(self.files(), ["b.mkv"])

    def test_the_free_space_is_measured_where_the_files_go(self):
        keep = make(self.tmp)
        usage = types.SimpleNamespace(total=1, used=0, free=10**9)
        with mock.patch.object(inbox.shutil, "disk_usage", return_value=usage) as measured:
            self.put("a.mkv", inbox_=keep)
        self.assertEqual(Path(measured.call_args.args[0]), self.tmp / "inbox")

    def test_uploads_in_flight_count_against_the_free_space(self):
        keep = make(self.tmp, inbox_keep_free=0)
        usage = types.SimpleNamespace(total=10**9, used=0, free=100)
        with mock.patch.object(inbox.shutil, "disk_usage", return_value=usage):
            with keep.admit("alice", 70):
                with self.assertRaises(InboxError) as caught:
                    with keep.admit("bob", 40):
                        self.fail("l'espace aurait dû manquer")
                self.assertEqual(caught.exception.status, 507)
            with keep.admit("bob", 40):                                                  # la réservation de alice est rendue à la fin
                pass

    def test_no_more_than_the_allowed_uploads_at_once_and_a_slot_comes_back(self):
        with self.inbox.admit("alice", 1), self.inbox.admit("bob", 1):
            with self.assertRaises(InboxError) as caught:
                with self.inbox.admit("carol", 1):
                    self.fail("trois envois ne devraient pas passer")
            self.assertEqual(caught.exception.status, 429)
        with self.inbox.admit("carol", 1):
            pass

    def test_a_slot_comes_back_when_an_upload_fails(self):
        one = make(self.tmp, inbox_max_uploads=1)
        with self.assertRaises(InboxError):
            one.receive("alice", "x.mkv", io.BytesIO(b"ab"), 5)
        self.assertEqual(self.put("y.mkv", inbox_=one)["name"], "y.mkv")

    def test_the_no_space_answer_comes_before_the_busy_one_so_that_a_retry_is_not_pointless(self):
        one = make(self.tmp, inbox_max_uploads=1)
        usage = types.SimpleNamespace(total=10**9, used=0, free=100)
        with mock.patch.object(inbox.shutil, "disk_usage", return_value=usage), one.admit("alice", 1):
            with self.assertRaises(InboxError) as caught:
                one.receive("bob", "x.mkv", io.BytesIO(b"x" * 200), 200)
            self.assertEqual(caught.exception.status, 507)

    def test_there_is_no_limit_by_file_or_by_account_only_the_disk_refuses(self):
        usage = types.SimpleNamespace(total=10**15, used=0, free=10**15)
        with mock.patch.object(inbox.shutil, "disk_usage", return_value=usage):
            with self.inbox.admit("alice", 10**13), self.inbox.admit("alice", 10**13):         # deux envois de 10 To pour un seul compte
                pass
            self.put("a.mkv", b"x" * 1000)
            self.put("b.mkv", b"x" * 1000)
        self.assertEqual(self.inbox.used("alice"), 2000)

    def test_real_threads_the_third_upload_is_refused_while_two_are_running(self):
        gate, started, outcome = threading.Event(), threading.Semaphore(0), {}

        class Slow:
            def __init__(self):
                self.sent = False

            def read(self, n):
                if not self.sent:
                    self.sent = True
                    started.release()
                    gate.wait(10)
                return b"x" * n

        def run(account):
            try:
                self.inbox.receive(account, "x.mkv", Slow(), 4)
                outcome[account] = "ok"
            except InboxError as e:
                outcome[account] = e.status

        threads = [threading.Thread(target=run, args=(a,)) for a in ("alice", "bob")]
        for t in threads:
            t.start()
        self.assertTrue(started.acquire(timeout=10) and started.acquire(timeout=10))
        run("carol")
        gate.set()
        for t in threads:
            t.join(10)
        self.assertEqual(outcome, {"carol": 429, "alice": "ok", "bob": "ok"})


class TestListing(TempCase):
    def test_every_finished_file_of_every_account_newest_first_without_the_parts(self):
        self.put("vieux.mkv", account="alice")
        self.put("neuf.mp3", b"12345", account="bob")
        folder = self.tmp / "inbox"
        os.utime(folder / "alice" / "vieux.mkv", (1000, 1000))
        os.utime(folder / "bob" / "neuf.mp3", (2000, 2000))
        (folder / "bob" / "en cours.mkv.part").write_bytes(b"x")
        (folder / "bob" / "dossier").mkdir()
        (folder / "stray.txt").write_bytes(b"x")                                     # un fichier hors d'un dossier de compte
        self.assertEqual(self.inbox.listing(), [
            {"account": "bob", "name": "neuf.mp3", "size": 5, "date": 2000},
            {"account": "alice", "name": "vieux.mkv", "size": 3, "date": 1000}])

    def test_one_account_only(self):
        self.put("a.mkv", account="alice")
        self.put("b.mkv", account="bob")
        self.assertEqual([f["name"] for f in self.inbox.listing("alice")], ["a.mkv"])
        self.assertEqual(self.inbox.listing("carol"), [])

    def test_nothing_sent_yet(self):
        self.assertEqual(self.inbox.listing(), [])

    def test_a_file_deleted_while_the_list_is_read_is_skipped(self):
        self.put("a.mkv")
        self.put("b.mkv")

        class Gone:
            name = "gone.mkv"

            def is_file(self, follow_symlinks=True):
                return True

            def stat(self, follow_symlinks=True):
                raise FileNotFoundError(self.name)

        real = inbox._entries
        with mock.patch.object(inbox, "_entries", side_effect=lambda folder: [*real(folder), Gone()] if folder.name == "alice" else real(folder)):
            self.assertEqual([f["name"] for f in self.inbox.listing()], ["a.mkv", "b.mkv"])
            self.assertEqual(self.inbox.used("alice"), 6)

    def test_a_link_is_not_a_file(self):
        self.put("a.mkv")
        folder = self.tmp / "inbox" / "alice"
        try:
            os.symlink(self.tmp, folder / "lien.mkv")
        except (OSError, NotImplementedError):
            self.skipTest("pas de liens symboliques ici")
        self.assertEqual([f["name"] for f in self.inbox.listing()], ["a.mkv"])


class TestPaths(TempCase):
    def setUp(self):
        super().setUp()
        self.put("Film.mkv", b"abc")
        (self.tmp / "hub.db").write_bytes(b"secret")
        (self.tmp / "inbox" / "alice" / "Film.mkv.part").write_bytes(b"x")
        (self.tmp / "inbox" / "alice" / ".caché.mkv").write_bytes(b"x")
        (self.tmp / "inbox" / "alice" / "dossier").mkdir()

    def refused(self, account, name):
        with self.assertRaises(InboxError, msg=(account, name)) as caught:
            self.inbox.path_of(account, name)
        self.assertEqual(caught.exception.status, 404, (account, name))

    def test_a_finished_file_is_found(self):
        self.assertEqual(self.inbox.path_of("alice", "Film.mkv").read_bytes(), b"abc")

    def test_nothing_that_leaves_the_folder_of_the_account_is_found(self):
        for account, name in (("alice", ".."), ("alice", "."), ("alice", "../../hub.db"), ("alice", "..\\..\\hub.db"), ("alice", "a/../Film.mkv"), ("alice", "/Film.mkv"),
                              ("alice", "\\Film.mkv"), ("..", "hub.db"), (".", "hub.db"), ("../..", "hub.db"), ("alice/..", "alice/Film.mkv"), ("", "Film.mkv"), ("a b", "Film.mkv"),
                              ("alice", "Film.mkv\x00.txt"), ("alice", "C:Film.mkv"), ("alice", "Film.mkv:flux")):
            self.refused(account, name)

    def test_a_part_a_hidden_file_a_folder_and_a_missing_file_are_not_found(self):
        for name in ("Film.mkv.part", ".caché.mkv", "dossier", "absent.mkv", "", "Film.mkv.", "Film.mkv "):
            self.refused("alice", name)
        self.refused("bob", "Film.mkv")                                               # le fichier d'un autre compte

    def test_a_link_to_a_file_elsewhere_is_not_followed(self):
        link = self.tmp / "inbox" / "alice" / "lien.mkv"
        try:
            os.symlink(self.tmp / "hub.db", link)
        except (OSError, NotImplementedError):
            self.skipTest("pas de liens symboliques ici")
        self.refused("alice", "lien.mkv")

    def test_a_path_resolution_that_misbehaves_does_not_refuse_a_good_upload(self):
        """Sous Windows, `Path.resolve()` échoue par moments sur un dossier en pleine activité : le dossier du compte ne doit pas en dépendre."""
        with mock.patch.object(Path, "resolve", side_effect=lambda *a, **k: Path("C:/autre/forme")):
            self.assertEqual(self.put("a.mkv")["name"], "a.mkv")
            self.assertEqual(self.inbox.path_of("alice", "a.mkv").read_bytes(), b"abc")
            self.assertEqual(sorted(f["name"] for f in self.inbox.listing()), ["Film.mkv", "a.mkv"])

    def test_a_folder_of_the_account_that_is_a_junction_elsewhere_is_not_followed(self):
        if sys.platform != "win32":
            self.skipTest("les jonctions n'existent que sous Windows")
        import _winapi
        outside = self.tmp / "dehors"
        outside.mkdir()
        (outside / "x.mkv").write_bytes(b"x")
        _winapi.CreateJunction(str(outside), str(self.tmp / "inbox" / "mallory"))
        self.refused("mallory", "x.mkv")
        self.assertEqual(self.inbox.listing("mallory"), [])
        self.assertEqual([f["account"] for f in self.inbox.listing()], ["alice"])
        with self.assertRaises(InboxError):
            self.put("y.mkv", account="mallory")
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["x.mkv"])

    def test_a_folder_of_the_account_that_is_a_link_elsewhere_is_not_followed(self):
        outside = self.tmp / "dehors"
        outside.mkdir()
        (outside / "x.mkv").write_bytes(b"x")
        try:
            os.symlink(outside, self.tmp / "inbox" / "mallory", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("pas de liens symboliques ici")
        self.refused("mallory", "x.mkv")
        self.assertEqual(self.inbox.listing("mallory"), [])

    def test_remove_deletes_the_file_and_only_that_one(self):
        self.inbox.remove("alice", "Film.mkv")
        self.assertEqual(self.files(), [".caché.mkv", "Film.mkv.part", "dossier"])
        self.refused_remove("alice", "Film.mkv")

    def refused_remove(self, account, name):
        with self.assertRaises(InboxError) as caught:
            self.inbox.remove(account, name)
        self.assertEqual(caught.exception.status, 404)

    def test_remove_refuses_what_path_of_refuses(self):
        for account, name in (("alice", "../../hub.db"), ("alice", "Film.mkv.part"), ("alice", "dossier"), ("..", "hub.db")):
            self.refused_remove(account, name)
        self.assertTrue((self.tmp / "hub.db").exists())
        self.assertTrue((self.tmp / "inbox" / "alice" / "Film.mkv.part").exists())


class TestSweep(TempCase):
    def setUp(self):
        super().setUp()
        self.folder = self.tmp / "inbox" / "alice"
        self.folder.mkdir(parents=True)
        self.now = 10 * 86400

    def part(self, name, age):
        path = self.folder / name
        path.write_bytes(b"x")
        os.utime(path, (self.now - age, self.now - age))
        return path

    def test_the_parts_older_than_a_day_go_the_others_stay(self):
        old, young = self.part("vieux.mkv.part", 86400 + 1), self.part("jeune.mkv.part", 86400 - 60)
        done = self.part("fini.mkv", 30 * 86400)
        self.assertEqual(self.inbox.sweep(self.now), 1)
        self.assertEqual((old.exists(), young.exists(), done.exists()), (False, True, True))

    def test_a_part_exactly_one_day_old_stays_and_a_second_older_goes(self):
        exact, older = self.part("pile.mkv.part", 86400), self.part("plus.mkv.part", 86401)
        self.assertEqual(self.inbox.sweep(self.now), 1)
        self.assertEqual((exact.exists(), older.exists()), (True, False))

    def test_every_account_is_swept_and_a_folder_in_the_way_is_left_alone(self):
        other = self.tmp / "inbox" / "bob"
        other.mkdir()
        old = other / "x.mkv.part"
        old.write_bytes(b"x")
        os.utime(old, (0, 0))
        (self.folder / "dossier.part").mkdir()
        os.utime(self.folder / "dossier.part", (0, 0))
        self.assertEqual(self.inbox.sweep(self.now), 1)
        self.assertFalse(old.exists())
        self.assertTrue((self.folder / "dossier.part").is_dir())

    def test_a_part_that_vanishes_during_the_sweep_does_not_stop_it(self):
        old = self.part("vieux.mkv.part", 86400 + 1)

        class Gone:
            name = "parti.mkv.part"
            path = str(self.folder / "parti.mkv.part")

            def is_file(self, follow_symlinks=True):
                return True

            def stat(self, follow_symlinks=True):
                raise FileNotFoundError(self.name)

        real = inbox._entries
        with mock.patch.object(inbox, "_entries", side_effect=lambda folder: [Gone(), *real(folder)] if folder.name == "alice" else real(folder)):
            self.assertEqual(self.inbox.sweep(self.now), 1)
        self.assertFalse(old.exists())

    def test_nothing_to_sweep_before_the_first_upload(self):
        shutil.rmtree(self.tmp / "inbox")
        self.assertEqual(self.inbox.sweep(self.now), 0)

    def test_the_default_moment_is_now(self):
        old = self.part("vieux.mkv.part", 0)
        os.utime(old, (time.time() - 2 * 86400, time.time() - 2 * 86400))
        self.assertEqual(self.inbox.sweep(), 1)


class Stop(Exception):
    """Fait sortir la boucle infinie du fil."""


class TestKeepClean(TempCase):
    def start(self):
        with mock.patch.object(inbox.threading, "Thread") as thread:
            self.inbox.keep_clean()
        thread.assert_called_once()
        self.assertEqual((thread.call_args.kwargs["name"], thread.call_args.kwargs["daemon"]), ("inbox-sweep", True))
        thread.return_value.start.assert_called_once_with()
        return thread.call_args.kwargs["target"]

    def test_it_sweeps_at_once_then_every_day(self):
        with mock.patch.object(self.inbox, "sweep") as sweep:
            loop = self.start()
            self.assertEqual(sweep.call_count, 1)
            with mock.patch.object(inbox.time, "sleep", side_effect=[None, Stop]) as sleep, self.assertRaises(Stop):
                loop()
        self.assertEqual([c.args for c in sleep.call_args_list], [(86400,), (86400,)])
        self.assertEqual(sweep.call_count, 2)

    def test_a_sweep_that_fails_does_not_stop_the_next_ones(self):
        with mock.patch.object(self.inbox, "sweep", side_effect=[0, OSError("disque"), 0]) as sweep:
            loop = self.start()
            with mock.patch.object(inbox.time, "sleep", side_effect=[None, None, Stop]), self.assertRaises(Stop):
                loop()
        self.assertEqual(sweep.call_count, 3)

    def test_the_first_sweep_failing_does_not_stop_the_server_from_starting(self):
        with mock.patch.object(self.inbox, "sweep", side_effect=OSError("disque")):
            self.start()


if __name__ == "__main__":
    unittest.main()
