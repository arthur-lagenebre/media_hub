"""Les albums en collaboration : un album crédité à plusieurs artistes (ALBUMARTISTS) est rattaché à chacun d'eux, sans qu'aucune liste le compte deux fois."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tests import fixtures
from hub import config

fixtures.REPO  # noqa: B018  (rend `libraries` importable)
config.use_media_library(fixtures.REPO)

from libraries.music.library import artist_id  # noqa: E402

from hub import enrich  # noqa: E402
from hub.scan import music  # noqa: E402
from tests.test_server import ServerCase  # noqa: E402

DP_MBID = "aaaaaaaa-0000-0000-0000-000000000001"
POPPY_MBID = "aaaaaaaa-0000-0000-0000-000000000002"


def collaboration(folder, names, mbids, title="Duo", year="2013"):
    """Un album d'une piste crédité à `names` (ALBUMARTIST « A & B », ALBUMARTISTS un nom par artiste, MUSICBRAINZ_ALBUMARTISTID `mbids`)."""
    path = fixtures.write_flac(folder / f"{year} - {title}" / "01 - Ensemble.flac", "Ensemble", title, " & ".join(names), 1, 1, year=year)
    fixtures.add_tags(path, *[("ALBUMARTISTS", n) for n in names], *[("MUSICBRAINZ_ALBUMARTISTID", i) for i in mbids])
    return path


class TestScanCollaborations(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.lib = config.Library("musique", "Musique", "music", self.root)

    def scan(self, known=None):
        items, entries, counts = music.scan_music(self.lib, known or {})
        return {i["title"]: json.loads(i["meta"]) for i in items}, {i["title"]: i for i in items}, entries, counts

    def test_a_collaboration_names_each_credited_artist_with_their_identifier(self):
        collaboration(self.root / "Daft Punk", ["Daft Punk", "Poppy"], [DP_MBID, POPPY_MBID])
        meta, rows, _, _ = self.scan()
        self.assertEqual(meta["Duo"]["credits"], [{"name": "Daft Punk", "id": artist_id("Daft Punk"), "mbid": DP_MBID},
                                                  {"name": "Poppy", "id": artist_id("Poppy"), "mbid": POPPY_MBID}])
        self.assertEqual((meta["Duo"]["artist_id"], meta["Duo"]["artist_mbid"], meta["Duo"]["artist_dir"]), (artist_id("Daft Punk"), DP_MBID, "Daft Punk"))
        self.assertEqual((meta["Duo"]["artist"], rows["Duo"]["grp"]), ("Daft Punk & Poppy", "Daft Punk & Poppy"))       # le texte de l'étiquette reste celui qu'on montre

    def test_the_main_artist_is_the_one_whose_folder_holds_the_album(self):
        for title in ("Seule", "Encore"):
            fixtures.write_flac(self.root / "Poppy" / f"2017 - {title}" / "01 - S.flac", "S", title, "Poppy", 1, 1, year="2017")
        collaboration(self.root / "Poppy", ["Daft Punk", "Poppy"], [DP_MBID, POPPY_MBID])
        meta = self.scan()[0]
        self.assertEqual((meta["Duo"]["artist_id"], meta["Duo"]["artist_mbid"], meta["Duo"]["artist_dir"]), (artist_id("Poppy"), POPPY_MBID, "Poppy"))

    def test_without_a_folder_the_first_credited_is_the_main_artist(self):
        collaboration(self.root, ["Daft Punk", "Poppy"], [DP_MBID, POPPY_MBID])          # posé à la racine : personne n'a de dossier
        meta = self.scan()[0]
        self.assertEqual((meta["Duo"]["artist_id"], meta["Duo"]["artist_dir"]), (artist_id("Daft Punk"), ""))

    def test_names_and_identifiers_of_different_lengths_are_not_paired(self):
        collaboration(self.root / "Daft Punk", ["Daft Punk", "Poppy"], [DP_MBID])
        meta = self.scan()[0]
        self.assertEqual([c["mbid"] for c in meta["Duo"]["credits"]], ["", ""])
        self.assertEqual(meta["Duo"]["artist_mbid"], "")

    def test_an_album_without_albumartists_is_unchanged(self):
        fixtures.write_flac(self.root / "Daft Punk" / "2001 - Discovery" / "01 - A.flac", "A", "Discovery", "Daft Punk", 1, 1)
        meta = self.scan()[0]
        self.assertNotIn("credits", meta["Discovery"])
        self.assertEqual((meta["Discovery"]["artist_id"], meta["Discovery"]["artist_dir"]), (artist_id("Daft Punk"), "Daft Punk"))

    def test_the_artist_folder_counts_the_first_credited(self):
        fixtures.write_flac(self.root / "Daft Punk" / "2001 - Discovery" / "01 - A.flac", "A", "Discovery", "Daft Punk", 1, 1)
        collaboration(self.root / "Daft Punk", ["Daft Punk", "Poppy"], [DP_MBID, POPPY_MBID], title="Un")
        collaboration(self.root / "Daft Punk", ["Daft Punk", "Poppy"], [DP_MBID, POPPY_MBID], title="Deux", year="2014")
        meta = self.scan()[0]
        self.assertEqual({m["artist_dir"] for m in meta.values()}, {"Daft Punk"})            # « Daft Punk & Poppy » n'est l'artiste d'aucun dossier : Daft Punk l'est

    def test_a_track_kept_from_before_albumartists_were_read_is_read_again(self):
        collaboration(self.root / "Daft Punk", ["Daft Punk", "Poppy"], [DP_MBID, POPPY_MBID])
        entries = {e["rel"]: e for e in self.scan()[2]}
        rel = "Daft Punk/2013 - Duo/01 - Ensemble.flac"
        meta = json.loads(entries[rel]["meta"])
        del meta["info"]["albumartists"]
        entries[rel] = {**entries[rel], "meta": json.dumps(meta)}
        counts = self.scan(entries)[3]
        self.assertEqual((counts.read, counts.reused), (1, 0))

    def test_a_wanted_album_owned_as_a_collaboration_in_the_same_folder_is_not_wanted(self):
        fixtures.write_flac(self.root / "Daft Punk" / "2001 - Discovery" / "01 - A.flac", "A", "Discovery", "Daft Punk", 1, 1)
        collaboration(self.root / "Daft Punk", ["Daft Punk", "Poppy"], [DP_MBID, POPPY_MBID])
        (self.root / "Daft Punk" / "2013 - Duo.jpg").write_bytes(fixtures.PNG)
        (self.root / "Daft Punk" / "2020 - Absent.jpg").write_bytes(fixtures.PNG)
        sheet = self.scan()[3].kv["wanted:musique"]
        self.assertEqual([w["title"] for w in sheet[artist_id("Daft Punk")]], ["Absent"])

    def test_an_album_of_the_same_name_in_another_artist_folder_does_not_count(self):
        fixtures.write_flac(self.root / "Daft Punk" / "2001 - Discovery" / "01 - A.flac", "A", "Discovery", "Daft Punk", 1, 1)
        fixtures.write_flac(self.root / "Muse" / "2013 - Duo" / "01 - A.flac", "A", "Duo", "Muse", 1, 1, year="2013")
        (self.root / "Daft Punk" / "2013 - Duo.jpg").write_bytes(fixtures.PNG)
        sheet = self.scan()[3].kv["wanted:musique"]
        self.assertEqual([w["title"] for w in sheet[artist_id("Daft Punk")]], ["Duo"])


class TestCreditsOfAnAlbum(unittest.TestCase):
    """Ce que la fiche et la grille tirent de `meta.credits` : un album d'un seul crédité (ALBUMARTISTS d'un seul nom, le cas de tout album repassé par media_library) n'est pas une collaboration."""

    DUO = {"artist_id": "dp", "artist_mbid": DP_MBID, "credits": [{"name": "Daft Punk", "id": "dp", "mbid": DP_MBID}, {"name": "Poppy", "id": "po", "mbid": POPPY_MBID}]}
    SOLO = {"artist_id": "dp", "artist_mbid": DP_MBID, "credits": [{"name": "Daft Punk", "id": "dp", "mbid": DP_MBID}]}

    def test_an_album_with_a_single_credit_is_not_a_collaboration(self):
        from hub.server import app
        self.assertEqual(app.album_credits(self.SOLO), [])
        self.assertEqual(len(app.album_credits(self.DUO)), 2)
        self.assertEqual(app.album_credits({"credits": "abîmé"}), [])

    def test_each_credited_artist_takes_their_own_identifier(self):
        from hub.server import app
        self.assertEqual((app.artist_mbid_of(self.DUO, "dp"), app.artist_mbid_of(self.DUO, "po")), (DP_MBID, POPPY_MBID))
        self.assertEqual(app.artist_mbid_of(self.SOLO, "dp"), DP_MBID)

    def test_a_corrected_name_replaces_the_label_of_a_single_credit_album_only(self):
        from hub import artistedit
        solo, duo = ({"grp": "Daft Punk", "meta": json.dumps(meta)} for meta in (self.SOLO, self.DUO))
        self.assertEqual(artistedit.renamed(solo, {"dp": "Les Robots"})["grp"], "Les Robots")
        self.assertEqual(artistedit.renamed(duo, {"dp": "Les Robots"})["grp"], "Daft Punk")


class TestCollaborationsServer(ServerCase):
    """Daft Punk (Discovery, dans son dossier) et Duo, une collaboration de Daft Punk et Poppy rangée dans le dossier de Daft Punk ; Poppy n'a pas de dossier."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        folder = cls.run_.libs["musique"] / "Daft Punk"
        collaboration(folder, ["Daft Punk", "Poppy"], [DP_MBID, POPPY_MBID])
        (folder / "2013 - Duo.jpg").write_bytes(fixtures.PNG)              # l'album était manquant ; acquis depuis en collaboration
        (folder / "artist.webp").write_bytes(b"photo de Daft Punk")
        cls.hub.jobs.scan(cls.hub.cfg.library("musique"))
        cls.run_.wait_jobs()
        cls.dp, cls.poppy = artist_id("Daft Punk"), artist_id("Poppy")

    def artist(self, ident):
        status, data = self.api.json(f"/api/artist/musique/{ident}")
        self.assertEqual(status, 200, ident)
        return data

    def test_the_main_artist_keeps_their_albums_and_lists_the_collaboration_apart(self):
        sheet = self.artist(self.dp)
        self.assertEqual([a["title"] for a in sheet["albums"]], ["Discovery"])
        self.assertEqual([a["title"] for a in sheet["collaborations"]], ["Duo"])
        self.assertTrue(sheet["photo"])
        self.assertEqual(sheet["wanted"], [])                                # Duo est là : il ne manque plus

    def test_a_participant_without_folder_has_a_sheet_of_collaborations(self):
        sheet = self.artist(self.poppy)
        self.assertEqual((sheet["name"], sheet["albums"]), ("Poppy", []))
        self.assertEqual([a["title"] for a in sheet["collaborations"]], ["Duo"])
        self.assertEqual(self.api.json("/api/artist/musique/deadbeef")[0], 404)

    def test_a_participant_photo_goes_under_the_data_folder(self):
        home = self.run_.libs["musique"] / "Daft Punk" / "artist.webp"
        self.assertFalse(self.artist(self.poppy)["photo"])                   # la photo de Daft Punk, dans le dossier de l'album, n'est pas la sienne
        status, _, _ = self.api.request("POST", f"/api/artist/musique/{self.poppy}/photo", body=fixtures.PNG, headers={"Content-Type": "image/png"})
        self.assertEqual(status, 200)
        self.assertEqual(home.read_bytes(), b"photo de Daft Punk")
        self.assertTrue(list((self.hub.cfg.data / "artists").glob(f"{self.poppy}.*")))
        self.assertTrue(self.artist(self.poppy)["photo"])

    def test_the_grid_has_no_card_for_a_participant_and_counts_each_album_once(self):
        albums, artists = self.items("musique"), self.items("musique", view="artists")
        self.assertEqual(sorted(i["title"] for i in albums["items"]), ["Discovery", "Duo"])
        self.assertEqual([i["id"] for i in artists["items"]], [self.dp])
        self.assertEqual(artists["items"][0]["n"], 2)
        for counts in (albums, artists):
            self.assertEqual((counts["artists"], counts["albums"]), (1, 2))

    def test_the_home_has_no_card_for_a_participant(self):
        status, home = self.api.json("/api/home")
        self.assertEqual(status, 200)
        recent = next(lib["recent"] for lib in home["libraries"] if lib["id"] == "musique")
        self.assertNotIn(self.poppy, [card["id"] for card in recent])
        self.assertEqual(sum(card.get("n", 1) if card["type"] == "artist" else 1 for card in recent), 2)

    def test_the_album_names_its_credited_artists(self):
        duo = self.item_by_title("musique", "Duo")
        self.assertEqual(duo["credits"], [{"name": "Daft Punk", "id": self.dp}, {"name": "Poppy", "id": self.poppy}])
        self.assertEqual(self.detail(duo["id"])["credits"], duo["credits"])
        self.assertNotIn("credits", self.item_by_title("musique", "Discovery"))

    def test_renaming_the_main_artist_keeps_the_collaboration_label(self):
        base = f"/api/artist/musique/{self.dp}/edit"
        self.assertEqual(self.api.json(base, {"name": "Les Robots"})[0], 200)
        self.addCleanup(self.api.json, base, {"name": None})
        self.assertEqual(self.item_by_title("musique", "Duo")["grp"], "Daft Punk & Poppy")
        self.assertEqual(self.item_by_title("musique", "Discovery")["grp"], "Les Robots")
        sheet = self.artist(self.dp)
        self.assertEqual({t["artist"] for t in sheet["tracks"] if t["album"] == "Duo"}, {"Daft Punk & Poppy"})

    def test_search_finds_a_collaboration_by_a_credited_name(self):
        status, data = self.api.json("/api/search?q=poppy")
        self.assertEqual(status, 200)
        found = [i for lib in data["results"] for i in lib["items"]]
        self.assertEqual([(i["title"], [c["id"] for c in i["credits"]]) for i in found], [("Duo", [self.dp, self.poppy])])

    def test_artist_sheets_cover_every_credited_artist_with_an_identifier(self):
        artists, ids, homes = enrich.album_artists(self.hub.db, self.hub.cfg.library("musique"))
        self.assertEqual((artists[DP_MBID], artists[POPPY_MBID]), ("Daft Punk", "Poppy"))
        self.assertEqual((ids[DP_MBID], ids[POPPY_MBID]), ({self.dp}, {self.poppy}))
        self.assertEqual(homes, {self.dp: self.run_.libs["musique"] / "Daft Punk"})


if __name__ == "__main__":
    unittest.main()
