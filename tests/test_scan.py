"""Le scan des bibliothèques : ce qu'on range dans la base, ce qu'on relit ou reprend du scan précédent, et les lignes qu'on en tire."""

from __future__ import annotations

import json
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests import fixtures
from hub import config

fixtures.REPO  # noqa: B018  (rend `libraries` importable)
config.use_media_library(fixtures.REPO)

from hub import scan as scanning  # noqa: E402
from hub.db import Database, make_id  # noqa: E402
from hub.scan import books, common, music  # noqa: E402


class TempCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)


def known_of(entries):
    return {e["rel"]: e for e in entries}


class TestScanBooks(TempCase):
    def setUp(self):
        super().setUp()
        fixtures.write_cbz(self.root / "Manga" / "Naruto" / "Tome 01.cbz", Series="Naruto", Number="1", Count="3", Writer="Kishimoto", Summary="Un ninja.", Genre="Action", Year="1999", Publisher="Kana")
        fixtures.write_cbz(self.root / "Manga" / "Naruto" / "Tome 03.cbz", Series="Naruto", Number="3", Count="3", Year="2001")
        fixtures.write_epub(self.root / "Romans" / "Dumas" / "Monte-Cristo.epub", "Le Comte", "Dumas")
        self.lib = config.Library("livres", "Livres", "books", self.root)

    def scan(self, known=None):
        items, entries, counts = books.scan_books(self.lib, known or {})
        return {i["title"]: i for i in items}, {e["rel"]: e for e in entries}, counts

    def test_a_series_is_an_item_with_what_its_files_say(self):
        items, _, _ = self.scan()
        naruto = items["Naruto"]
        meta = json.loads(naruto["meta"])
        self.assertEqual((naruto["type"], naruto["year"], naruto["summary"], naruto["grp"], naruto["count"]), ("series", "1999", "Un ninja.", "Manga", 2))
        self.assertEqual(json.loads(naruto["genres"]), ["Action"])
        self.assertEqual((meta["authors"], meta["publisher"], meta["tagged"], meta["years"]), (["Kishimoto"], "Kana", 2, [1999, 2001]))
        self.assertEqual((meta["unit"], meta["units"]), ("tome", "tomes"))
        self.assertEqual(meta["missing"], [{"label": "Naruto", "numbers": [2], "total": 3}])

    def test_a_novel_has_other_words_for_its_unit_and_no_year(self):
        item = self.scan()[0]["Dumas"]
        meta = json.loads(item["meta"])
        self.assertEqual((item["year"], meta["years"], meta["unit"], meta["units"], meta["missing"]), ("", [], "livre", "livres", []))

    def test_every_file_is_an_entry_of_its_series(self):
        items, entries, _ = self.scan()
        first = entries["Manga/Naruto/Tome 01.cbz"]
        self.assertEqual((first["kind"], first["number"], first["title"], first["summary"], first["item_id"]), ("book", 1.0, "Tome 01", "Un ninja.", items["Naruto"]["id"]))
        self.assertEqual(first["id"], make_id("livres", "Manga/Naruto/Tome 01.cbz"))
        self.assertEqual(json.loads(first["meta"]), {"info": json.loads(first["meta"])["info"], "error": "", "format": "cbz", "sub": "", "label": "1"})
        self.assertEqual(json.loads(first["meta"])["info"]["Writer"], "Kishimoto")
        self.assertEqual(entries["Manga/Naruto/Tome 03.cbz"]["summary"], "")
        novel = entries["Romans/Dumas/Monte-Cristo.epub"]
        self.assertIsNone(novel["number"])
        self.assertEqual((json.loads(novel["meta"])["label"], json.loads(novel["meta"])["error"]), ("", ""))

    def test_the_first_scan_reads_everything(self):
        counts = self.scan()[2]
        self.assertEqual((counts.read, counts.reused), (3, 0))

    def test_a_file_that_did_not_change_is_not_read_again(self):
        _, entries, _ = self.scan()
        items, again, counts = self.scan(known_of(entries.values()))
        self.assertEqual((counts.read, counts.reused), (0, 3))
        self.assertEqual(json.loads(again["Manga/Naruto/Tome 01.cbz"]["meta"]), json.loads(entries["Manga/Naruto/Tome 01.cbz"]["meta"]))

    def test_a_file_that_changed_is_read_again_and_the_others_are_not(self):
        _, entries, _ = self.scan()
        known = known_of(entries.values())
        known["Manga/Naruto/Tome 01.cbz"] = {**known["Manga/Naruto/Tome 01.cbz"], "size": 1}
        counts = self.scan(known)[2]
        self.assertEqual((counts.read, counts.reused), (1, 2))

    def test_a_reading_that_failed_is_done_again(self):
        _, entries, _ = self.scan()
        known = known_of(entries.values())
        meta = json.loads(known["Manga/Naruto/Tome 03.cbz"]["meta"])
        known["Manga/Naruto/Tome 03.cbz"] = {**known["Manga/Naruto/Tome 03.cbz"], "meta": json.dumps({**meta, "error": "archive illisible"})}
        known["Romans/Dumas/Monte-Cristo.epub"] = {**known["Romans/Dumas/Monte-Cristo.epub"], "meta": json.dumps({"format": "epub"})}       # sans lecture du tout
        counts = self.scan(known)[2]
        self.assertEqual((counts.read, counts.reused), (2, 1))


class TestScanBooksStack(TempCase):
    """Une série de 2 tomes ou plus montre jusqu'à STACK_MAX couvertures empilées : sa fiche garde les entrées des tomes 2 à STACK_MAX."""

    def series(self, volumes, name="Serie"):
        for n in range(1, volumes + 1):
            fixtures.write_cbz(self.root / "Manga" / name / f"Tome {n:02d}.cbz", Series=name, Number=str(n))
        return config.Library("livres", "Livres", "books", self.root)

    def stack(self, volumes):
        """(la pile de la série de `volumes` tomes, les identifiants de ses tomes dans l'ordre)."""
        lib = self.series(volumes, name=f"Serie {volumes}")
        items, entries, _ = books.scan_books(lib, {})
        item = next(i for i in items if i["title"] == f"Serie {volumes}")
        return json.loads(item["meta"]).get("stack"), [e["id"] for e in sorted(entries, key=lambda e: e["number"]) if e["item_id"] == item["id"]]

    def test_a_single_volume_has_nothing_to_stack(self):
        self.assertIsNone(self.stack(1)[0])

    def test_the_stack_holds_the_entries_of_volumes_two_to_the_maximum_in_order(self):
        for volumes, kept in ((2, 1), (3, 2), (5, 2)):
            with self.subTest(volumes=volumes):
                stack, ids = self.stack(volumes)
                self.assertEqual(stack, ids[1:1 + kept])
                self.assertEqual(len(stack), kept)

    def test_the_stack_follows_the_volume_numbers_not_the_order_of_the_folders(self):
        """Des tomes 3 et 4 à la racine et des tomes 1 et 2 dans un sous-dossier : le scan range le sous-dossier après, la fiche et la pile suivent les numéros."""
        for rel, number in (("Tome 03.cbz", "3"), ("Tome 04.cbz", "4"), ("Bonus/Tome 01.cbz", "1"), ("Bonus/Tome 02.cbz", "2")):
            fixtures.write_cbz(self.root / "Manga" / "Mêlée" / rel, Series="Mêlée", Number=number)
        items, entries, _ = books.scan_books(config.Library("livres", "Livres", "books", self.root), {})
        item = next(i for i in items if i["title"] == "Mêlée")
        by_number = [e["id"] for e in sorted(entries, key=lambda e: e["number"]) if e["item_id"] == item["id"]]
        self.assertEqual(json.loads(item["meta"])["stack"], by_number[1:common.STACK_MAX])

    def test_a_volume_added_after_a_scan_joins_the_stack_at_the_next_scan(self):
        lib = self.series(2)
        db = Database(self.root / "hub.db")
        scanning.scan_library(db, lib, log=lambda _m: None)
        before = json.loads(db.one("SELECT meta FROM items")["meta"])["stack"]
        fixtures.write_cbz(self.root / "Manga" / "Serie" / "Tome 03.cbz", Series="Serie", Number="3")
        scanning.scan_library(db, lib, log=lambda _m: None)
        after = json.loads(db.one("SELECT meta FROM items")["meta"])["stack"]
        self.assertEqual((len(before), len(after), after[:1]), (1, 2, before))


class TestScanMusic(TempCase):
    def setUp(self):
        super().setUp()
        fixtures.write_flac(self.root / "Daft" / "2001 - Disco" / "01 - A.flac", "A", "Disco", "Daft", 1, 2)
        fixtures.write_flac(self.root / "Daft" / "2001 - Disco" / "02 - B.flac", "B", "Disco", "Daft", 2, 2)
        fixtures.write_flac(self.root / "01 - Solo.flac", "Solo", "Single", "Seul", 1, 1)
        (self.root / "Daft" / "2001 - Disco" / "cover.png").write_bytes(fixtures.PNG)
        self.lib = config.Library("musique", "Musique", "music", self.root)

    def scan(self, known=None):
        items, entries, counts = music.scan_music(self.lib, known or {})
        return {i["title"]: i for i in items}, {e["rel"]: e for e in entries}, counts

    def test_an_album_is_an_item_identified_by_its_folder(self):
        items, _, _ = self.scan()
        disco = items["Disco"]
        meta = json.loads(disco["meta"])
        self.assertEqual(disco["id"], make_id("musique", "Daft/2001 - Disco"))
        self.assertEqual((disco["type"], disco["year"], disco["grp"], disco["count"]), ("album", "2001", "Daft", 2))
        self.assertEqual((meta["artist"], meta["folder"], meta["artist_dir"], meta["discs"], meta["duration"]), ("Daft", "Daft/2001 - Disco", "Daft", 1, 2.0))
        self.assertEqual((meta["has_cover"], meta["cover_file"]), (True, "cover.png"))

    def test_the_artist_folder_is_found_when_its_name_differs_from_the_tag(self):
        fixtures.write_flac(self.root / "AC_DC" / "1980 - Back in Black" / "01 - Hells Bells.flac", "Hells Bells", "Back in Black", "AC/DC", 1, 1)
        meta = json.loads(self.scan()[0]["Back in Black"]["meta"])
        self.assertEqual((meta["artist"], meta["artist_dir"]), ("AC/DC", "AC_DC"))

    def test_an_album_at_the_root_is_identified_by_a_dot(self):
        solo = self.scan()[0]["Single"]
        self.assertEqual(solo["id"], make_id("musique", "."))
        meta = json.loads(solo["meta"])
        self.assertEqual((meta["folder"], meta["artist_dir"], meta["has_cover"]), ("", "", False))

    def test_every_track_is_an_entry_with_its_tags(self):
        items, entries, _ = self.scan()
        track = entries["Daft/2001 - Disco/02 - B.flac"]
        self.assertEqual((track["kind"], track["title"], track["number"], track["duration"], track["item_id"]), ("audio", "B", 2, 1.0, items["Disco"]["id"]))
        meta = json.loads(track["meta"])
        self.assertEqual((meta["error"], meta["format"], meta["info"]["title"]), ("", "flac", "B"))

    def test_the_wanted_albums_are_kept_apart(self):
        self.assertEqual(self.scan()[2].kv, {"wanted:musique": {}})

    def test_the_first_scan_reads_every_track_and_the_next_reuses_them(self):
        _, entries, counts = self.scan()
        self.assertEqual((counts.read, counts.reused), (3, 0))
        counts = self.scan(known_of(entries.values()))[2]
        self.assertEqual((counts.read, counts.reused), (0, 3))

    def test_a_track_that_changed_is_read_again(self):
        _, entries, _ = self.scan()
        known = known_of(entries.values())
        known["01 - Solo.flac"] = {**known["01 - Solo.flac"], "mtime": 1}
        counts = self.scan(known)[2]
        self.assertEqual((counts.read, counts.reused), (1, 2))

    def test_a_track_without_tags_kept_is_read_again(self):
        _, entries, _ = self.scan()
        known = known_of(entries.values())
        known["01 - Solo.flac"] = {**known["01 - Solo.flac"], "meta": json.dumps({"info": None})}
        counts = self.scan(known)[2]
        self.assertEqual((counts.read, counts.reused), (1, 2))

    def credited(self, name, artists, ids, album_ids, artist="Daft"):
        """Un album « Credits/<name> » dont l'unique piste porte ARTISTS, MUSICBRAINZ_ARTISTID et MUSICBRAINZ_ALBUMARTISTID."""
        path = fixtures.write_flac(self.root / "Credits" / name / "01 - T.flac", "T", name, artist, 1, 1)
        fixtures.add_tags(path, *[("ARTISTS", a) for a in artists], *[("MUSICBRAINZ_ARTISTID", i) for i in ids], *[("MUSICBRAINZ_ALBUMARTISTID", i) for i in album_ids])
        return self.scan()

    def test_a_track_keeps_its_artists_with_their_identifiers(self):
        items, entries, _ = self.credited("Feat", ["Daft", "Poppy"], ["id-daft", "id-poppy"], ["id-daft"], artist="Daft feat. Poppy")
        info = json.loads(entries["Credits/Feat/01 - T.flac"]["meta"])["info"]
        self.assertEqual(info["artists"], [{"name": "Daft", "id": "id-daft"}, {"name": "Poppy", "id": "id-poppy"}])
        self.assertEqual(info["albumartist_ids"], ["id-daft"])
        self.assertEqual(info["artist"], "Daft feat. Poppy")                                   # le tag ARTIST reste tel quel : la file et les playlists le montrent en entier
        self.assertEqual(json.loads(items["Feat"]["meta"])["artist_mbids"], ["id-daft"])      # l'album garde les identifiants de son artiste

    def test_names_and_identifiers_of_different_lengths_are_not_paired(self):
        _, entries, _ = self.credited("Court", ["Daft", "Poppy"], ["id-daft"], ["id-daft"])
        self.assertEqual(json.loads(entries["Credits/Court/01 - T.flac"]["meta"])["info"]["artists"], [{"name": "Daft", "id": ""}, {"name": "Poppy", "id": ""}])

    def test_a_track_without_artists_has_an_empty_list(self):
        _, entries, _ = self.scan()
        info = json.loads(entries["01 - Solo.flac"]["meta"])["info"]
        self.assertEqual((info["artists"], info["albumartist_ids"]), ([], []))

    def test_a_track_kept_from_before_the_artists_were_read_is_read_again(self):
        _, entries, _ = self.scan()
        known = known_of(entries.values())
        meta = json.loads(known["01 - Solo.flac"]["meta"])
        del meta["info"]["artists"]
        known["01 - Solo.flac"] = {**known["01 - Solo.flac"], "meta": json.dumps(meta)}
        counts = self.scan(known)[2]
        self.assertEqual((counts.read, counts.reused), (1, 2))

    def boxed(self, name, *discs):
        """Un album « Box/<name> » : `discs` donne, pour chaque piste, `(numéro de disque, DISCSUBTITLE)` (le titre `None` n'écrit pas le tag) ; rend items et entrées du scan."""
        for n, (disc, title) in enumerate(discs, 1):
            path = fixtures.write_flac(self.root / "Box" / name / f"{n:02d} - T.flac", f"T{n}", name, "Daft", n, len(discs))
            fixtures.add_tags(path, ("DISCNUMBER", str(disc)), *([("DISCSUBTITLE", title)] if title is not None else []))
        return self.scan()

    def disc_titles(self, name, *discs):
        items, _, _ = self.boxed(name, *discs)
        return json.loads(items[name]["meta"]).get("disc_titles", {})

    def test_the_title_of_a_disc_is_kept_with_the_album_and_with_each_track(self):
        items, entries, _ = self.boxed("Deluxe", (1, None), (1, None), (2, "Bonus"))
        self.assertEqual(json.loads(items["Deluxe"]["meta"])["disc_titles"], {"2": "Bonus"})
        self.assertEqual([json.loads(entries[f"Box/Deluxe/{n:02d} - T.flac"]["meta"])["info"]["disc_title"] for n in (1, 2, 3)], ["", "", "Bonus"])

    def test_a_title_that_only_repeats_the_number_of_the_disc_is_not_kept(self):
        for repeat in ("CD 2", "cd2", "Disc 2", "Disque 02", "Disk 2"):
            with self.subTest(repeat):
                self.assertEqual(self.disc_titles("Repete", (1, None), (2, repeat)), {})
        self.assertEqual(self.disc_titles("Autre", (1, None), (2, "Disc 3")), {"2": "Disc 3"})           # un autre numéro n'est pas une répétition : on ne devine pas
        self.assertEqual(self.disc_titles("Live", (1, None), (2, "Disc 2: Live à Paris")), {"2": "Disc 2: Live à Paris"})

    def test_an_album_of_one_disc_keeps_no_disc_title(self):
        self.assertEqual(self.disc_titles("Seul", (1, "Live à Paris"), (1, "Live à Paris")), {})

    def test_an_album_without_disc_titles_has_none(self):
        items, entries, _ = self.boxed("Nu", (1, None), (2, None))
        self.assertNotIn("disc_titles", json.loads(items["Nu"]["meta"]))
        self.assertEqual(json.loads(entries["Box/Nu/01 - T.flac"]["meta"])["info"]["disc_title"], "")

    def test_a_disc_whose_tracks_disagree_takes_the_most_common_title(self):
        self.assertEqual(self.disc_titles("Melange", (1, None), (2, "Bonus"), (2, "Bonus"), (2, "Inédits")), {"2": "Bonus"})
        self.assertEqual(self.disc_titles("Trous", (1, None), (2, None), (2, "Bonus"), (2, None)), {"2": "Bonus"})         # une piste sans titre ne compte pas contre les autres
        self.assertEqual(self.disc_titles("Egalite", (1, None), (2, "Inédits"), (2, "Bonus")), {"2": "Inédits"})            # à égalité, la première piste du disque

    def test_a_title_is_cleaned_of_its_spaces(self):
        self.assertEqual(self.disc_titles("Blancs", (1, None), (2, "  Bonus ")), {"2": "Bonus"})

    def test_a_track_kept_from_before_the_disc_titles_were_read_is_read_again(self):
        _, entries, _ = self.boxed("Ancien", (1, None), (2, "Bonus"))
        known = known_of(entries.values())
        meta = json.loads(known["Box/Ancien/02 - T.flac"]["meta"])
        del meta["info"]["disc_title"]
        known["Box/Ancien/02 - T.flac"] = {**known["Box/Ancien/02 - T.flac"], "meta": json.dumps(meta)}
        items, entries, counts = self.scan(known)
        self.assertEqual(counts.read, 1)
        self.assertEqual(json.loads(items["Ancien"]["meta"])["disc_titles"], {"2": "Bonus"})

    def typed(self, name, *tracks):
        """Un album « Types/<name> » : `tracks` donne, pour chaque piste, ses valeurs de RELEASETYPE (une clé répétée par type) ; rend items et entrées du scan."""
        for n, types in enumerate(tracks, 1):
            path = fixtures.write_flac(self.root / "Types" / name / f"{n:02d} - T.flac", f"T{n}", name, "Daft", n, len(tracks))
            fixtures.add_tags(path, *[("RELEASETYPE", t) for t in types])
        return self.scan()

    def release_types(self, name, *tracks):
        return json.loads(self.typed(name, *tracks)[0][name]["meta"]).get("release_types")

    def test_the_release_types_of_a_track_are_kept_with_the_album_and_with_each_track(self):
        items, entries, _ = self.typed("Concert", ("album", "live"), ("album", "live"))
        self.assertEqual(json.loads(items["Concert"]["meta"])["release_types"], ["album", "live"])
        self.assertEqual(json.loads(entries["Types/Concert/01 - T.flac"]["meta"])["info"]["release_types"], ["album", "live"])

    def test_the_release_types_are_lowercase_and_without_spaces_or_blanks(self):
        self.assertEqual(self.release_types("Casse", (" Album ", "", "LIVE")), ["album", "live"])

    def test_an_album_without_release_type_has_none(self):
        items, entries, _ = self.typed("Sans", (), ())
        self.assertNotIn("release_types", json.loads(items["Sans"]["meta"]))
        self.assertEqual(json.loads(entries["Types/Sans/01 - T.flac"]["meta"])["info"]["release_types"], [])

    def test_tracks_that_disagree_give_the_most_common_release_types_and_ignore_the_untagged(self):
        self.assertEqual(self.release_types("Divers", ("single",), ("ep",), ("ep",), ()), ["ep"])

    def test_a_track_kept_from_before_the_release_types_were_read_is_read_again(self):
        _, entries, _ = self.typed("Vieux", ("album",), ("album",))
        known = known_of(entries.values())
        meta = json.loads(known["Types/Vieux/02 - T.flac"]["meta"])
        del meta["info"]["release_types"]
        known["Types/Vieux/02 - T.flac"] = {**known["Types/Vieux/02 - T.flac"], "meta": json.dumps(meta)}
        items, _, counts = self.scan(known)
        self.assertEqual(counts.read, 1)
        self.assertEqual(json.loads(items["Vieux"]["meta"])["release_types"], ["album"])

    def reissued(self, name, *tags):
        """Un album « Reedition/<name> » d'une piste éditée en 2016 (DATE) ; `tags` ajoute `(clé, valeur)` (ORIGINALYEAR, ORIGINALDATE)."""
        path = fixtures.write_flac(self.root / "Reedition" / name / "01 - T.flac", "T", name, "Daft", 1, 1, year="2016-03-25")
        fixtures.add_tags(path, *tags)
        return self.scan()

    def test_the_year_of_an_album_is_its_first_release_not_its_edition(self):
        for name, tags in (("Annee", [("ORIGINALYEAR", "1970")]), ("Date", [("ORIGINALDATE", "1970-05-08")]), ("Aucune", [])):
            with self.subTest(name):
                items, _, _ = self.reissued(name, *tags)
                self.assertEqual(items[name]["year"], "2016" if name == "Aucune" else "1970")

    def test_a_track_kept_from_before_the_date_was_read_is_read_again(self):
        _, entries, _ = self.reissued("Ancienne", ("ORIGINALYEAR", "1970"))
        known = known_of(entries.values())
        meta = json.loads(known["Reedition/Ancienne/01 - T.flac"]["meta"])
        del meta["info"]["date"]
        meta["info"]["year"] = "2016"                                                          # l'année d'avant : celle de la date d'édition
        known["Reedition/Ancienne/01 - T.flac"] = {**known["Reedition/Ancienne/01 - T.flac"], "meta": json.dumps(meta)}
        items, _, counts = self.scan(known)
        self.assertEqual(counts.read, 1)
        self.assertEqual(items["Ancienne"]["year"], "1970")


class TestArtistDirs(unittest.TestCase):
    @staticmethod
    def releases(*pairs):
        return [types.SimpleNamespace(rel=rel, artist_id=artist) for rel, artist in pairs]

    def test_an_artist_lives_in_the_folder_that_holds_most_of_their_albums(self):
        found = music.artist_dirs(self.releases(("Rock/a", "z"), ("Rock/b", "z"), ("Pop/c", "z")))
        self.assertEqual(found, {"z": "Rock"})

    def test_a_folder_shared_without_majority_belongs_to_nobody(self):
        self.assertEqual(music.artist_dirs(self.releases(("Divers/a", "x"), ("Divers/b", "y"))), {})

    def test_an_album_at_the_root_has_no_artist_folder(self):
        self.assertEqual(music.artist_dirs(self.releases(("a", "x"))), {})

    def test_with_as_many_albums_in_two_folders_the_first_in_name_order_wins(self):
        self.assertEqual(music.artist_dirs(self.releases(("B/a", "z"), ("A/b", "z"))), {"z": "A"})


class TestRows(unittest.TestCase):
    def test_an_item_row_has_nothing_unset(self):
        row = common.item_row("lib", "key", "movie", "Le Titre")
        self.assertEqual(row, {"id": make_id("lib", "key"), "lib": "lib", "type": "movie", "title": "Le Titre", "sort": "le titre", "letter": "L", "year": "", "summary": "",
                               "genres": "[]", "grp": "", "meta": "{}", "added": 0, "count": 0})

    def test_an_item_row_keeps_what_it_is_given(self):
        row = common.item_row("lib", "key", "movie", "Éa 10", year=2010, summary="s", genres=("Action", "Été"), grp="g", meta={"é": 1}, added=5.9, count=3)
        self.assertEqual((row["year"], row["summary"], row["genres"], row["grp"], row["meta"], row["added"], row["count"]), ("2010", "s", '["Action", "Été"]', "g", '{"é": 1}', 5, 3))
        self.assertEqual(common.item_row("l", "k", "t", "x", year=None, summary=None, grp=None, added=None, count=None)["year"], "")

    def test_an_entry_row_has_nothing_unset(self):
        row = common.entry_row("lib", "item", "a/b.mkv", "movie")
        self.assertEqual(row, {"id": make_id("lib", "a/b.mkv"), "item_id": "item", "lib": "lib", "rel": "a/b.mkv", "kind": "movie", "season": None, "disc": None, "number": None,
                               "title": "", "summary": "", "duration": 0.0, "size": 0, "mtime": 0, "meta": "{}", "probe": None})

    def test_an_entry_row_keeps_what_it_is_given(self):
        row = common.entry_row("lib", "item", "r", "audio", title="T", summary="S", season=2, disc=1, number=3, duration="4.5", size="7", mtime=8.9, meta={"é": 1})
        self.assertEqual((row["title"], row["summary"], row["season"], row["disc"], row["number"], row["duration"], row["size"], row["mtime"], row["meta"]),
                         ("T", "S", 2, 1, 3, 4.5, 7, 8, '{"é": 1}'))
        row = common.entry_row("lib", "item", "r", "audio", title=None, summary=None, duration=None, size=None, mtime=None)
        self.assertEqual((row["title"], row["summary"], row["duration"], row["size"], row["mtime"]), ("", "", 0.0, 0, 0))

    def test_numbers_in_a_title_are_sorted_as_numbers(self):
        self.assertLess(common.sort_key("Rocky 2"), common.sort_key("Rocky 10"))
        self.assertEqual(common.sort_key("Éléphant"), "elephant")

    def test_invisible_formatting_characters_of_a_tag_do_not_reach_the_title_the_sort_or_the_letter(self):
        # « La Bataille de Gaulle: J'écris ton nom » portait une marque de sens de lecture (U+200E) en tête : invisible, mais rangée après le Z
        for invisible in ("\u200e", "\u200b", "\ufeff", "\u200f", "\u2060", "\u00ad"):
            row = common.item_row("lib", "key", "movie", f"{invisible}La Bataille de Gaulle: J'écris ton nom{invisible}", grp=f"{invisible}La Bataille de Gaulle")
            self.assertEqual((row["title"], row["sort"], row["letter"], row["grp"]), ("La Bataille de Gaulle: J'écris ton nom", "la bataille de gaulle: j'ecris ton nom", "L", "La Bataille de Gaulle"), repr(invisible))

    def test_the_title_of_a_file_is_cleaned_like_the_title_of_an_item(self):
        row = common.entry_row("lib", "item", "r", "audio", title="\u200eOne More Time ")
        self.assertEqual(row["title"], "One More Time")

    def test_the_two_joiners_stay_in_the_title_but_never_decide_the_order(self):
        row = common.item_row("lib", "key", "movie", "\u200cabc\u200d")
        self.assertEqual((row["title"], row["sort"]), ("\u200cabc\u200d", "abc"))

    def test_a_film_with_an_invisible_character_is_sorted_between_its_neighbours_not_after_the_z(self):
        titles = ["Zootopie 2", "\u200eLa Bataille de Gaulle: J'écris ton nom", "Yoroï", "Batman"]
        rows = sorted((common.item_row("lib", t, "movie", t) for t in titles), key=lambda r: r["sort"])
        self.assertEqual([r["title"] for r in rows], ["Batman", "La Bataille de Gaulle: J'écris ton nom", "Yoroï", "Zootopie 2"])

    def test_visible_text_is_left_alone_accents_apostrophes_and_inner_spaces_included(self):
        row = common.item_row("lib", "key", "movie", "  L’Âge de fer : Œuvre  ", grp="Saga d'été")
        self.assertEqual((row["title"], row["grp"]), ("L’Âge de fer : Œuvre", "Saga d'été"))
        self.assertEqual(common.item_row("lib", "key", "movie", "\u200e")["title"], "")

    def test_a_file_is_unchanged_only_when_size_and_date_are_the_same(self):
        row = {"size": 10, "mtime": 20}
        self.assertTrue(common.unchanged(row, 10, 20))
        self.assertFalse(common.unchanged(row, 11, 20))
        self.assertFalse(common.unchanged(row, 10, 21))
        self.assertFalse(common.unchanged(None, 10, 20))

    def test_the_cover_of_an_album_is_cover_dot_an_image_extension(self):
        for name in ("cover.jpg", "COVER.PNG", "Cover.webp"):
            self.assertTrue(common.is_album_cover(name), name)
        for name in ("folder.jpg", "cover", "cover.txt", "mycover.jpg", "cover.jpg.bak"):
            self.assertFalse(common.is_album_cover(name), name)

    def test_the_image_of_a_folder_is_the_first_known_name_in_order(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        self.assertEqual(common.folder_image(folder), "")
        self.assertEqual(common.folder_image(folder / "absent"), "")
        for name in ("Thumb.jpg", "Cover.PNG", "notes.txt"):
            (folder / name).write_bytes(b"")
        self.assertEqual(common.folder_image(folder), "Cover.PNG")                  # cover avant thumb
        (folder / "poster.webp").write_bytes(b"")
        self.assertEqual(common.folder_image(folder), "poster.webp")                # poster avant cover
        (folder / "poster.jpg").write_bytes(b"")
        self.assertEqual(common.folder_image(folder), "poster.jpg")                 # .jpg avant .webp
        (folder / "sub.png").mkdir()
        (folder / "folder.png").mkdir()                                              # un dossier n'est pas une image
        self.assertEqual(common.folder_image(folder), "poster.jpg")


class TestScanLibrary(TempCase):
    def setUp(self):
        super().setUp()
        self.db = Database(self.root / "hub.db")
        self.said = []
        self.lib = config.Library("livres", "Livres", "books", self.root / "shelf")

    def scan(self):
        return scanning.scan_library(self.db, self.lib, log=self.said.append)

    def test_a_missing_folder_is_reported_and_nothing_is_stored(self):
        self.assertEqual(self.scan(), {"lib": "livres", "error": "dossier introuvable", "items": 0, "entries": 0})
        self.assertIn("dossier introuvable", self.said[0])
        self.assertEqual(self.db.known_entries("livres"), {})

    def test_a_scan_stores_the_library_and_says_how_much_was_read(self):
        fixtures.write_cbz(self.root / "shelf" / "Manga" / "Naruto" / "Tome 01.cbz", Series="Naruto", Number="1")
        clock = iter([100.0, 102.46])
        with mock.patch.object(scanning, "time", types.SimpleNamespace(time=lambda: next(clock, 555))):      # le module `time` du scan seulement : la base a sa propre horloge
            done = self.scan()
        self.assertEqual(done, {"lib": "livres", "items": 1, "entries": 1, "read": 1, "reused": 0, "seconds": 2.5})
        self.assertIn("1 élément(s), 1 fichier(s) (1 lu(s), 0 repris) en 2.5 s", self.said[0])
        self.assertEqual(list(self.db.known_entries("livres")), ["Manga/Naruto/Tome 01.cbz"])
        self.assertEqual(self.db.kv_get("scanned:livres"), 555)

    def test_the_next_scan_reuses_what_did_not_change(self):
        fixtures.write_cbz(self.root / "shelf" / "Manga" / "Naruto" / "Tome 01.cbz", Series="Naruto", Number="1")
        self.scan()
        done = self.scan()
        self.assertEqual((done["read"], done["reused"]), (0, 1))

    def test_a_library_that_became_empty_is_warned_about(self):
        fixtures.write_cbz(self.root / "shelf" / "Manga" / "Naruto" / "Tome 01.cbz", Series="Naruto", Number="1")
        self.scan()
        shutil.rmtree(self.root / "shelf" / "Manga")
        self.said.clear()
        done = self.scan()
        self.assertEqual((done["items"], done["entries"]), (0, 0))
        self.assertTrue(any("ATTENTION" in line and "en avait 1" in line for line in self.said))

    def test_an_empty_library_that_was_always_empty_is_not_a_warning(self):
        (self.root / "shelf").mkdir()
        self.scan()
        self.assertFalse(any("ATTENTION" in line for line in self.said))

    def test_enriched_data_is_found_by_item_id(self):
        self.db.kv_set("enrich:livres:abc", {"tmdb": 1})
        self.db.kv_set("enrich:autre:def", {"tmdb": 2})
        self.assertEqual(scanning.enriched_for(self.db, "livres"), {"abc": {"tmdb": 1}})

    def test_every_kind_of_library_has_its_scanner(self):
        self.assertEqual(sorted(scanning.SCANNERS), ["books", "movies", "music", "shows"])


if __name__ == "__main__":
    unittest.main()
