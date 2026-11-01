"""La pochette d'un album manquant, posée dans le dossier de l'artiste : largeur, étiquette, cas sans pochette."""

from __future__ import annotations

from tests import fixtures
from tests.test_server import ServerCase


class TestWantedImage(ServerCase):
    def setUp(self):
        artist = self.run_.libs["musique"] / "Daft Punk"
        for name in ("2013 - Random Access Memories.png", "1997 - Homework.txt"):
            path = artist / name
            path.write_bytes(fixtures.PNG)
            self.addCleanup(path.unlink, True)
        self.hub.jobs.scan(self.hub.cfg.library("musique"))
        self.run_.wait_jobs()
        artist_id = self.detail(self.item_by_title("musique", "Discovery")["id"])["artist_id"]
        sheet = self.api.json(f"/api/artist/musique/{artist_id}")[1]
        self.base = f"/img/wanted/musique/{artist_id}/"
        self.cover = sheet["wanted"][1]["cover"]                 # Random Access Memories : la seule qui ait une image
        self.assertEqual([bool(w["cover"]) for w in sheet["wanted"]], [False, True])        # Homework n'a pas d'image : la fiche montre une case grisée

    def test_the_cover_is_served_with_a_stable_tag(self):
        status, headers, data = self.api.get(self.cover)
        self.assertEqual((status, headers["Content-Type"].split("/")[0], headers["Cache-Control"]), (200, "image", "private, no-cache"))
        self.assertTrue(data)
        self.assertEqual(self.api.get(self.cover)[1]["ETag"], headers["ETag"])

    def test_the_same_tag_gets_a_304(self):
        tag = self.api.get(self.cover)[1]["ETag"]
        status, headers, data = self.api.get(self.cover, headers={"If-None-Match": tag})
        self.assertEqual((status, data, headers["ETag"]), (304, b"", tag))
        self.assertEqual(self.api.get(self.cover, headers={"If-None-Match": '"autre"'})[0], 200)

    def test_a_width_that_is_not_a_number_is_the_original_size(self):
        self.assertEqual(self.api.get(self.cover + "?w=abc")[0], 200)
        self.assertEqual(self.api.get(self.cover + "?w=160")[0], 200)

    def test_head_has_no_body(self):
        status, headers, data = self.api.request("HEAD", self.cover)
        self.assertEqual((status, data), (200, b""))

    def test_an_unknown_album_or_artist_has_no_cover(self):
        self.assertEqual(self.api.get(self.base + "inconnu")[0], 404)
        self.assertEqual(self.api.get("/img/wanted/musique/deadbeef/inconnu")[0], 404)
