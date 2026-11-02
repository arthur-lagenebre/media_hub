"""Les playlists : des listes de pistes gardées dans la base, dont l'ordre se change et que les pistes disparues ne cassent pas."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import playlists
from hub.db import Database
from hub.playlists import PlaylistError


def item(ident, title):
    return {"id": ident, "lib": "musique", "type": "album", "title": title, "sort": title.casefold(), "letter": title[:1], "year": "", "summary": "", "genres": "[]", "grp": "", "meta": "{}",
            "added": 0, "count": 1}


def track(ident, item_id, number, kind="audio", **over):
    return {"id": ident, "item_id": item_id, "lib": "musique", "rel": f"{item_id}/{ident}.flac", "kind": kind, "season": None, "disc": None, "number": number, "title": f"Piste {ident}",
            "summary": "", "duration": 60.0, "size": 1, "mtime": 1, "meta": '{"info": {"artist": "Daft"}}', "probe": None, **over}


class PlaylistCase(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.db = Database(root / "hub.db")
        self.db.replace_library("musique", [item("alb1", "Disco"), item("alb2", "Homework"), item("film", "Un film")],
                                [track("a", "alb1", 1), track("b", "alb1", 2), track("c", "alb2", 1), track("d", "alb2", 2), track("e", "alb2", 3), track("v", "film", 1, kind="video")])

    def order(self, pid):
        return playlists._order(self.db, pid)

    def refused(self, call, status=400):
        with self.assertRaises(PlaylistError) as caught:
            call()
        self.assertEqual(caught.exception.status, status)
        return caught.exception.message


class TestCreate(PlaylistCase):
    def test_a_name_is_cleaned_and_required(self):
        self.assertEqual(playlists.clean_name("  Ma   liste "), "Ma liste")
        self.assertEqual(len(playlists.clean_name("x" * 200)), playlists.MAX_NAME)
        for name in ("", "   ", None):
            self.assertIn("donne un nom", self.refused(lambda: playlists.clean_name(name)))

    def test_a_playlist_is_created_with_tracks_that_exist_in_the_order_given(self):
        pid = playlists.create(self.db, "p", "Ma liste", ["c", "a", "inconnue", 3, "v"])
        self.assertEqual(self.order(pid), ["c", "a"])                       # ni la piste inconnue, ni la vidéo

    def test_the_same_name_gives_the_same_playlist_whatever_its_case(self):
        first = playlists.create(self.db, "p", "Ma liste", ["a"])
        again = playlists.create(self.db, "p", "MA LISTE", ["b"])
        self.assertEqual(first, again)
        self.assertEqual(self.order(first), ["a", "b"])
        self.assertNotEqual(playlists.create(self.db, "q", "Ma liste"), first)             # chacun les siennes

    def test_no_valid_track_creates_nothing(self):
        self.assertIn("aucune piste valide", self.refused(lambda: playlists.create(self.db, "p", "Vide", ["v", "x"])))
        self.assertEqual(playlists.listing(self.db, "p"), [])

    def test_an_empty_playlist_may_be_created(self):
        pid = playlists.create(self.db, "p", "Vide")
        self.assertEqual(self.order(pid), [])


class TestShow(PlaylistCase):
    def test_the_listing_is_sorted_by_name_without_regard_to_case_and_summarized(self):
        playlists.create(self.db, "p", "b liste", ["a", "c"])
        playlists.create(self.db, "p", "A liste", ["b", "d", "e"])
        playlists.create(self.db, "q", "Autre", ["a"])
        found = playlists.listing(self.db, "p")
        self.assertEqual([(p["name"], p["count"], p["duration"]) for p in found], [("A liste", 3, 180.0), ("b liste", 2, 120.0)])
        self.assertEqual(found[1]["covers"], ["alb1", "alb2"])
        self.assertEqual(len(found[1]["cover_urls"]), 2)

    def test_the_detail_lists_the_tracks_and_counts_the_missing_ones(self):
        pid = playlists.create(self.db, "p", "Liste", ["a", "c", "e"])
        self.db.execute("DELETE FROM entries WHERE id='c'")
        out = playlists.detail(self.db, "p", pid)
        self.assertEqual([t["id"] for t in out["tracks"]], ["a", "e"])
        self.assertEqual((out["missing"], out["count"]), (1, 2))
        first = out["tracks"][0]
        self.assertEqual((first["title"], first["artist"], first["album"], first["item"], first["duration"], first["number"]), ("Piste a", "Daft", "Disco", "alb1", 60.0, 1))
        self.assertTrue(first["file"] and first["cover"])

    def test_a_playlist_of_someone_else_is_unknown(self):
        pid = playlists.create(self.db, "p", "Liste")
        self.assertIn("inconnue", self.refused(lambda: playlists.detail(self.db, "q", pid), 404))
        self.assertIn("inconnue", self.refused(lambda: playlists.detail(self.db, "p", "absente"), 404))


class TestAdd(PlaylistCase):
    def test_tracks_are_added_at_the_end_and_may_repeat(self):
        pid = playlists.create(self.db, "p", "Liste", ["a"])
        self.assertEqual(playlists.add(self.db, "p", pid, ["b", "a", "x"]), 2)
        self.assertEqual(self.order(pid), ["a", "b", "a"])

    def test_nothing_valid_to_add_is_refused(self):
        pid = playlists.create(self.db, "p", "Liste", ["a"])
        self.assertIn("aucune piste valide", self.refused(lambda: playlists.add(self.db, "p", pid, ["x", 1])))

    def test_the_length_of_a_playlist_is_bounded_and_the_bound_is_allowed(self):
        with mock.patch.object(playlists, "MAX_TRACKS", 4):
            pid = playlists.create(self.db, "p", "Liste", ["a"])
            playlists.add(self.db, "p", pid, ["b", "c", "d"])                                       # 4 : pile la limite
            self.assertEqual(len(self.order(pid)), 4)
            self.assertIn("ne peut pas dépasser 4 pistes", self.refused(lambda: playlists.add(self.db, "p", pid, ["e"])))
            small = playlists.create(self.db, "p", "Autre", ["a", "b"])
            self.assertIn("ne peut pas dépasser", self.refused(lambda: playlists.add(self.db, "p", small, ["c", "d", "e"])))      # 2 + 3 > 4, mais 3 - 2 ne l'est pas
        self.assertEqual(len(self.order(small)), 2)


class TestRemove(PlaylistCase):
    def make(self):
        return playlists.create(self.db, "p", "Liste", ["a", "b", "c"])

    def test_the_first_and_the_last_can_be_removed(self):
        pid = self.make()
        playlists.remove(self.db, "p", pid, 0)
        self.assertEqual(self.order(pid), ["b", "c"])
        playlists.remove(self.db, "p", pid, 1)
        self.assertEqual(self.order(pid), ["b"])

    def test_a_position_outside_the_list_is_refused(self):
        pid = self.make()
        for index in (-1, 3, 99, "0", None, 1.0):
            self.assertIn("position invalide", self.refused(lambda: playlists.remove(self.db, "p", pid, index)), index)
        self.assertEqual(self.order(pid), ["a", "b", "c"])

    def test_the_position_counts_only_the_tracks_that_are_still_shown(self):
        pid = self.make()
        self.db.execute("DELETE FROM entries WHERE id='a'")
        playlists.remove(self.db, "p", pid, 0)                           # la première piste affichée est « b »
        self.assertEqual(self.order(pid), ["a", "c"])
        self.assertIn("position invalide", self.refused(lambda: playlists.remove(self.db, "p", pid, 1)))


class TestMove(PlaylistCase):
    def make(self):
        return playlists.create(self.db, "p", "Liste", ["a", "b", "c", "d"])

    def test_a_track_goes_to_its_new_place(self):
        pid = self.make()
        playlists.move(self.db, "p", pid, 0, 2)
        self.assertEqual(self.order(pid), ["b", "c", "a", "d"])
        playlists.move(self.db, "p", pid, 3, 0)
        self.assertEqual(self.order(pid), ["d", "b", "c", "a"])

    def test_the_ends_of_the_list_are_valid_places_and_beyond_is_not(self):
        pid = self.make()
        playlists.move(self.db, "p", pid, 0, 3)
        self.assertEqual(self.order(pid), ["b", "c", "d", "a"])
        for src, dst in ((4, 0), (0, 4), (-1, 0), (0, -1), ("1", 0), (0, None)):
            self.assertIn("position invalide", self.refused(lambda: playlists.move(self.db, "p", pid, src, dst)), (src, dst))
        self.assertEqual(self.order(pid), ["b", "c", "d", "a"])

    def test_the_tracks_that_are_not_shown_stay_at_the_end(self):
        pid = self.make()
        self.db.execute("DELETE FROM entries WHERE id='b'")
        playlists.move(self.db, "p", pid, 0, 2)                         # affichées : a, c, d
        self.assertEqual(self.order(pid), ["c", "d", "a", "b"])
        self.assertIn("position invalide", self.refused(lambda: playlists.move(self.db, "p", pid, 0, 3)))


class TestRenameDelete(PlaylistCase):
    def test_a_playlist_is_renamed(self):
        pid = playlists.create(self.db, "p", "Ancien")
        playlists.rename(self.db, "p", pid, "  Nouveau  nom ")
        self.assertEqual(playlists.listing(self.db, "p")[0]["name"], "Nouveau nom")
        self.assertIn("donne un nom", self.refused(lambda: playlists.rename(self.db, "p", pid, " ")))
        self.assertIn("inconnue", self.refused(lambda: playlists.rename(self.db, "q", pid, "x"), 404))

    def test_a_playlist_is_deleted_with_its_tracks(self):
        pid = playlists.create(self.db, "p", "Liste", ["a", "b"])
        self.assertIn("inconnue", self.refused(lambda: playlists.delete(self.db, "q", pid), 404))
        playlists.delete(self.db, "p", pid)
        self.assertEqual(playlists.listing(self.db, "p"), [])
        self.assertEqual(self.order(pid), [])


class TestAudioEntries(PlaylistCase):
    def test_only_audio_entries_that_exist_are_found_even_in_a_long_list(self):
        ids = ["a", "v", "x"] + [f"n{n}" for n in range(900)] + ["e", "a"]
        found = playlists.audio_entries(self.db, ids)
        self.assertEqual(sorted(found), ["a", "e"])
        self.assertEqual((found["a"]["album"], found["a"]["album_id"]), ("Disco", "alb1"))


if __name__ == "__main__":
    unittest.main()
