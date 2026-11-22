"""Corriger la fiche d'un artiste dans le hub : nom affiché, genres, résumé, pays, années d'activité et fiche MusicBrainz choisie, sans rien écrire dans la médiathèque.

L'identifiant de l'artiste (tiré de son nom par media_library) ne change jamais : regroupement, albums manquants et photo en dépendent. Les corrections sont appliquées à la lecture.
"""

from __future__ import annotations

import json
import types
from unittest import mock

from tests.rig import call
from tests.test_server_routes import JPEG, ArtistCase, item
from tests.test_server_views import entry
from hub import accounts, enrich
from hub.media import images

MB1 = "11111111-1111-1111-1111-111111111111"
MB2 = "22222222-2222-2222-2222-222222222222"


class FakeMusicBrainz:
    """MusicBrainz et Wikipédia sans réseau : deux fiches (MB1, le groupe des étiquettes ; MB2, un homonyme) et une recherche par nom."""
    SHEETS = {MB1: {"type": "Group", "area": {"name": "France"}, "life-span": {"begin": "1993", "end": "2021", "ended": True}, "relations": []},
              MB2: {"type": "Person", "area": {"name": "Canada"}, "life-span": {"begin": "1980"}, "disambiguation": "homonyme",
                    "relations": [{"type": "official homepage", "url": {"resource": "https://homonyme.example/"}}]}}

    def __init__(self):
        self.asked = []

    def artist(self, mbid):
        self.asked.append(mbid)
        if mbid not in self.SHEETS:
            raise enrich.MusicBrainzError("introuvable", 404)
        return {"id": mbid, **self.SHEETS[mbid]}

    def external(self, url):
        raise enrich.MusicBrainzError("pas d'article", 404)

    def search_artists(self, name, limit=10):
        self.asked.append(("search", name, limit))
        return [{"id": MB2, "name": "Daft Punk", "type": "Person", "country": "CA", "disambiguation": "homonyme", "score": 100},
                {"id": MB1, "name": "Daft Punk", "type": "Group", "country": "FR", "score": 90}] * 10


class EditCase(ArtistCase):
    def setUp(self):
        super().setUp()
        self.db.execute("UPDATE items SET meta=json_set(meta, '$.artist_mbid', ?, '$.artist', 'Daft Punk') WHERE grp='Daft Punk'", (MB1,))
        self.db.execute("UPDATE entries SET meta=? WHERE id='t1'", (json.dumps({"info": {"artist": "Daft Punk"}}),))
        self.db.execute("UPDATE entries SET meta=? WHERE id='t3'", (json.dumps({"info": {"artist": "Daft Punk feat. Pharrell"}}),))
        self.db.kv_set(f"artist:{MB1}", {"fetched": "2026-01-01", "info": {"type": "Group", "area": "France", "begin": "1993", "end": "2021", "members": [],
                                                                          "wiki": {"lang": "fr", "title": "Daft Punk", "url": "https://fr.wikipedia.org/wiki/Daft_Punk", "extract": "Un duo."},
                                                                          "links": [{"label": "Site officiel", "url": "https://daftpunk.example/"}]}})
        self.root_cookie = self.hub.cookie(accounts.create(self.db, "root", "motdepasse12"))
        self.bob = self.hub.cookie(accounts.create(self.db, "bob", "motdepasse12"))

    def edit(self, body, status=200, headers=None, target="/api/artist/lib/ar1/edit"):
        reply = self.post(target, body, headers=headers or self.root_cookie)
        self.assertEqual(reply.status, status, reply.body)
        return reply

    def artist(self, headers=None):
        reply = self.get("/api/artist/lib/ar1", headers=headers or self.root_cookie)
        self.assertEqual(reply.status, 200, reply.body)
        return reply.json()

    def listing(self, query=""):
        return self.get("/api/library/lib?" + query, headers=self.root_cookie).json()["items"]

    def pin(self, mbid, status=200, fake=None, headers=None):
        with mock.patch.object(enrich, "musicbrainz", return_value=fake or FakeMusicBrainz()):
            reply = self.post("/api/artist/lib/ar1/musicbrainz", {"mbid": mbid}, headers=headers or self.root_cookie)
        self.assertEqual(reply.status, status, reply.body)
        return reply


class TestTheShownName(EditCase):
    def test_the_sheet_shows_the_corrected_name(self):
        reply = self.edit({"name": "Les Robots"})
        self.assertEqual(reply.json(), {"ok": True, "edited": ["name"]})
        self.assertEqual(self.artist()["name"], "Les Robots")

    def test_the_artists_view_sorts_and_searches_by_the_corrected_name(self):
        self.assertEqual([c["title"] for c in self.listing("view=artists")], ["Air", "Daft Punk"])
        self.edit({"name": "Zébulon"})
        cards = self.listing("view=artists")
        self.assertEqual([(c["id"], c["title"], c["letter"]) for c in cards], [("ar2", "Air", "A"), ("ar1", "Zébulon", "Z")])
        self.assertEqual([c["id"] for c in self.listing("view=artists&q=zebulon")], ["ar1"])
        self.assertEqual(self.listing("view=artists&q=daft"), [])

    def test_the_album_cards_the_album_list_and_its_search_use_the_corrected_name(self):
        self.edit({"name": "Zébulon"})
        cards = self.listing()
        self.assertEqual([(c["id"], c["grp"]) for c in cards], [("other", "Air"), ("al1", "Zébulon"), ("al2", "Zébulon"), ("al3", "Zébulon")])
        self.assertEqual([c["id"] for c in self.listing("q=zebulon")], ["al1", "al2", "al3"])

    def test_the_album_sheet_and_its_tracks_use_the_corrected_name(self):
        self.edit({"name": "Les Robots"})
        sheet = self.get("/api/item/al1", headers=self.root_cookie).json()
        self.assertEqual((sheet["grp"], sheet["artist"], sheet["artist_id"]), ("Les Robots", "Les Robots", "ar1"))
        self.assertEqual([e["artist"] for e in sheet["entries"]], ["Les Robots", ""])          # le crédit de la piste qui était celui de l'album suit ; une piste sans crédit reste sans
        other = self.get("/api/item/al2", headers=self.root_cookie).json()
        self.assertEqual([e["artist"] for e in other["entries"]], ["Daft Punk feat. Pharrell"])       # un autre crédit n'est pas le nom de l'artiste : il reste

    def test_the_tracks_of_the_artist_sheet_use_the_corrected_name(self):
        self.edit({"name": "Les Robots"})
        self.assertEqual([t["artist"] for t in self.artist()["tracks"]], ["Les Robots", "Les Robots", "Daft Punk feat. Pharrell"])

    def test_the_home_page_and_the_global_search_use_the_corrected_name(self):
        self.edit({"name": "Les Robots"})
        home = self.get("/api/home", headers=self.root_cookie).json()
        recent = home["libraries"][0]["recent"]
        self.assertIn(("artist", "Les Robots"), [(c["type"], c["title"]) for c in recent])
        found = self.get("/api/search?q=robots", headers=self.root_cookie).json()["results"]
        self.assertEqual(sorted((c["id"], c["grp"]) for c in found[0]["items"]), [("al1", "Les Robots"), ("al2", "Les Robots"), ("al3", "Les Robots")])

    def test_every_account_sees_the_corrected_name(self):
        self.edit({"name": "Les Robots"})
        self.assertEqual(self.artist(self.bob)["name"], "Les Robots")


class TestWhatDoesNotChange(EditCase):
    def test_grouping_wanted_albums_and_the_photo_stay_with_the_same_identifier(self):
        self.db.kv_set("wanted:lib", {"ar1": [{"key": "k1", "year": "1997", "title": "Homework", "cover": ""}]})
        (self.root / "data" / "artists").mkdir()
        (self.root / "data" / "artists" / f"{MB1}.jpg").write_bytes(JPEG)          # une photo de l'ancien nom (celui de la fiche gardée) : elle se retrouve par le mbid des albums
        self.hub.images = images.Images(self.hub.cfg, self.db)
        self.edit({"name": "Les Robots"})
        self.pin(MB2)
        cards = self.listing("view=artists")
        self.assertEqual([(c["id"], c["n"]) for c in cards], [("ar2", 1), ("ar1", 3)])
        out = self.artist()
        self.assertEqual((out["id"], [a["id"] for a in out["albums"]], [w["title"] for w in out["wanted"]], out["photo"]), ("ar1", ["al1", "al2", "al3"], ["Homework"], True))
        self.assertEqual(self.get("/img/artist/lib/ar1", headers=self.root_cookie).status, 200)
        self.assertEqual([r["grp"] for r in self.db.query("SELECT grp FROM items WHERE type='album' ORDER BY id")], ["Daft Punk", "Daft Punk", "Daft Punk", "Air"])   # la base garde les étiquettes

    def test_nothing_is_written_in_the_library(self):
        before = sorted(p.as_posix() for p in self.lib.path.rglob("*"))
        self.edit({"name": "Les Robots", "genres": ["Rock"], "summary": "Deux robots.", "country": "Mars", "active": "depuis toujours"})
        self.assertEqual(sorted(p.as_posix() for p in self.lib.path.rglob("*")), before)


class TestTheSheetFields(EditCase):
    def test_summary_country_and_years_replace_what_musicbrainz_and_wikipedia_say(self):
        self.assertEqual(self.artist()["info"]["facts"], "Groupe · France · 1993 – 2021")
        self.edit({"summary": "Deux robots.", "country": "Mars", "active": "depuis toujours"})
        info = self.artist()["info"]
        self.assertEqual((info["facts"], info["summary"], info["wiki"]), ("Groupe · Mars · depuis toujours", "Deux robots.", None))
        self.assertIn("https://fr.wikipedia.org/wiki/Daft_Punk", [link["url"] for link in info["links"]])      # la source reste parmi les liens

    def test_the_country_of_musicbrainz_is_said_in_the_language_of_the_page_and_a_corrected_one_as_written(self):
        kept = self.db.kv_get(f"artist:{MB1}")
        kept["info"]["area"] = "Germany"
        self.db.kv_set(f"artist:{MB1}", kept)
        english = {"Cookie": self.root_cookie["Cookie"] + "; hub_lang=en"}
        self.assertEqual((self.artist()["info"]["facts"], self.artist(english)["info"]["facts"]), ("Groupe · Allemagne · 1993 – 2021", "Groupe · Germany · 1993 – 2021"))
        self.edit({"country": "RFA"})
        self.assertEqual(self.artist(english)["info"]["facts"], "Groupe · RFA · 1993 – 2021")

    def test_an_artist_without_a_sheet_gets_one_from_its_corrections(self):
        self.db.kv_delete(f"artist:{MB1}")
        self.assertNotIn("info", self.artist())
        self.edit({"country": "Mars"})
        self.assertEqual(self.artist()["info"]["facts"], "Mars")

    def test_each_field_comes_back_on_its_own(self):
        self.edit({"name": "Les Robots", "genres": ["Rock"], "summary": "Deux robots.", "country": "Mars", "active": "depuis toujours"})
        self.assertEqual(self.artist()["edited"], ["name", "genres", "summary", "country", "active"])
        self.assertEqual(self.edit({"country": None}).json()["edited"], ["name", "genres", "summary", "active"])
        out = self.artist()
        self.assertEqual((out["name"], out["info"]["facts"]), ("Les Robots", "Groupe · France · depuis toujours"))
        self.edit({"name": None, "genres": None})
        out = self.artist()
        self.assertEqual((out["name"], out["genres"], out["edited"]), ("Daft Punk", ["B", "A", "C", "D", "E"], ["summary", "active"]))
        self.edit({"summary": None, "active": None})
        self.assertEqual((self.artist()["edited"], self.db.kv_get("artistedit:lib:ar1")), ([], None))
        self.assertEqual(self.artist()["info"]["wiki"]["extract"], "Un duo.")

    def test_only_the_administrator_is_told_what_was_corrected(self):
        self.edit({"name": "Les Robots"})
        self.assertNotIn("edited", self.artist(self.bob))
        self.assertEqual(self.artist()["edited"], ["name"])


class TestGenres(EditCase):
    def test_corrected_genres_show_on_the_sheet_and_only_the_known_ones_are_linkable(self):
        self.assertEqual(self.artist()["genres_linkable"], ["B", "A", "C", "D", "E"])
        self.edit({"genres": ["Rock", "B", "Inconnu"]})
        out = self.artist()
        self.assertEqual((out["genres"], out["genres_linkable"]), (["Rock", "B", "Inconnu"], ["B"]))

    def test_a_genre_of_another_artist_of_the_library_is_linkable(self):
        self.db.execute("UPDATE items SET genres='[\"Jazz\"]' WHERE id='other'")
        self.edit({"genres": ["Jazz"]})
        self.assertEqual(self.artist()["genres_linkable"], ["Jazz"])

    def test_the_genre_filter_still_goes_by_the_albums(self):
        self.edit({"genres": ["Rock"]})
        self.assertEqual(self.listing("view=artists&genre=Rock"), [])
        self.assertEqual([c["id"] for c in self.listing("view=artists&genre=B")], ["ar1"])


class TestBounds(EditCase):
    def refused(self, body, message=None):
        reply = self.edit(body, status=400)
        if message:
            self.assertIn(message, reply.json()["error"])
        self.assertIsNone(self.db.kv_get("artistedit:lib:ar1"))

    def test_the_name_is_one_to_two_hundred_characters_without_control_characters(self):
        self.refused({"name": ""}, "nom")
        self.refused({"name": "   "})
        self.refused({"name": "x" * 201}, "200")
        self.refused({"name": "Daft\nPunk"}, "contrôle")
        self.refused({"name": 12})
        self.edit({"name": "x" * 200})

    def test_at_most_twenty_genres_of_sixty_characters(self):
        self.refused({"genres": ["g%d" % n for n in range(21)]}, "20")
        self.refused({"genres": ["x" * 61]}, "60")
        self.refused({"genres": "Rock"})
        self.refused({"genres": [""]})
        self.edit({"genres": ["g%d" % n for n in range(20)]})

    def test_the_summary_is_five_thousand_characters_at_most_and_keeps_its_paragraphs(self):
        self.refused({"summary": "x" * 5001}, "5000")
        self.edit({"summary": "x" * 5000})
        self.edit({"summary": None})
        self.refused({"summary": "a\x00b"}, "contrôle")
        self.edit({"summary": "Premier.\n\nSecond."})
        self.assertEqual(self.artist()["info"]["summary"], "Premier.\n\nSecond.")

    def test_country_and_years_are_short_texts(self):
        self.refused({"country": "x" * 101}, "100")
        self.refused({"active": "x" * 61}, "60")

    def test_an_unknown_field_or_an_empty_body_is_refused_and_nothing_is_kept(self):
        self.refused({"name": "Les Robots", "photo": "x"}, "champ inconnu")
        self.refused({}, "rien")

    def test_one_refused_field_keeps_none(self):
        self.refused({"name": "Les Robots", "genres": "Rock"})

    def test_the_musicbrainz_sheet_is_chosen_by_its_own_route(self):
        self.refused({"mbid": MB2}, "/musicbrainz")


class TestWhoMayEdit(EditCase):
    def test_an_ordinary_account_is_refused(self):
        self.edit({"name": "Les Robots"}, status=403, headers=self.bob)
        self.pin(MB2, status=403, headers=self.bob)
        self.assertEqual(self.get("/api/artist/lib/ar1/musicbrainz?q=daft", headers=self.bob).status, 403)
        self.assertIsNone(self.db.kv_get("artistedit:lib:ar1"))

    def test_a_request_without_a_session_or_with_a_wrong_token_is_refused(self):
        self.assertEqual(call(self.hub, "POST", "/api/artist/lib/ar1/edit", {"name": "X"})[1].status, 401)
        self.edit({"name": "X"}, status=401, headers={"Authorization": "Bearer faux"})
        self.assertIsNone(self.db.kv_get("artistedit:lib:ar1"))

    def test_an_unknown_artist_is_a_404(self):
        self.edit({"name": "X"}, status=404, target="/api/artist/lib/nope/edit")


class TestMusicBrainzSheet(EditCase):
    def test_the_search_is_bounded_and_gives_what_helps_to_choose(self):
        fake = FakeMusicBrainz()
        with mock.patch.object(enrich, "musicbrainz", return_value=fake):
            reply = self.get("/api/artist/lib/ar1/musicbrainz?q=Daft+Punk", headers=self.root_cookie)
        self.assertEqual(reply.status, 200, reply.body)
        results = reply.json()["results"]
        self.assertEqual(len(results), 10)
        self.assertEqual(results[0], {"mbid": MB2, "name": "Daft Punk", "type": "Person", "country": "CA", "disambiguation": "homonyme", "score": 100})
        self.assertEqual(fake.asked, [("search", "Daft Punk", 10)])

    def test_an_empty_or_too_long_search_is_refused(self):
        self.assertEqual(self.get("/api/artist/lib/ar1/musicbrainz?q=", headers=self.root_cookie).status, 400)
        self.assertEqual(self.get("/api/artist/lib/ar1/musicbrainz?q=" + "x" * 201, headers=self.root_cookie).status, 400)
        with mock.patch.object(enrich, "musicbrainz", return_value=FakeMusicBrainz()):
            self.assertEqual(self.get("/api/artist/lib/ar1/musicbrainz?q=" + "x" * 200, headers=self.root_cookie).status, 200)

    def test_musicbrainz_out_of_reach_is_a_502_in_french(self):
        fake = FakeMusicBrainz()
        fake.search_artists = mock.Mock(side_effect=enrich.MusicBrainzError("délai dépassé"))
        with mock.patch.object(enrich, "musicbrainz", return_value=fake):
            reply = self.get("/api/artist/lib/ar1/musicbrainz?q=daft", headers=self.root_cookie)
        self.assertEqual((reply.status, reply.json()), (502, {"error": "MusicBrainz : délai dépassé"}))

    def test_pinning_another_sheet_rebuilds_it_from_musicbrainz_now(self):
        fake = FakeMusicBrainz()
        reply = self.pin(MB2, fake=fake)
        self.assertEqual(reply.json(), {"ok": True, "edited": ["mbid"]})
        self.assertEqual(fake.asked[0], MB2)
        info = self.artist()["info"]
        self.assertEqual((info["facts"], info["disambiguation"]), ("Artiste solo · Canada · né en 1980", "homonyme"))
        self.assertEqual([link["url"] for link in info["links"]], ["https://homonyme.example/", f"https://musicbrainz.org/artist/{MB2}"])

    def test_the_address_of_the_sheet_is_accepted(self):
        self.pin(f"https://musicbrainz.org/artist/{MB2.upper()}")
        self.assertIn(f"https://musicbrainz.org/artist/{MB2}", [link["url"] for link in self.artist()["info"]["links"]])

    def test_unpinning_comes_back_to_the_sheet_of_the_albums(self):
        self.edit({"name": "Les Robots"})
        self.pin(MB2)
        reply = self.pin(None)
        self.assertEqual(reply.json(), {"ok": True, "edited": ["name"]})
        info = self.artist()["info"]
        self.assertEqual(info["facts"], "Groupe · France · 1993 – 2021")
        self.assertIn(f"https://musicbrainz.org/artist/{MB1}", [link["url"] for link in info["links"]])
        self.pin(MB2)
        self.edit({"mbid": None})                       # « Rétablir » de la fiche par la route des corrections, comme les autres champs
        self.assertEqual(self.artist()["info"]["facts"], "Groupe · France · 1993 – 2021")

    def test_an_invalid_identifier_is_refused_and_musicbrainz_is_not_asked(self):
        fake = FakeMusicBrainz()
        for bad in ("abc", "1111111-1111-1111-1111-111111111111", 12, "", f"{MB2}/../x"):
            self.pin(bad, status=400, fake=fake)
        self.assertEqual((fake.asked, self.db.kv_get("artistedit:lib:ar1")), ([], None))

    def test_a_sheet_musicbrainz_does_not_know_is_a_502_and_nothing_is_kept(self):
        self.pin("33333333-3333-3333-3333-333333333333", status=502)
        self.assertIsNone(self.db.kv_get("artistedit:lib:ar1"))

    def test_the_links_follow_the_chosen_sheet(self):
        self.post("/api/artist/lib/ar1/links", {"url": "https://ajout.example/"}, headers=self.root_cookie)
        self.assertIn("https://ajout.example/", [link["url"] for link in self.artist()["info"]["links"]])
        self.pin(MB2)
        self.assertNotIn("https://ajout.example/", [link["url"] for link in self.artist()["info"]["links"]])     # rangés sous l'autre fiche : ils ne la suivent pas
        self.post("/api/artist/lib/ar1/links", {"url": "https://autre.example/"}, headers=self.root_cookie)
        self.assertEqual(self.db.kv_get(f"artistlinks:{MB2}")["added"], [{"label": "autre.example", "url": "https://autre.example/"}])
        self.pin(None)
        links = [link["url"] for link in self.artist()["info"]["links"]]
        self.assertIn("https://ajout.example/", links)                                                         # retirer le choix les rend
        self.assertNotIn("https://autre.example/", links)

    def test_the_artists_job_keeps_the_pinned_sheet_fresh(self):
        self.pin(MB2)
        self.db.kv_set(f"artist:{MB2}", {"fetched": "2000-01-01", "info": {}})
        fake = FakeMusicBrainz()
        with mock.patch.object(enrich, "musicbrainz", return_value=fake):
            enrich.enrich_artists(types.SimpleNamespace(data=self.root), self.db, self.lib, lambda line: None)
        self.assertIn(MB2, fake.asked)
        self.assertEqual(self.db.kv_get(f"artist:{MB2}")["info"]["area"], "Canada")

    def test_a_rescan_keeps_the_corrections(self):
        self.edit({"name": "Les Robots"})
        self.pin(MB2)
        before = self.db.kv_get("artistedit:lib:ar1")
        self.fill([item("al1", "album", grp="Daft Punk", meta={"artist_id": "ar1", "artist_mbid": MB1})], [entry("t1", "al1", kind="audio", number=1)])
        self.assertEqual(self.db.kv_get("artistedit:lib:ar1"), before)
        self.assertEqual(self.artist()["name"], "Les Robots")


if __name__ == "__main__":
    import unittest
    unittest.main()
