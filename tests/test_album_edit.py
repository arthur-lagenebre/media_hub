"""Corriger un album dans le hub : titre, année, genres, label, type de sortie et pochette, sans rien écrire dans la médiathèque.

Une vraie médiathèque d'essai (de vrais .flac), un vrai scan, les routes appelées comme le fait le navigateur ; les pistes et l'artiste ne se corrigent pas ici.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import fixtures
from tests.rig import call
from tests.test_server_routes import RouteCase
from tests.test_server_views import entry, item
from hub import accounts, scan as scanning
from hub.db import make_id
from hub.media import images

LIBRARY = "lib"
JPEG = b"\xff\xd8\xff\xe0" + b"poster" * 10
ALBUMS = (  # dossier, titre, artiste, année, genre, type, label
    ("Daft Punk/2001 - Discovery", "Discovery", "Daft Punk", "2001", "Electronic", "album", "Virgin"),
    ("Daft Punk/2001 - Homework", "Homework", "Daft Punk", "2001", "Electronic", "album", ""),
    ("Air/1998 - Sexy Boy", "Sexy Boy", "Air", "1998", "Ambient", "single", "Édition Source"),
)


def build_albums(root):
    """Trois albums d'une piste, étiquetés ; la pochette de Discovery est un cover.png."""
    for folder, title, artist, year, genre, kind, label in ALBUMS:
        path = fixtures.write_flac(root / folder / f"01 - {title}.flac", title, title, artist, 1, 1, year=year, genre=genre)
        fixtures.add_tags(path, ("RELEASETYPE", kind), *([("LABEL", label)] if label else []))
    (root / "Daft Punk" / "2001 - Discovery" / "cover.png").write_bytes(fixtures.PNG)


def snapshot(root):
    """Tout ce qui est écrit sous `root` : chemins et contenus."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@unittest.skipUnless(fixtures.FFMPEG, "ffmpeg absent : pas de vrais .flac")
class AlbumCase(RouteCase):
    kind = "music"

    @classmethod
    def setUpClass(cls):
        cls.source = Path(tempfile.mkdtemp())
        cls.addClassCleanup(shutil.rmtree, cls.source, True)
        build_albums(cls.source)

    def setUp(self):
        super().setUp()
        shutil.copytree(self.source, self.lib.path, dirs_exist_ok=True)
        self.admin = self.hub.cookie(accounts.create(self.db, "root", "motdepasse12"))
        self.bob = self.hub.cookie(accounts.create(self.db, "bob", "motdepasse12"))
        self.hub.images = images.Images(self.hub.cfg, self.db)
        self.scan()
        self.disco, self.homework, self.sexy = (make_id(LIBRARY, folder) for folder, *_ in ALBUMS)

    def scan(self):
        return scanning.scan_library(self.db, self.lib, log=lambda _m: None)

    def edit(self, ident, body, headers=None, status=200):
        """La réponse de la correction, qui doit avoir ce `status` : un test qui corrige pour en vérifier la suite ne se contente pas d'un refus muet."""
        reply = self.post(f"/api/item/{ident}/edit", body, headers=headers or self.admin)
        self.assertEqual(reply.status, status, reply.body)
        return reply

    def sheet(self, ident, headers=None):
        return self.get(f"/api/item/{ident}", headers=headers or self.admin).json()

    def listing(self, query="", headers=None):
        return self.get("/api/library/lib?" + query, headers=headers or self.admin).json()

    def titles(self, query=""):
        return [card["title"] for card in self.listing(query)["items"]]

    def row(self, ident):
        return dict(self.db.one("SELECT * FROM items WHERE id=?", (ident,)))


class TestEachFieldShowsEverywhere(AlbumCase):
    def test_a_title_shows_on_the_sheet_the_list_the_search_and_the_sort(self):
        self.assertEqual(self.titles(), ["Sexy Boy", "Discovery", "Homework"])             # par artiste, puis année, puis titre
        reply = self.edit(self.disco, {"title": "Zèbre"})
        self.assertEqual((reply.status, reply.json()), (200, {"ok": True, "edited": ["title"]}))
        self.assertEqual(self.sheet(self.disco)["title"], "Zèbre")
        self.assertEqual(self.titles(), ["Sexy Boy", "Homework", "Zèbre"])                   # le tri suit le nouveau titre
        self.assertEqual(self.titles("q=zebre"), ["Zèbre"])                                  # la recherche, sans accent
        self.assertEqual(self.titles("q=discovery"), [])                                     # l'ancien titre ne trouve plus rien
        self.assertEqual(self.row(self.disco)["letter"], "Z")

    def test_a_year_shows_on_the_sheet_the_list_and_the_sort(self):
        self.assertEqual(self.edit(self.disco, {"year": 1999}).json()["edited"], ["year"])
        self.assertEqual(self.sheet(self.disco)["year"], "1999")
        self.assertEqual([c["year"] for c in self.listing()["items"] if c["id"] == self.disco], ["1999"])
        self.assertEqual(self.titles(), ["Sexy Boy", "Discovery", "Homework"])
        self.edit(self.disco, {"year": "2010"})
        self.assertEqual(self.titles(), ["Sexy Boy", "Homework", "Discovery"])               # chez un même artiste, du plus ancien au plus récent

    def test_genres_show_on_the_sheet_the_list_and_the_genre_filter(self):
        self.assertEqual(self.titles("genre=Rock"), [])
        self.assertEqual(self.edit(self.disco, {"genres": ["Rock", "Pop"]}).json()["edited"], ["genres"])
        self.assertEqual(self.sheet(self.disco)["genres"], ["Rock", "Pop"])
        self.assertEqual(self.titles("genre=Rock"), ["Discovery"])
        self.assertEqual(self.titles("genre=Electronic"), ["Homework"])
        menu = [name for name, _ in self.listing()["genres"]]
        self.assertEqual((("Rock" in menu), ("Pop" in menu)), (True, True))

    def test_a_label_shows_on_the_sheet(self):
        self.assertEqual(self.sheet(self.disco)["label"], "Virgin")
        self.assertEqual(self.edit(self.disco, {"label": "Parlophone"}).json()["edited"], ["label"])
        self.assertEqual(self.sheet(self.disco)["label"], "Parlophone")

    def test_an_album_can_be_given_a_label_it_never_had(self):
        self.assertNotIn("label", self.sheet(self.homework))
        self.edit(self.homework, {"label": "Virgin"})
        self.assertEqual(self.sheet(self.homework)["label"], "Virgin")
        self.edit(self.homework, {"label": None})
        self.assertNotIn("label", self.sheet(self.homework))

    def test_the_main_release_type_is_the_first_of_release_types_and_changes_the_counters(self):
        before = self.listing()
        self.assertEqual((before["albums"], before["eps"], before["singles"]), (2, 0, 1))
        self.assertEqual(self.sheet(self.disco)["release_types"], ["album"])
        self.assertEqual(self.edit(self.disco, {"type": "ep"}).json()["edited"], ["type"])
        after = self.listing()
        self.assertEqual((after["albums"], after["eps"], after["singles"]), (1, 1, 1))
        self.assertEqual(self.sheet(self.disco)["release_types"], ["ep"])

    def test_a_type_replaces_the_main_one_and_the_secondary_types_of_the_files_stay(self):
        self.db.execute("UPDATE items SET meta=json_set(meta, '$.release_types', json('[\"album\",\"live\"]')) WHERE id=?", (self.disco,))
        self.edit(self.disco, {"type": "ep"})
        self.assertEqual(self.sheet(self.disco)["release_types"], ["ep", "live"])
        self.edit(self.disco, {"type": "live"})
        self.assertEqual(self.sheet(self.disco)["release_types"], ["live"])                  # une fois chacun
        self.edit(self.disco, {"type": None})
        self.assertEqual(self.sheet(self.disco)["release_types"], ["album", "live"])

    def test_several_fields_at_once_and_one_correction_never_touches_another(self):
        self.edit(self.disco, {"title": "Nouveau", "genres": ["Rock"]})
        reply = self.edit(self.disco, {"year": 2000})
        self.assertEqual(reply.json()["edited"], ["title", "year", "genres"])
        sheet = self.sheet(self.disco)
        self.assertEqual((sheet["title"], sheet["year"], sheet["genres"]), ("Nouveau", "2000", ["Rock"]))
        self.assertEqual(self.edit(self.homework, {"year": 1990}).json()["edited"], ["year"])  # un autre album n'a que la sienne


class TestACorrectionLastsAndCanBeUndone(AlbumCase):
    def corrected(self):
        self.edit(self.disco, {"title": "Zèbre", "year": 1990, "genres": ["Rock"], "label": "Autre", "type": "ep"})

    def test_a_correction_is_kept_by_the_next_scan_even_when_nothing_changed_on_disk(self):
        self.corrected()
        before = self.row(self.disco)
        self.scan()
        self.assertEqual(self.row(self.disco), before)
        self.assertEqual(self.sheet(self.disco)["edited"], ["title", "year", "genres", "label", "type"])

    def test_a_correction_survives_a_scan_that_rereads_the_files(self):
        self.corrected()
        before = self.row(self.disco)
        self.db.execute("UPDATE entries SET size=0 WHERE item_id=?", (self.disco,))          # le fichier a l'air d'avoir changé : ses étiquettes sont relues
        self.scan()
        self.assertEqual(self.row(self.disco), before)

    def test_the_correction_applies_to_an_album_scanned_after_it_was_corrected(self):
        self.corrected()
        self.db.execute("DELETE FROM items WHERE lib=?", (LIBRARY,))
        self.db.execute("DELETE FROM entries WHERE lib=?", (LIBRARY,))
        self.scan()
        self.assertEqual(self.sheet(self.disco)["title"], "Zèbre")

    def test_resetting_a_field_gives_back_the_tag_without_waiting_for_a_scan(self):
        original = self.row(self.disco)
        self.corrected()
        reply = self.edit(self.disco, {"title": None})
        self.assertEqual(reply.json()["edited"], ["year", "genres", "label", "type"])
        row = self.row(self.disco)
        self.assertEqual((row["title"], row["sort"], row["letter"]), (original["title"], original["sort"], original["letter"]))
        self.assertEqual(self.sheet(self.disco)["year"], "1990")                              # les autres corrections restent
        for field in ("year", "genres", "label", "type"):
            self.edit(self.disco, {field: None})
        self.assertEqual(self.row(self.disco), original)                                      # la ligne est celle que le scan a écrite, à l'octet près
        self.assertEqual(self.sheet(self.disco)["edited"], [])
        self.assertIsNone(self.db.kv_get(f"edit:{LIBRARY}:{self.disco}"))

    def test_a_restored_row_is_the_scanned_one_to_the_byte_even_with_accents(self):
        original = self.row(self.sexy)
        self.assertIn("Édition Source", original["meta"])                                     # écrit tel quel, pas en \u00c9
        self.edit(self.sexy, {"title": "Autre", "label": "Ailleurs", "type": "live"})
        self.edit(self.sexy, {"title": None, "label": None, "type": None})
        self.assertEqual(self.row(self.sexy), original)

    def test_a_reset_survives_the_next_scan(self):
        self.corrected()
        self.edit(self.disco, {"title": None, "type": None})
        self.scan()
        sheet = self.sheet(self.disco)
        self.assertEqual((sheet["title"], sheet["release_types"], sheet["year"]), ("Discovery", ["album"], "1990"))

    def test_a_correction_resets_to_the_original_even_when_the_tags_never_had_the_field(self):
        original = self.row(self.homework)
        self.edit(self.homework, {"label": "X", "type": "live"})
        self.edit(self.homework, {"label": None, "type": None})
        self.assertEqual(self.row(self.homework), original)

    def test_only_the_corrected_fields_are_kept_in_the_database(self):
        self.edit(self.disco, {"title": "Zèbre"})
        self.assertEqual(self.db.kv_get(f"edit:{LIBRARY}:{self.disco}"), {"title": "Zèbre"})
        self.edit(self.disco, {"genres": ["Rock", "Rock", " Pop "]})
        self.assertEqual(self.db.kv_get(f"edit:{LIBRARY}:{self.disco}"), {"title": "Zèbre", "genres": ["Rock", "Pop"]})

    def test_a_correction_the_database_holds_wrongly_never_breaks_a_scan(self):
        for junk in ("du texte", ["title"], {"title": 5, "year": "x"}, {"summary": "non"}, None):
            with self.subTest(junk=junk):
                self.db.kv_set(f"edit:{LIBRARY}:{self.disco}", junk)
                self.scan()
                self.assertEqual(self.sheet(self.disco)["title"], "Discovery")

    def test_a_correction_the_database_holds_in_part_is_applied_in_part(self):
        self.db.kv_set(f"edit:{LIBRARY}:{self.disco}", {"title": "Bon", "year": "abc"})
        self.scan()
        sheet = self.sheet(self.disco)
        self.assertEqual((sheet["title"], sheet["year"]), ("Bon", "2001"))

    def test_the_correction_of_an_album_that_vanished_waits_for_its_return(self):
        self.edit(self.disco, {"title": "Zèbre"})
        shutil.move(self.lib.path / "Daft Punk" / "2001 - Discovery", self.lib.path / "Daft Punk" / "2001 - Discovery ailleurs")
        self.scan()
        self.assertEqual(self.titles("q=zebre"), [])                                          # renommé, l'album est un autre : il n'a plus sa correction
        self.assertEqual(self.db.kv_get(f"edit:{LIBRARY}:{self.disco}"), {"title": "Zèbre"})
        shutil.move(self.lib.path / "Daft Punk" / "2001 - Discovery ailleurs", self.lib.path / "Daft Punk" / "2001 - Discovery")
        self.scan()
        self.assertEqual(self.titles("q=zebre"), ["Zèbre"])


class TestSavingAndScanningTakeTurns(AlbumCase):
    """L'enregistrement d'une correction et l'écriture d'une bibliothèque par le scan ne se croisent pas : les deux restent présents, la correction et ce que le scan vient de lire."""
    WAIT = 0.5          # le temps qu'un fil laisse à l'autre pour passer s'il le pouvait : un fil qui attend le verrou est encore là après

    def setUp(self):
        super().setUp()
        self.inside, self.release = threading.Event(), threading.Event()
        self.addCleanup(self.release.set)                         # un test qui échoue ne laisse pas de fil bloqué
        track = fixtures.add_tags(self.lib.path / "Daft Punk" / "2001 - Homework" / "01 - Homework.flac", ("LABEL", "Nouveau label"))
        os.utime(track, (time.time() + 60, time.time() + 60))          # le scan relit ce qui a bougé : la date change à coup sûr

    def holding(self, real):
        """Une version de `real` qui s'arrête à l'intérieur jusqu'à `release` : le fil qui l'appelle tient alors tout ce qu'il a pris."""
        def wait(*args, **kw):
            self.inside.set()
            self.assertTrue(self.release.wait(10))
            return real(*args, **kw)
        return wait

    def patch(self, owner, name):
        patch = mock.patch.object(owner, name, self.holding(getattr(owner, name)))
        patch.start()
        self.addCleanup(patch.stop)

    def thread(self, target):
        found = threading.Thread(target=target, daemon=True)
        found.start()
        return found

    def check_both_are_there(self):
        row = self.row(self.homework)
        self.assertEqual((row["title"], json.loads(row["meta"])["label"]), ("Zèbre", "Nouveau label"))
        self.assertEqual(self.db.kv_get(f"edit:{LIBRARY}:{self.homework}"), {"title": "Zèbre"})

    def test_a_scan_waits_for_a_save_that_has_read_the_row(self):
        self.patch(self.db, "rewrite_item")
        saving = self.thread(lambda: self.edit(self.homework, {"title": "Zèbre"}))
        self.assertTrue(self.inside.wait(10))                     # l'enregistrement a lu la ligne d'avant et va la réécrire
        scanning_thread = self.thread(self.scan)
        scanning_thread.join(self.WAIT)
        self.assertTrue(scanning_thread.is_alive(), "le scan a écrit la bibliothèque entre la lecture de la ligne et sa réécriture")
        self.release.set()
        saving.join(10)
        scanning_thread.join(10)
        self.check_both_are_there()

    def test_a_save_waits_for_a_scan_that_has_read_the_corrections(self):
        self.patch(self.db, "replace_library")
        scanning_thread = self.thread(self.scan)
        self.assertTrue(self.inside.wait(10))                     # le scan a lu les corrections et va écrire la bibliothèque
        saving = self.thread(lambda: self.edit(self.homework, {"title": "Zèbre"}))
        saving.join(self.WAIT)
        self.assertTrue(saving.is_alive(), "la correction est passée entre la lecture des corrections et l'écriture de la bibliothèque")
        self.release.set()
        scanning_thread.join(10)
        saving.join(10)
        self.check_both_are_there()

    def test_a_save_does_not_wait_for_the_scan_to_read_the_files(self):
        patch = mock.patch.dict(scanning.SCANNERS, music=self.holding(scanning.SCANNERS["music"]))
        patch.start()
        self.addCleanup(patch.stop)
        scanning_thread = self.thread(self.scan)
        self.assertTrue(self.inside.wait(10))                     # le scan lit encore les dossiers : il ne tient rien
        saving = self.thread(lambda: self.edit(self.homework, {"title": "Zèbre"}))
        saving.join(10)
        self.assertFalse(saving.is_alive(), "l'enregistrement attend la lecture des fichiers")
        self.release.set()
        scanning_thread.join(10)
        self.check_both_are_there()                               # lue au dernier moment, la correction n'est pas perdue


class TestACoverIsChosenInTheHub(AlbumCase):
    def cover(self, ident=None):
        found = self.hub.images.get("item", ident or self.disco)
        return found[0] if found else None

    def test_the_cover_of_the_folder_is_shown_until_one_is_chosen(self):
        self.assertEqual(self.cover(), fixtures.PNG)

    def test_a_chosen_cover_goes_under_the_data_folder_and_passes_before_cover_png(self):
        before = snapshot(self.lib.path)
        reply = self.post(f"/api/item/{self.disco}/poster", JPEG, headers=self.admin)
        self.assertEqual((reply.status, reply.json()), (200, {"ok": True}))
        self.assertEqual(self.cover(), JPEG)
        self.assertEqual(snapshot(self.lib.path), before)                                     # rien n'est écrit dans la médiathèque
        kept = list((self.hub.cfg.data / "posters").iterdir())
        self.assertEqual([p.read_bytes() for p in kept], [JPEG])
        self.assertEqual(self.sheet(self.disco)["edited"], ["poster"])

    def test_a_chosen_cover_is_shown_for_an_album_that_has_none_in_its_folder(self):
        self.post(f"/api/item/{self.homework}/poster", JPEG, headers=self.admin)
        self.assertEqual(self.cover(self.homework), JPEG)

    def test_resetting_the_cover_gives_back_cover_png_and_removes_the_file(self):
        self.post(f"/api/item/{self.disco}/poster", JPEG, headers=self.admin)
        reply = self.edit(self.disco, {"poster": None})
        self.assertEqual((reply.status, reply.json()), (200, {"ok": True, "edited": []}))
        self.assertEqual(self.cover(), fixtures.PNG)
        self.assertEqual(list((self.hub.cfg.data / "posters").iterdir()), [])
        self.assertEqual(self.sheet(self.disco)["edited"], [])

    def test_resetting_a_cover_that_was_never_chosen_changes_nothing(self):
        self.assertEqual(self.edit(self.disco, {"poster": None}).json(), {"ok": True, "edited": []})
        self.assertEqual(self.cover(), fixtures.PNG)

    def test_a_chosen_cover_survives_a_scan(self):
        self.post(f"/api/item/{self.disco}/poster", JPEG, headers=self.admin)
        self.scan()
        self.assertEqual(self.cover(), JPEG)

    def test_a_cover_is_not_a_value_to_set_through_edit(self):
        self.edit(self.disco, {"poster": "x.jpg"}, status=400)
        self.assertEqual(self.sheet(self.disco)["edited"], [])

    def test_only_the_administrator_chooses_a_cover(self):
        self.assertEqual(self.post(f"/api/item/{self.disco}/poster", JPEG, headers=self.bob).status, 403)
        self.assertEqual(list((self.hub.cfg.data / "posters").glob("*")) if (self.hub.cfg.data / "posters").exists() else [], [])


class TestTheBoundsAreEnforced(AlbumCase):
    def refused(self, body):
        reply = self.edit(self.disco, body, status=400)
        self.assertEqual(self.db.kv_get(f"edit:{LIBRARY}:{self.disco}"), None, body)        # un refus ne garde rien, même des champs valables du même envoi
        self.assertEqual(self.row(self.disco)["title"], "Discovery", body)
        return reply.json()["error"]

    def test_a_title_is_one_to_three_hundred_characters_without_control_characters(self):
        for title in ("", "   ", "x" * 301, "deux\nlignes", "tab\tulé", "nul\x00", 5, ["a"], True):
            with self.subTest(title=title):
                self.refused({"title": title})
        self.assertIn("titre", self.refused({"title": ""}))
        self.assertEqual(self.edit(self.disco, {"title": "x" * 300}).status, 200)
        self.assertEqual(self.edit(self.disco, {"title": "é"}).status, 200)

    def test_a_year_is_four_digits_between_1000_and_next_year(self):
        this = time.localtime().tm_year
        for year in ("199", "99999", "abcd", "19 9", 999, this + 2, 0, -1999, 19.5, True, ["1999"], "٢٠٢٠", ""):
            with self.subTest(year=year):
                self.refused({"year": year})
        for year in (1000, "1000", this + 1, str(this)):
            with self.subTest(year=year):
                self.assertEqual(self.edit(self.disco, {"year": year}).status, 200)
                self.assertEqual(self.sheet(self.disco)["year"], str(year))

    def test_genres_are_at_most_twenty_of_sixty_characters(self):
        for genres in ("Rock", [["Rock"]], [f"g{n}" for n in range(21)], ["x" * 61], [""], ["Ro\nck"], [5], {"a": 1}):
            with self.subTest(genres=genres):
                self.refused({"genres": genres})
        self.assertEqual(self.edit(self.disco, {"genres": [f"g{n}" for n in range(20)]}).status, 200)
        self.assertEqual(self.edit(self.disco, {"genres": ["x" * 60]}).status, 200)
        self.assertEqual(self.edit(self.disco, {"genres": []}).status, 200)
        self.assertEqual(self.sheet(self.disco)["genres"], [])                                # aucun genre est un choix

    def test_a_label_is_at_most_two_hundred_characters(self):
        for label in ("x" * 201, "a\nb", 5, ["Virgin"], True):
            with self.subTest(label=label):
                self.refused({"label": label})
        self.assertEqual(self.edit(self.disco, {"label": "x" * 200}).status, 200)

    def test_the_type_is_one_of_the_seven_known(self):
        for kind in ("mixtape", "", "ALBUM ", 5, ["ep"], True):
            with self.subTest(kind=kind):
                self.refused({"type": kind})
        for kind in ("album", "ep", "single", "compilation", "live", "soundtrack", "remix"):
            with self.subTest(kind=kind):
                self.assertEqual(self.edit(self.disco, {"type": kind}).status, 200)
                self.assertEqual(self.sheet(self.disco)["release_types"][0], kind)

    def test_what_is_not_a_field_of_an_album_is_refused(self):
        for body in ({"artist": "Autre"}, {"tracks": []}, {"summary": "Un texte"}, {"id": "x"}, {"cover": "x.jpg"}, {}, {"title": "Bon", "artist": "Mauvais"}):
            with self.subTest(body=body):
                self.refused(body)
        self.edit(self.disco, ["title"], status=400)
        self.assertEqual(self.post(f"/api/item/{self.disco}/edit", b"du bruit", headers=self.admin).status, 400)

    def test_a_valid_field_does_not_excuse_an_invalid_one(self):
        self.refused({"title": "Bon", "year": 5})

    def test_a_title_with_invisible_marks_is_kept_without_them_like_a_scanned_one(self):
        self.edit(self.disco, {"title": "\u200bZèbre\u200e"})
        self.assertEqual(self.sheet(self.disco)["title"], "Zèbre")
        self.edit(self.disco, {"title": None})
        self.refused({"title": "\u200b\u200e"})                                              # rien ne reste une fois les marques retirées


class TestWhoMayEdit(AlbumCase):
    def test_an_ordinary_account_is_refused_and_nothing_changes(self):
        before = self.row(self.disco)
        self.edit(self.disco, {"title": "Zèbre"}, headers=self.bob, status=403)
        self.assertEqual((self.row(self.disco), self.db.kv_get(f"edit:{LIBRARY}:{self.disco}")), (before, None))

    def test_a_request_without_a_session_is_refused(self):
        self.assertEqual(call(self.hub, "POST", f"/api/item/{self.disco}/edit", {"title": "Zèbre"})[1].status, 401)
        self.assertEqual(self.row(self.disco)["title"], "Discovery")

    def test_a_bearer_token_that_is_not_the_account_s_is_refused(self):
        self.edit(self.disco, {"title": "Zèbre"}, headers={"Authorization": "Bearer faux"}, status=401)

    def test_an_unknown_album_is_a_404(self):
        self.edit("inconnu", {"title": "Zèbre"}, status=404)

    def test_the_corrections_show_to_every_account_but_only_the_administrator_is_told_what_was_corrected(self):
        self.edit(self.disco, {"title": "Zèbre", "type": "ep"})
        self.post(f"/api/item/{self.disco}/poster", JPEG, headers=self.admin)
        admin, bob = self.sheet(self.disco), self.sheet(self.disco, self.bob)
        self.assertEqual(admin["edited"], ["title", "type", "poster"])
        self.assertNotIn("edited", bob)
        self.assertEqual((bob["title"], bob["release_types"]), ("Zèbre", ["ep"]))
        self.assertEqual(self.titles("q=zebre"), ["Zèbre"])

    def test_the_sheet_of_an_album_without_correction_tells_the_administrator_so(self):
        self.assertEqual(self.sheet(self.disco)["edited"], [])

    def test_the_original_values_are_never_in_the_sheet(self):
        self.edit(self.disco, {"title": "Zèbre", "type": "ep"})
        for headers in (self.admin, self.bob):
            self.assertNotIn("original", self.sheet(self.disco, headers))


class TestOnlyAlbumsAreEdited(RouteCase):
    def setUp(self):
        super().setUp()
        self.fill([item("m1", "movie", title="Film"), item("s1", "show", title="Serie"), item("b1", "series", title="Livres")], [entry("v1", "m1")])
        self.admin = self.hub.cookie(accounts.create(self.db, "root", "motdepasse12"))

    def test_a_film_a_show_and_a_book_series_are_refused_with_a_400(self):
        for ident in ("m1", "s1", "b1"):
            with self.subTest(item=ident):
                reply = self.post(f"/api/item/{ident}/edit", {"title": "Autre"}, headers=self.admin)
                self.assertEqual((reply.status, "album" in reply.json()["error"]), (400, True))
                self.assertIsNone(self.db.kv_get(f"edit:lib:{ident}"))
        self.assertEqual(self.db.one("SELECT title FROM items WHERE id='m1'")["title"], "Film")

    def test_the_sheet_of_a_film_has_no_edited(self):
        self.assertNotIn("edited", self.get("/api/item/m1", headers=self.admin).json())
