"""La boîte de réception face à des noms hostiles, sans réseau : formes Unicode (NFC/NFD, sosies d'un point ou d'une barre), points et espaces de fin que Windows ignore, flux de données alternatifs NTFS (`nom:flux`), noms courts 8.3, noms réservés,
et deux envois qui arrivent ensemble dans un même nouveau sous-dossier (fils, avec un point de rendez-vous : pas de `sleep`)."""

from __future__ import annotations

import io
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from hub import inbox
from hub.inbox import InboxError
from tests.test_inbox import make
from tests.test_inbox_subfolders import SubCase, tree

WINDOWS = os.name == "nt"
LAG = 10                     # secondes : un point de rendez-vous qui n'aboutit pas échoue le test au lieu de le bloquer


def short_name(path):
    """Le nom court 8.3 de `path` (Windows), ou None quand le volume n'en fabrique pas."""
    if not WINDOWS:
        return None
    import ctypes
    buffer = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 1024):
        return None
    short = Path(buffer.value).name
    return short if short.lower() != path.name.lower() else None


class Gate:
    """Un corps d'envoi qui se bloque à la première lecture jusqu'à ce que `wait_for` soit levé : à ce moment l'envoi a déjà posé son dossier et son `.part`. `ready` dit « je suis posé ». `data` : le corps, puis la fin ; `short` : s'arrête sans rien rendre (envoi interrompu)."""

    def __init__(self, data=b"abc", wait_for=None, short=False):
        self.data, self.short, self.wait_for = io.BytesIO(data), short, wait_for
        self.ready, self.first = threading.Event(), True

    def read(self, n):
        if self.first:
            self.first = False
            self.ready.set()
            if self.wait_for is not None and not self.wait_for.wait(LAG):
                raise TimeoutError("le point de rendez-vous n'a pas été atteint")
            if self.short:
                return b""
        return self.data.read(n)


class Sender(threading.Thread):
    """Un envoi dans un fil : `reply` ou `error` après `join`."""

    def __init__(self, box, name, source, length, sub, account="alice"):
        super().__init__(daemon=True)
        self.call = (account, name, source, length, sub)
        self.box, self.reply, self.error = box, None, None

    def run(self):
        try:
            self.reply = self.box.receive(*self.call)
        except Exception as e:
            self.error = e


class TestSegmentsOfEveryForm(unittest.TestCase):
    def test_a_decomposed_path_becomes_composed_whatever_the_script(self):
        self.assertEqual(inbox.clean_dir("Cafe\u0301/Le\u0301a"), ("Café", "Léa"))
        self.assertEqual(inbox.clean_dir("\u1112\u1161\u11ab"), ("\ud55c",))              # jamos coréens séparés : une syllabe
        self.assertEqual(inbox.clean_dir("A\u030angstro\u0308m"), ("Ångström",))

    def test_dots_and_spaces_at_either_end_are_dropped_in_every_mix(self):
        for raw in ("a.", "a ", "a. .", "a . ", " a", ". a", " . a. . "):
            self.assertEqual(inbox.clean_dir(raw), ("a",), repr(raw))
        self.assertEqual(inbox.clean_dir("a. /b ./ c"), ("a", "b", "c"))

    def test_a_cut_that_ends_on_a_space_or_a_dot_leaves_neither(self):
        (segment,) = inbox.clean_dir("a" * 119 + " b")
        self.assertEqual(segment, "a" * 119)
        (segment,) = inbox.clean_dir("a" * 118 + "..b")
        self.assertEqual(segment, "a" * 118)

    def test_look_alikes_of_a_dot_or_a_slash_are_ordinary_names_never_a_separator(self):
        for raw in ("\uff0e\uff0e", "\u2025", "\u2024\u2024", "\u3002\u3002", "\ufe52\ufe52", "\uff0f", "\u2215", "\u29f8", "\uff3c", "\u2216", "\u0589\u0589"):
            self.assertEqual(inbox.clean_dir(raw), (raw,), ascii(raw))
            self.assertTrue(inbox.is_folder_name(raw), ascii(raw))
        self.assertEqual(inbox.clean_dir("\uff0e\uff0e/\uff0f/\u2215.."), ("\uff0e\uff0e", "\uff0f", "\u2215"))      # le dernier perd ses vrais points de fin

    def test_invisible_and_direction_changing_characters_are_replaced(self):
        self.assertEqual(inbox.clean_dir("a\u202eb/c\u200bd/e\ufeff"), ("a_b", "c_d", "e_"))
        self.assertEqual(inbox.clean_dir("\u200b"), ("_",))                              # il ne reste pas rien : un nom visible
        self.assertEqual(inbox.clean_dir("a\ud800b"), ("a_b",))                          # une demi-paire de substitution ne se code pas

    def test_a_name_with_a_colon_loses_it_so_no_stream_can_be_named(self):
        self.assertEqual(inbox.clean_dir("a:b/a::$DATA/a::$INDEX_ALLOCATION/x:y:z"), ("a_b", "a__$DATA", "a__$INDEX_ALLOCATION", "x_y_z"))
        self.assertEqual(inbox.clean_name("x.mkv:flux.mkv"), "x.mkv_flux.mkv")
        self.assertEqual(inbox.clean_name("x:y.mkv"), "x_y.mkv")

    def test_a_stream_after_the_extension_is_a_refused_type_not_a_stream(self):
        for raw in ("x.mkv:Zone.Identifier", "x.mkv::$DATA", "x.mkv:flux", "x.mkv:"):
            with self.assertRaises(InboxError, msg=raw) as caught:
                inbox.clean_name(raw)
            self.assertIn(caught.exception.status, (400, 415), raw)

    def test_the_reserved_names_of_windows_are_prefixed_in_every_spelling(self):
        for raw in ("con", "Con.", "CON .x", "com1", "LPT9", "NUL", "aux.tar.gz", "CON. ", " nul", "Prn"):
            (segment,) = inbox.clean_dir(raw)
            self.assertTrue(segment.startswith("_"), raw)
        for raw in ("CONSOLE", "COM0", "COM10", "CONN", "NULL", "AUXX", "LPT"):
            self.assertEqual(inbox.clean_dir(raw), (raw,), raw)

    def test_a_short_name_is_an_ordinary_name(self):
        self.assertEqual(inbox.clean_dir("PROGRA~1/Docume~1"), ("PROGRA~1", "Docume~1"))
        self.assertTrue(inbox.is_stored_name("FILMS~1.mkv"))


class TestNothingAliasedIsAFile(SubCase):
    """Windows ignore un point ou une espace de fin et ouvre `nom::$DATA` comme `nom` : un chemin qui s'écrit autrement que dans la liste ne doit rien ouvrir, comme sur un système qui ne les ignore pas."""

    def setUp(self):
        super().setUp()
        self.send("x.mkv", "a/b", data=b"secret")

    def test_a_dir_with_a_final_dot_or_space_or_a_stream_opens_nothing(self):
        for sub in ("a.", "a ", "a/b.", "a/b ", " a", "a/ b", "a/b. .", "a::$INDEX_ALLOCATION", "a/b::$INDEX_ALLOCATION", "a:b", "a/b:"):
            self.assertEqual(self.status(self.inbox.path_of, "alice", "x.mkv", sub), 404, repr(sub))
            self.assertEqual(self.status(self.inbox.remove, "alice", "x.mkv", sub), 404, repr(sub))
        self.assertEqual(self.inbox.path_of("alice", "x.mkv", "a/b").read_bytes(), b"secret")

    def test_a_name_with_a_final_dot_or_space_or_a_stream_opens_nothing(self):
        for name in ("x.mkv.", "x.mkv ", "x.mkv. .", "x.mkv::$DATA", "x.mkv:flux", "x.mkv:", ".x.mkv", "x.mkv.part", "x.mkv/", "x.mkv\\", "..\\x.mkv"):
            self.assertEqual(self.status(self.inbox.path_of, "alice", name, "a/b"), 404, repr(name))
            self.assertEqual(self.status(self.inbox.remove, "alice", name, "a/b"), 404, repr(name))
        self.assertEqual(self.inbox.path_of("alice", "x.mkv", "a/b").read_bytes(), b"secret")

    def test_an_account_folder_with_a_final_dot_or_a_stream_opens_nothing(self):
        for folder in ("alice.", "alice ", "alice::$INDEX_ALLOCATION", "ALICE.", "alice:x"):
            self.assertEqual(self.status(self.inbox.path_of, folder, "x.mkv", "a/b"), 404, repr(folder))

    def test_a_stream_in_the_name_of_a_sent_file_makes_no_second_file(self):
        reply = self.send("x.mkv:flux.mkv", "a/b", data=b"autre")
        self.assertEqual(reply["name"], "x.mkv_flux.mkv")
        self.assertEqual(tree(self.alice), ["a/", "a/b/", "a/b/x.mkv", "a/b/x.mkv_flux.mkv"])
        self.assertEqual((self.alice / "a" / "b" / "x.mkv").read_bytes(), b"secret")      # le flux n'a pas touché l'autre fichier

    def test_a_stream_alone_does_not_create_an_empty_file_of_the_base_name(self):
        self.send("y.mkv:flux.mkv", "c")
        self.assertFalse((self.alice / "c" / "y.mkv").exists())

    def test_a_folder_with_a_stream_in_its_name_is_made_with_the_name_cleaned(self):
        reply = self.send("z.mkv", "d::$INDEX_ALLOCATION")
        self.assertEqual(reply["dir"], "d__$INDEX_ALLOCATION")
        self.assertEqual(sorted(p.name for p in self.alice.iterdir()), ["a", "d__$INDEX_ALLOCATION"])

    def test_a_path_that_ends_with_a_dot_is_the_same_folder_as_without(self):
        self.assertEqual(self.send("y.mkv", "a./b ")["dir"], "a/b")                       # nettoyé à l'envoi : le même dossier
        self.assertEqual(tree(self.alice), ["a/", "a/b/", "a/b/x.mkv", "a/b/y.mkv"])


class TestUnicodeFormsOnDisk(SubCase):
    def test_a_decomposed_name_is_stored_composed_and_found_by_its_composed_name(self):
        self.assertEqual(self.send("Cafe\u0301.mkv", "Cafe\u0301")["name"], "Café.mkv")
        self.assertEqual(self.send("Cafe\u0301.mkv", "Café")["name"], "Café (2).mkv")
        self.assertEqual(sorted(p.name for p in self.alice.iterdir()), ["Café"])
        self.assertEqual(self.inbox.path_of("alice", "Café.mkv", "Café").read_bytes(), b"abc")

    def test_a_folder_made_by_hand_decomposed_takes_a_composed_upload_and_the_reply_names_the_folder_that_is_there(self):
        (self.alice / "Cafe\u0301").mkdir(parents=True)
        reply = self.send("a.mkv", "Café")
        self.assertEqual(len([p for p in self.alice.iterdir()]), 1)                       # jamais deux dossiers « Café »
        self.assertEqual(reply["dir"], "Cafe\u0301")
        self.assertEqual(self.inbox.path_of("alice", "a.mkv", reply["dir"]).read_bytes(), b"abc")      # ce que la réponse dit se relit tel quel

    def test_every_dir_and_name_of_the_listing_reads_back_and_removes(self):
        names = ["%2e%2e", "%2f", "a b", "Café", "Cafe\u0301", "日本語", "\U0001f600", "(2)", "\u2025", "\uff0e\uff0e", "\uff0f", "PROGRA~1", "x_y"]
        for number, folder in enumerate(names):
            self.send(f"f{number}.mkv", folder + "/" + folder, data=bytes([number]))
        listed = self.inbox.listing("alice")
        self.assertEqual(len(listed), len(names))
        for f in listed:
            self.assertEqual(self.inbox.path_of("alice", f["name"], f["dir"]).read_bytes(), bytes([int(f["name"][1:-4])]), f)
        for f in listed:
            self.inbox.remove("alice", f["name"], f["dir"])
        self.assertEqual(tree(self.alice), [])

    def test_a_dir_that_decodes_to_the_parent_is_not_a_folder_but_its_literal_text_is(self):
        self.send("a.mkv", "%2e%2e")
        self.assertEqual(self.inbox.path_of("alice", "a.mkv", "%2e%2e").name, "a.mkv")
        self.assertEqual(self.status(self.inbox.path_of, "alice", "a.mkv", ".."), 404)
        self.assertEqual(self.status(self.inbox.path_of, "alice", "a.mkv", "%252e%252e"), 404)        # jamais décodé, ni une fois ni deux

    def test_a_lone_surrogate_in_a_dir_to_read_is_a_404_not_a_crash(self):
        self.send("a.mkv", "x")
        for sub in ("\ud800", "x/\udfff", "x\ud800"):
            self.assertEqual(self.status(self.inbox.path_of, "alice", "a.mkv", sub), 404, ascii(sub))
        self.assertEqual(self.status(self.inbox.path_of, "alice", "\ud800.mkv", "x"), 404)


class TestOnlyListedNamesAreServed(SubCase):
    """Sans noms courts 8.3 : ce que le système ouvre sans que la liste du dossier le donne (l'alias d'un fichier, d'un dossier) n'est jamais servi ni supprimé. On aveugle ce qui doit l'être pour fabriquer l'alias."""

    def hidden(self, *names):
        """`_entries` sans les entrées `names` : le fichier est là et s'ouvre par son nom, mais la liste ne le donne pas, comme un alias."""
        real = inbox._entries
        return mock.patch.object(inbox, "_entries", lambda folder: [e for e in real(folder) if e.name not in names])

    def test_a_file_the_listing_does_not_give_is_neither_read_nor_removed(self):
        self.send("x.mkv", data=b"secret")
        (self.alice / "y.mkv.part").write_bytes(b"en cours")
        with self.hidden("x.mkv", "y.mkv.part"):
            self.assertEqual(self.status(self.inbox.path_of, "alice", "x.mkv"), 404)
            self.assertEqual(self.status(self.inbox.remove, "alice", "x.mkv"), 404)
        self.assertEqual(self.inbox.path_of("alice", "x.mkv").read_bytes(), b"secret")
        self.assertEqual((self.alice / "y.mkv.part").read_bytes(), b"en cours")

    def test_a_file_the_listing_does_not_give_in_a_subfolder_is_neither_read_nor_removed(self):
        self.send("x.mkv", "a/b", data=b"secret")
        with self.hidden("x.mkv"):
            self.assertEqual(self.status(self.inbox.path_of, "alice", "x.mkv", "a/b"), 404)
            self.assertEqual(self.status(self.inbox.remove, "alice", "x.mkv", "a/b"), 404)
        self.assertEqual(self.inbox.path_of("alice", "x.mkv", "a/b").read_bytes(), b"secret")

@unittest.skipUnless(WINDOWS, "les noms courts 8.3 n'existent que sous Windows")
class TestShortNames(SubCase):
    """NTFS donne à « Dossier Long » le nom court `DOSSIE~1` et ouvre les deux : le hub ne doit ni écrire ailleurs ni écraser à travers l'un."""

    def alias_of(self, path):
        short = short_name(path)
        if short is None:
            self.skipTest("ce volume ne fabrique pas de noms courts 8.3")
        return short

    def test_a_file_sent_under_the_short_name_of_another_does_not_overwrite_it(self):
        self.alice.mkdir(parents=True)
        (self.alice / "Un film avec un long nom.mkv").write_bytes(b"original")
        short = self.alias_of(self.alice / "Un film avec un long nom.mkv")
        reply = self.send(short, data=b"autre")
        self.assertEqual((self.alice / "Un film avec un long nom.mkv").read_bytes(), b"original")
        self.assertEqual(self.inbox.path_of("alice", reply["name"]).read_bytes(), b"autre")
        self.assertNotEqual(reply["name"], short)

    def test_a_folder_sent_under_the_short_name_of_another_is_that_folder_and_nothing_is_made_elsewhere(self):
        (self.alice / "Un dossier avec un long nom").mkdir(parents=True)
        short = self.alias_of(self.alice / "Un dossier avec un long nom")
        reply = self.send("x.mkv", short)
        self.assertEqual((reply["name"], reply["dir"]), ("x.mkv", "Un dossier avec un long nom"))         # le nom que la liste donne, pas l'alias
        self.assertEqual(tree(self.alice), ["Un dossier avec un long nom/", "Un dossier avec un long nom/x.mkv"])
        self.assertEqual(sorted(p.name for p in (self.tmp / "inbox").iterdir()), ["alice"])

    def test_a_folder_below_the_short_name_of_another_is_made_inside_that_folder(self):
        (self.alice / "Un dossier avec un long nom").mkdir(parents=True)
        short = self.alias_of(self.alice / "Un dossier avec un long nom")
        reply = self.send("x.mkv", short + "/Sous")
        self.assertEqual(reply["dir"], "Un dossier avec un long nom/Sous")
        self.assertEqual(tree(self.alice), ["Un dossier avec un long nom/", "Un dossier avec un long nom/Sous/", "Un dossier avec un long nom/Sous/x.mkv"])

    def test_a_folder_is_read_and_removed_only_under_a_name_the_listing_gives_not_under_its_short_name(self):
        self.send("a.mkv", "Un dossier avec un long nom/Un sous-dossier long", data=b"vu")
        base = self.alice / "Un dossier avec un long nom"
        short, sub_short = self.alias_of(base), self.alias_of(base / "Un sous-dossier long")
        for sub in (short + "/Un sous-dossier long", "Un dossier avec un long nom/" + sub_short, short + "/" + sub_short):
            self.assertEqual(self.status(self.inbox.path_of, "alice", "a.mkv", sub), 404, sub)
            self.assertEqual(self.status(self.inbox.remove, "alice", "a.mkv", sub), 404, sub)
        self.assertEqual(self.inbox.path_of("alice", "a.mkv", "Un dossier avec un long nom/Un sous-dossier long").read_bytes(), b"vu")
        self.assertEqual(self.inbox.path_of("alice", "a.mkv", "UN DOSSIER avec un LONG nom/un sous-dossier LONG").read_bytes(), b"vu")      # la casse reste équivalente

    def test_a_failed_upload_under_a_short_name_leaves_the_other_folders_content(self):
        (self.alice / "Un dossier avec un long nom").mkdir(parents=True)
        (self.alice / "Un dossier avec un long nom" / "garde.mkv").write_bytes(b"x")
        short = self.alias_of(self.alice / "Un dossier avec un long nom")
        with self.assertRaises(InboxError) as caught:
            self.inbox.receive("alice", "rate.mkv", io.BytesIO(b"ab"), 10, short)
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(tree(self.alice), ["Un dossier avec un long nom/", "Un dossier avec un long nom/garde.mkv"])

    def test_the_short_name_of_a_part_or_of_a_finished_file_is_neither_read_nor_removed(self):
        self.send("x.mkv", data=b"fini")
        (self.alice / "Un film en cours.mkv.part").write_bytes(b"en cours")
        (self.alice / "Un film fini avec un long nom.mkv").write_bytes(b"fini aussi")
        for long in ("Un film en cours.mkv.part", "Un film fini avec un long nom.mkv"):
            short = self.alias_of(self.alice / long)
            self.assertEqual(self.status(self.inbox.path_of, "alice", short), 404, short)
            self.assertEqual(self.status(self.inbox.remove, "alice", short), 404, short)
        self.assertEqual((self.alice / "Un film en cours.mkv.part").read_bytes(), b"en cours")
        self.assertEqual(self.inbox.path_of("alice", "Un film fini avec un long nom.mkv").read_bytes(), b"fini aussi")

    def test_a_short_name_that_is_a_file_is_a_clear_409_and_nothing_is_written(self):
        (self.alice / "Un fichier avec un long nom.mkv").parent.mkdir(parents=True)
        (self.alice / "Un fichier avec un long nom.mkv").write_bytes(b"x")
        short = self.alias_of(self.alice / "Un fichier avec un long nom.mkv")
        before = tree(self.alice)
        with self.assertRaises(InboxError) as caught:
            self.send("x.mkv", short)
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("autre nom", caught.exception.message)
        self.assertEqual(tree(self.alice), before)


@unittest.skipUnless(WINDOWS, "ces noms ne sont réservés que sous Windows")
class TestWhatWindowsCannotOpen(SubCase):
    """Un nom que `clean_name` accepte doit pouvoir s'écrire : sinon l'envoi échoue en 503 (« boîte inutilisable ») pour un simple nom de fichier. Selon la version de Windows, `COM\u00b9`, `CONIN$`… sont réservés (Windows 10) ou non (Windows 11) : le test suit le système."""

    def test_a_name_with_a_superscript_digit_or_a_console_device_is_stored_not_a_503(self):
        for raw in ("COM\u00b9.mkv", "LPT\u00b2.srt", "COM\u00b3.mkv", "CONIN$.mkv", "CONOUT$.mkv", "CLOCK$.mkv"):
            with mock.patch.object(sys, "stderr", io.StringIO()):
                try:
                    reply = self.send(raw)
                except InboxError as e:
                    self.assertNotEqual(e.status, 503, f"{raw} : {e.message}")           # un refus net (400 / 415) serait acceptable, pas « boîte inutilisable »
                    continue
            self.assertEqual(self.inbox.path_of("alice", reply["name"]).read_bytes(), b"abc", raw)

    def test_a_folder_with_such_a_name_takes_a_file(self):
        for raw in ("COM\u00b9", "CONIN$", "CLOCK$"):
            with mock.patch.object(sys, "stderr", io.StringIO()):
                reply = self.send("x.mkv", raw)
            self.assertEqual(self.inbox.path_of("alice", "x.mkv", reply["dir"]).read_bytes(), b"abc", raw)


class TestLongestPath(SubCase):
    def test_four_levels_of_the_longest_names_and_the_longest_file_name_are_stored_when_the_system_can_hold_them(self):
        deep = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(("\\\\?\\" + str(deep)) if WINDOWS else deep, True))
        sub = "/".join(chr(0xe9 + n) * 60 for n in range(4))                              # 4 × 120 octets
        name = "f" * 195 + ".mkv"
        try:
            probe = deep.joinpath(*sub.split("/"))
            probe.mkdir(parents=True)
            (probe / name).write_bytes(b"x")
        except OSError:
            self.skipTest("ce système ne tient pas un chemin aussi long")
        reply = self.send(name, sub)
        self.assertEqual(reply["dir"], sub)
        self.assertEqual(self.inbox.path_of("alice", name, sub).read_bytes(), b"abc")
        self.inbox.remove("alice", name, sub)
        self.assertEqual(tree(self.alice), [])


class TestSameNewSubfolderAtTheSameTime(SubCase):
    def setUp(self):
        super().setUp()
        self.box = make(self.tmp, self.db, inbox_max_uploads=64)

    def run_all(self, *senders):
        for sender in senders:
            sender.start()
        for sender in senders:
            sender.join(LAG * 2)
            self.assertFalse(sender.is_alive(), "un envoi est resté bloqué")

    def test_two_uploads_into_the_same_new_folder_both_in_flight_make_one_folder_and_keep_both_files(self):
        meet = threading.Barrier(2, timeout=LAG)

        class Meet(Gate):
            def read(self, n):
                if self.first:
                    self.first = False
                    self.ready.set()
                    meet.wait()                          # les deux ont posé leur dossier et leur .part
                return self.data.read(n)

        one = Sender(self.box, "Film.mkv", Meet(b"un"), 2, "Vacances/Ete")
        two = Sender(self.box, "Film.mkv", Meet(b"deux!"), 5, "vacances/ETE")
        self.run_all(one, two)
        self.assertEqual((one.error, two.error), (None, None))
        self.assertEqual(one.reply["dir"], two.reply["dir"])                              # le même dossier, nom de celui qui est sur le disque
        self.assertEqual(sorted(p.name for p in self.alice.iterdir()), [one.reply["dir"].split("/")[0]])
        self.assertEqual(sorted([one.reply["name"], two.reply["name"]]), ["Film (2).mkv", "Film.mkv"])
        stored = {p.name: p.read_bytes() for p in (self.alice / one.reply["dir"]).iterdir()}
        self.assertEqual(sorted(stored.values()), [b"deux!", b"un"])
        self.assertEqual(len(tree(self.alice)), 4)                                        # 2 dossiers + 2 fichiers, aucun .part

    def test_an_upload_that_fails_does_not_take_back_the_folder_another_upload_is_filling(self):
        failing, filling = threading.Event(), threading.Event()
        loser = Gate(short=True, wait_for=filling)               # a posé le dossier (et l'a créé), attend que l'autre soit dedans, puis s'interrompt
        winner = Gate(b"gagne", wait_for=failing)                # attend la fin de l'échec avant de continuer
        one = Sender(self.box, "perdu.mkv", loser, 10, "Neuf/Profond")
        two = Sender(self.box, "gagne.mkv", winner, 5, "neuf/PROFOND")
        one.start()
        self.assertTrue(loser.ready.wait(LAG))
        two.start()
        self.assertTrue(winner.ready.wait(LAG))
        filling.set()                                            # l'échec a lieu alors que le .part de l'autre est dans le dossier
        one.join(LAG)
        self.assertEqual(one.error.status, 400)
        self.assertEqual(sorted(p.name for p in (self.alice / "Neuf" / "Profond").iterdir()), ["gagne.mkv.part"])     # son dossier est resté
        failing.set()
        two.join(LAG)
        self.assertIsNone(two.error)
        self.assertEqual(two.reply["dir"], "Neuf/Profond")
        self.assertEqual(tree(self.alice), ["Neuf/", "Neuf/Profond/", "Neuf/Profond/gagne.mkv"])

    def test_removing_the_last_file_of_a_folder_while_an_upload_is_in_it_keeps_the_folder(self):
        self.send("vieux.mkv", "x/y")
        waiting, go = Gate(b"neuf"), threading.Event()
        waiting.wait_for = go
        upload = Sender(self.box, "neuf.mkv", waiting, 4, "X/Y")
        upload.start()
        self.assertTrue(waiting.ready.wait(LAG))
        self.box.remove("alice", "vieux.mkv", "x/y")                                      # le dossier ne contient plus que le .part de l'envoi
        self.assertTrue((self.alice / "x" / "y").is_dir())
        go.set()
        upload.join(LAG)
        self.assertIsNone(upload.error)
        self.assertEqual(tree(self.alice), ["x/", "x/y/", "x/y/neuf.mkv"])

    def test_many_uploads_failures_and_removals_in_the_same_new_folder_lose_no_file_and_leave_nothing_behind(self):
        keepers = [Sender(self.box, f"u{t}_{i}.mkv", io.BytesIO(bytes([t, i])), 2, ("Pile/Haut", "pile/HAUT", "PILE/haut")[t % 3]) for t in range(4) for i in range(6)]

        def fail(n):
            return Sender(self.box, f"rate{n}.mkv", io.BytesIO(b"ab"), 10, "pile/haut")

        failures = [fail(n) for n in range(30)]
        cycles = []

        def churn():
            for n in range(15):                          # envoyé puis retiré : le dossier se vide et se recrée sans cesse
                reply = self.box.receive("alice", f"g{n}.mkv", io.BytesIO(b"g"), 1, "Pile/Haut")
                try:
                    self.box.remove("alice", reply["name"], reply["dir"])
                except InboxError as e:
                    cycles.append(e)

        churner = threading.Thread(target=churn, daemon=True)
        churner.start()
        self.run_all(*keepers, *failures)
        churner.join(LAG * 3)
        self.assertEqual(cycles, [])
        self.assertEqual([s.error for s in keepers], [None] * len(keepers))
        self.assertEqual(sorted(s.error.status for s in failures), [400] * 30)
        found = {p.name: p.read_bytes() for p in self.alice.rglob("*") if p.is_file()}
        self.assertEqual(found, {f"u{t}_{i}.mkv": bytes([t, i]) for t in range(4) for i in range(6)})
        folders = [p.relative_to(self.alice).as_posix() for p in self.alice.rglob("*") if p.is_dir()]
        self.assertEqual(len(folders), 2, folders)                                        # un seul « pile/haut » : jamais un doublon
        self.assertEqual(len({f.lower() for f in folders}), 2)


if __name__ == "__main__":
    unittest.main()
