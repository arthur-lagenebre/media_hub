"""Le scan des films et des séries : ce que disent les tags Matroska, ce que dit le nom des fichiers, et ce qu'on reprend du scan précédent."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tests import fixtures
from hub import config

fixtures.REPO  # noqa: B018  (rend `libraries` importable)
config.use_media_library(fixtures.REPO)

from hub.db import make_id  # noqa: E402
from hub.scan import video  # noqa: E402
from tests.fixtures import episode_blocks, movie_blocks, write_mkv  # noqa: E402


class TempCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def touch(self, rel, blocks=(), minutes=95.5, mtime=None):
        path = write_mkv(self.root / rel, blocks, minutes=minutes)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path


class TestTags(unittest.TestCase):
    def test_numbers_are_read_as_integers_when_they_can(self):
        for text, wanted in (("3", 3), ("3.0", 3), ("1,5", 1.5), ("2.25", 2.25), (7, 7), ("abc", None), ("", None)):
            self.assertEqual(video._number(text), wanted, text)

    def test_an_actor_and_the_role_in_parentheses(self):
        self.assertEqual(video._split_actor("Tom Hanks (Forrest)"), ("Tom Hanks", "Forrest"))
        self.assertEqual(video._split_actor("Tom Hanks"), ("Tom Hanks", ""))
        self.assertEqual(video._split_actor("Tom (le grand) (Forrest)  "), ("Tom (le grand)", "Forrest"))
        self.assertEqual(video._split_actor("Tom ()"), ("Tom", ""))

    def test_a_role_that_has_parentheses_of_its_own_is_kept_whole(self):
        self.assertEqual(video._split_actor("Tom Hanks (Woody (voice))"), ("Tom Hanks", "Woody (voice)"))
        self.assertEqual(video._split_actor('Dennis Quaid (Charlie Wessler (segment "The Pitch"))'), ("Dennis Quaid", 'Charlie Wessler (segment "The Pitch")'))
        self.assertEqual(video._split_actor("Tom (le grand) (Woody (voice))  "), ("Tom (le grand)", "Woody (voice)"))

    def test_unbalanced_parentheses_leave_the_whole_text_as_the_name(self):
        self.assertEqual(video._split_actor("Tom (Woody"), ("Tom (Woody", ""))
        self.assertEqual(video._split_actor("Tom Woody)"), ("Tom Woody)", ""))
        self.assertEqual(video._split_actor("Tom (Woody))"), ("Tom (Woody))", ""))

    def test_a_tag_is_found_by_level_and_name_and_must_not_be_empty(self):
        tags = [(50, "TITLE", ""), (70, "TITLE", "Saga"), (50, "TITLE", "Film"), (50, "OTHER", "x"), (50, "TITLE", "Autre")]
        self.assertEqual(video._first(tags, 50, "TITLE"), "Film")
        self.assertEqual(video._first(tags, 70, "TITLE"), "Saga")
        self.assertEqual(video._first(tags, 60, "TITLE"), "")
        self.assertEqual(video._first(tags, 50, "ABSENT"), "")
        self.assertEqual(video._all(tags, 50, "TITLE"), ["Film", "Autre"])
        self.assertEqual(video._all(tags, 50, "ABSENT"), [])

    def test_the_tags_of_a_film(self):
        info = video._tag_info([
            (50, "TITLE", "Inception"), (50, "SUMMARY", "Rêves."), (50, "DATE_RELEASED", "2010-07-21"), (50, "TMDB", "movie/27205"), (50, "GENRE", "Science-Fiction, ,Action"),
            (50, "DIRECTOR", "Nolan"), (50, "WRITTEN_BY", "Nolan"), (50, "ACTOR", "Leo (Cobb)"), (50, "ACTOR", "Elliot"), (50, "PART_NUMBER", "2"),
            (70, "TITLE", "Saga"), (70, "PART_NUMBER", "1"), (60, "PART_NUMBER", "3"), (60, "TITLE", "Saison trois")])
        self.assertEqual(info, {
            "title": "Inception", "synopsis": "Rêves.", "date": "2010-07-21", "tmdb": "movie/27205", "genres": ["Science-Fiction", "Action"], "directors": ["Nolan"], "writers": ["Nolan"],
            "actors": [{"name": "Leo", "role": "Cobb"}, {"name": "Elliot", "role": ""}], "number": 2, "collection": "Saga", "collection_part": 1, "season": 3, "season_title": "Saison trois", "has_tags": True})

    def test_the_synopsis_is_preferred_to_the_summary_and_what_is_empty_is_left_out(self):
        self.assertEqual(video._tag_info([(50, "SYNOPSIS", "a"), (50, "SUMMARY", "b")])["synopsis"], "a")
        self.assertEqual(video._tag_info([(50, "TITLE", "t")]), {"title": "t", "has_tags": True})
        self.assertEqual(video._tag_info([]), {"has_tags": True})


class TestPersonIds(unittest.TestCase):
    """Le tag fils `X/TMDB` (« person/1234 ») qui suit DIRECTOR, WRITTEN_BY ou ACTOR est l'identifiant TMDB de cette personne."""

    def ids(self, tags):
        return video._tag_info(tags).get("person_ids")

    def test_each_child_belongs_to_the_person_before_it(self):
        info = video._tag_info([
            (50, "DIRECTOR", "Nolan"), (50, "DIRECTOR/TMDB", "person/525"), (50, "WRITTEN_BY", "Jonathan Nolan"), (50, "WRITTEN_BY/TMDB", "person/1"),
            (50, "ACTOR", "Leo (Cobb)"), (50, "ACTOR/TMDB", "person/6193"), (50, "ACTOR", "Elliot"), (50, "ACTOR/TMDB", "person/12")])
        self.assertEqual(info["person_ids"], {"Nolan": 525, "Jonathan Nolan": 1, "Leo": 6193, "Elliot": 12})          # entiers ; le rôle n'est pas dans le nom
        self.assertEqual((info["directors"], info["writers"], info["actors"]),
                         (["Nolan"], ["Jonathan Nolan"], [{"name": "Leo", "role": "Cobb"}, {"name": "Elliot", "role": ""}]))          # les listes ne changent pas

    def test_a_person_without_a_child_has_no_id_and_does_not_take_the_next_ones(self):
        tags = [(50, "ACTOR", "Sans"), (50, "ACTOR", "Avec"), (50, "ACTOR/TMDB", "person/7"), (50, "ACTOR", "Autre sans")]
        self.assertEqual(self.ids(tags), {"Avec": 7})

    def test_two_people_without_an_id_in_the_same_file_leave_no_ids_at_all(self):
        info = video._tag_info([(50, "ACTOR", "Un"), (50, "ACTOR", "Deux")])
        self.assertNotIn("person_ids", info)
        self.assertEqual(len(info["actors"]), 2)

    def test_what_is_not_a_person_id_next_to_its_father_is_ignored(self):
        for child in ("person/abc", "movie/5", "5", "person/", "person/12/3", ""):
            self.assertIsNone(self.ids([(50, "ACTOR", "Un"), (50, "ACTOR/TMDB", child)]), child)
        self.assertIsNone(self.ids([(50, "DIRECTOR", "Un"), (50, "ACTOR/TMDB", "person/5")]))          # le fils d'un autre tag
        self.assertIsNone(self.ids([(50, "ACTOR/TMDB", "person/5"), (50, "ACTOR", "Un")]))          # avant son père
        self.assertIsNone(self.ids([(70, "ACTOR", "Un"), (70, "ACTOR/TMDB", "person/5")]))          # seul le niveau du film compte
        for tag in ("ACTOR", "DIRECTOR"):
            self.assertIsNone(self.ids([(50, tag, ""), (50, f"{tag}/TMDB", "person/5")]), tag)          # une personne sans nom n'a pas d'identifiant

    def test_the_film_tmdb_tag_is_not_a_person(self):
        info = video._tag_info([(50, "TMDB", "movie/27205"), (50, "DIRECTOR", "Nolan"), (50, "DIRECTOR/TMDB", "person/525")])
        self.assertEqual((info["tmdb"], info["person_ids"]), ("movie/27205", {"Nolan": 525}))

    def test_an_actor_with_a_nested_role_is_filed_under_the_name_alone(self):
        tags = [(50, "ACTOR", "Tom Hanks (Woody (voice))"), (50, "ACTOR/TMDB", "person/31")]
        info = video._tag_info(tags)
        self.assertEqual(info["person_ids"], {"Tom Hanks": 31})
        self.assertEqual(info["actors"], [{"name": "Tom Hanks", "role": "Woody (voice)"}])

    def test_the_same_person_in_two_roles_keeps_her_id(self):
        tags = [(50, "DIRECTOR", "Eastwood"), (50, "DIRECTOR/TMDB", "person/190"), (50, "ACTOR", "Eastwood (Kowalski)"), (50, "ACTOR/TMDB", "person/190")]
        self.assertEqual(self.ids(tags), {"Eastwood": 190})


class TestReadFile(TempCase):
    def test_the_ids_of_a_real_file_are_read(self):
        path = self.touch("a.mkv", movie_blocks("Inception", directors=["Nolan"], actors=["Leo (Cobb)", "Sans"], person_ids={"Nolan": 525, "Leo (Cobb)": 6193}))
        info, _ = video.read_file(path)
        self.assertEqual(info["person_ids"], {"Nolan": 525, "Leo": 6193})
        self.assertEqual(info["actors"], [{"name": "Leo", "role": "Cobb"}, {"name": "Sans", "role": ""}])

    def test_a_tagged_file_gives_its_tags_and_its_duration(self):
        path = self.touch("a.mkv", movie_blocks("Inception", "Rêves.", "2010-07-21", "movie/27205", "Action", ["Nolan"], ["Leo (Cobb)"], collection="Saga", part=2), minutes=148)
        info, seconds = video.read_file(path)
        self.assertEqual((info["title"], info["collection"], info["collection_part"], info["has_tags"], seconds), ("Inception", "Saga", 2, True, 8880.0))

    def test_the_extension_is_read_whatever_its_case(self):
        info, seconds = video.read_file(self.touch("B.MKV", movie_blocks("Titre"), minutes=10))
        self.assertEqual((info["title"], seconds), ("Titre", 600.0))

    def test_a_file_without_tags_has_a_duration_only(self):
        self.assertEqual(video.read_file(self.touch("a.mkv", minutes=2)), ({}, 120.0))
        self.assertEqual(video.read_file(self.touch("zero.mkv", minutes=0)), ({}, 0))

    def test_the_actors_keep_the_order_of_the_file(self):
        actors = [f"Acteur {n} (Rôle {n})" for n in range(1, 7)]
        info, _ = video.read_file(self.touch("a.mkv", movie_blocks("Titre", actors=actors)))
        self.assertEqual([a["name"] for a in info["actors"]], [f"Acteur {n}" for n in range(1, 7)])

    def test_what_is_not_a_mkv_is_not_read(self):
        path = self.root / "a.mp4"
        path.write_bytes(b"x")
        self.assertEqual(video.read_file(path), ({}, 0))

    def test_a_mkv_that_cannot_be_read_is_marked_so_to_be_read_again(self):
        path = self.root / "a.mkv"
        path.write_bytes(b"ce n'est pas du matroska")
        self.assertEqual(video.read_file(path), ({"unreadable": True}, 0))
        self.assertEqual(video.read_file(self.root / "absent.mkv"), ({"unreadable": True}, 0))


class TestReader(TempCase):
    def known(self, rel, info, duration=7.0, **row):
        stat = (self.root / rel).stat()
        return {rel: {"size": stat.st_size, "mtime": int(stat.st_mtime), "meta": json.dumps({"info": info}), "duration": duration, **row}}

    def test_a_missing_file_is_nothing(self):
        reader = video.Reader("films", {})
        self.assertIsNone(reader(self.root / "absent.mkv", "absent.mkv"))
        self.assertEqual((reader.read, reader.reused), (0, 0))

    def test_a_new_file_is_read(self):
        path = self.touch("a.mkv", movie_blocks("Titre"), minutes=1)
        reader = video.Reader("films", {})
        size, mtime, info, duration = reader(path, "a.mkv")
        self.assertEqual((size, mtime, info["title"], duration), (path.stat().st_size, int(path.stat().st_mtime), "Titre", 60.0))
        self.assertEqual((reader.read, reader.reused), (1, 0))

    def test_a_file_that_did_not_change_is_taken_from_the_previous_scan(self):
        path = self.touch("a.mkv", movie_blocks("Titre"))
        reader = video.Reader("films", self.known("a.mkv", {"title": "Ancien"}, duration=42.0))
        self.assertEqual(reader(path, "a.mkv")[2:], ({"title": "Ancien"}, 42.0))
        self.assertEqual((reader.read, reader.reused), (0, 1))

    def test_a_file_kept_by_an_older_reading_is_read_again_though_it_did_not_change(self):
        """Une nouvelle lecture des tags (ici : les identifiants des personnes) ne s'applique qu'aux fichiers relus : sans la version de lecture, un fichier inchangé garderait l'ancien résultat pour toujours."""
        path = self.touch("a.mkv", movie_blocks("Titre", directors=["Nolan"], person_ids={"Nolan": 525}))
        reader = video.Reader("films", self.known("a.mkv", {"title": "Ancien"}), version=2)
        self.assertEqual(reader(path, "a.mkv")[2]["person_ids"], {"Nolan": 525})
        self.assertEqual((reader.read, reader.reused), (1, 0))

    def test_a_file_kept_by_the_current_reading_is_not_read_again(self):
        path = self.touch("a.mkv", movie_blocks("Titre"))
        known = self.known("a.mkv", {"title": "Ancien"})
        known["a.mkv"]["meta"] = json.dumps({"info": {"title": "Ancien"}, "read": 2})
        reader = video.Reader("films", known, version=2)
        self.assertEqual(reader(path, "a.mkv")[2], {"title": "Ancien"})
        self.assertEqual((reader.read, reader.reused), (0, 1))

    def test_a_file_read_before_the_nested_roles_fix_is_read_again_by_the_current_reading(self):
        """Les fichiers déjà scannés gardaient « Tom Hanks (Woody (voice)) » comme nom : sans changement de version, ils ne seraient corrigés que s'ils étaient réécrits."""
        path = self.touch("a.mkv", movie_blocks("Titre", actors=["Tom Hanks (Woody (voice))"], person_ids={"Tom Hanks (Woody (voice))": 31}))
        known = self.known("a.mkv", {"actors": [{"name": "Tom Hanks (Woody (voice))", "role": ""}]})
        known["a.mkv"]["meta"] = json.dumps({"info": {"actors": [{"name": "Tom Hanks (Woody (voice))", "role": ""}]}, "read": 2})
        reader = video.Reader("films", known, version=video.MOVIE_READ)
        info = reader(path, "a.mkv")[2]
        self.assertEqual((info["actors"], info["person_ids"]), ([{"name": "Tom Hanks", "role": "Woody (voice)"}], {"Tom Hanks": 31}))
        self.assertEqual((reader.read, reader.reused), (1, 0))

    def test_a_reading_newer_than_ours_is_not_taken_either(self):
        path = self.touch("a.mkv", movie_blocks("Titre"))
        known = self.known("a.mkv", {"title": "Futur"})
        known["a.mkv"]["meta"] = json.dumps({"info": {"title": "Futur"}, "read": 3})
        self.assertEqual(video.Reader("films", known, version=2)(path, "a.mkv")[2]["title"], "Titre")          # retour à une ancienne version du hub : on relit

    def test_a_file_that_changed_is_read_again(self):
        path = self.touch("a.mkv", movie_blocks("Titre"))
        for change in ({"size": 1}, {"mtime": 1}):
            reader = video.Reader("films", self.known("a.mkv", {"title": "Ancien"}, **change))
            self.assertEqual(reader(path, "a.mkv")[2]["title"], "Titre", change)
            self.assertEqual((reader.read, reader.reused), (1, 0), change)

    def test_a_reading_that_failed_or_was_never_kept_is_done_again(self):
        path = self.touch("a.mkv", movie_blocks("Titre"))
        reader = video.Reader("films", self.known("a.mkv", {"unreadable": True}))
        self.assertEqual(reader(path, "a.mkv")[2]["title"], "Titre")
        row = self.known("a.mkv", {})["a.mkv"]
        reader = video.Reader("films", {"a.mkv": {**row, "meta": "{}"}})
        self.assertEqual(reader(path, "a.mkv")[2]["title"], "Titre")
        self.assertEqual(reader.read, 1)


class TestNames(unittest.TestCase):
    def test_the_pinned_tmdb_id_is_not_part_of_a_title(self):
        self.assertEqual(video.clean_name("Dark {tmdb-12}"), "Dark")
        self.assertEqual(video.clean_name("Dark"), "Dark")
        self.assertEqual(video.clean_name("  Dark  "), "Dark")
        self.assertEqual(video.clean_name("   "), "   ")                         # rien ne reste : le nom tel quel

    def test_an_episode_title_loses_its_number_and_its_separators(self):
        for name, title in (("S01E02 - Le titre.mkv", "Le titre"), ("s1 e2.Le_titre.mkv", "Le titre"), ("03.Titre_du.film.mkv", "Titre du film"), ("12.mkv", "12"),
                            ("Texte.mkv", "Texte"), ("1 – Un.mkv", "Un"), ("123 Trois.mkv", "Trois")):
            self.assertEqual(video.episode_title(Path(name)), title, name)

    def test_a_season_is_the_folders_then_the_file_name_then_the_tag_then_the_first(self):
        path = Path("Loki.S02E03.mkv")
        self.assertEqual(video._season_of(path, 4, {"season": 9}), 4)
        self.assertEqual(video._season_of(path, None, {"season": 9}), 2)
        self.assertEqual(video._season_of(Path("Loki 3.mkv"), None, {"season": 9}), 9)
        self.assertEqual(video._season_of(Path("Loki 3.mkv"), None, {}), 1)
        self.assertEqual(video._season_of(Path("Loki 3.mkv"), 0, {}), 0)               # la saison 0 (les spéciaux) existe


class TestScanMovies(TempCase):
    def setUp(self):
        super().setUp()
        self.touch("Inception (2010)/Inception (2010).mkv", movie_blocks("Inception", "Rêves.", "2010-07-21", "movie/27205", "Science-Fiction, Action", ["Nolan"], ["Leo (Cobb)"], collection="Nolan", part=2),
                   minutes=148, mtime=1000)
        (self.root / "Inception (2010)" / "poster.jpg").write_bytes(b"")
        self.touch("Deux (2001)/Deux CD1.mkv", movie_blocks("Deux"), minutes=10, mtime=2000)
        self.touch("Deux (2001)/Deux CD2.mkv", movie_blocks("Deux"), minutes=20, mtime=3000)
        self.touch("_DC/1 - Man of Steel.mkv", minutes=90, mtime=4000)
        (self.root / "Casse (2000)").mkdir()
        (self.root / "Casse (2000)" / "Casse (2000).mkv").write_bytes(b"pas un mkv")
        (self.root / "Vide (1990)").mkdir()
        self.lib = config.Library("films", "Films", "movies", self.root)

    def scan(self, known=None, enriched=None):
        items, entries, counts = video.scan_movies(self.lib, known or {}, enriched)
        return {i["title"]: i for i in items}, {e["rel"]: e for e in entries}, counts

    def test_the_films_scanned_before_the_ids_were_read_are_read_again_once(self):
        self.touch("Tagué (2005)/Tagué (2005).mkv", movie_blocks("Tagué", directors=["Nolan"], person_ids={"Nolan": 525}), mtime=5000)
        _, entries, _ = video.scan_movies(self.lib, {})
        old = {e["rel"]: {**e, "meta": json.dumps({"info": json.loads(e["meta"])["info"]})} for e in entries}          # ce que l'ancien scan avait gardé : l'info, sans version de lecture
        items, entries, counts = video.scan_movies(self.lib, old)
        self.assertEqual((counts.reused, counts.read), (0, len(entries)))                                              # tous les films sont relus, même inchangés
        self.assertEqual(json.loads(next(i for i in items if i["title"] == "Tagué")["meta"])["person_ids"], {"Nolan": 525})
        _, _, again = video.scan_movies(self.lib, {e["rel"]: e for e in entries})
        self.assertEqual(again.reused + again.read, len(entries))
        self.assertGreater(again.reused, 0)                                                                            # une fois : le scan suivant reprend le résultat
        self.assertLess(again.read, len(entries))

    def test_a_film_keeps_the_ids_of_its_people_and_a_film_without_any_has_none(self):
        self.touch("Tagué (2005)/Tagué (2005).mkv", movie_blocks("Tagué", directors=["Nolan"], actors=["Leo (Cobb)"], person_ids={"Nolan": 525, "Leo (Cobb)": 6193}), mtime=5000)
        items = self.scan()[0]
        self.assertEqual(json.loads(items["Tagué"]["meta"])["person_ids"], {"Nolan": 525, "Leo": 6193})
        self.assertEqual(json.loads(items["Inception"]["meta"])["person_ids"], {})          # comme `directors` : la clé est là, vide

    def test_the_rating_kept_by_the_enrichment_comes_back_and_nothing_else(self):
        rating = {"tmdb": 27205, "vote": 8.4, "votes": 37000, "at": 1}
        kept = {make_id("films", "Inception (2010)/Inception (2010).mkv"): {"tmdb_rating": rating, "tmdb": "movie/1", "summary": "Autre."}}
        item = self.scan(enriched=kept)[0]["Inception"]
        meta = json.loads(item["meta"])
        self.assertEqual((meta["tmdb_rating"], meta["tmdb"], item["summary"]), (rating, "movie/27205", "Rêves."))          # les tags restent la référence
        self.assertNotIn("tmdb_rating", json.loads(self.scan()[0]["Inception"]["meta"]))

    def test_the_rating_of_another_film_is_not_taken_back(self):
        kept = {make_id("films", "Inception (2010)/Inception (2010).mkv"): {"tmdb_rating": {"tmdb": 1, "vote": 2.0, "votes": 900, "at": 1}}}       # réétiqueté depuis
        self.assertNotIn("tmdb_rating", json.loads(self.scan(enriched=kept)[0]["Inception"]["meta"]))

    def test_a_tagged_film_is_described_by_its_tags(self):
        item = self.scan()[0]["Inception"]
        meta = json.loads(item["meta"])
        self.assertEqual((item["type"], item["year"], item["summary"], item["grp"], item["count"], item["added"]), ("movie", "2010", "Rêves.", "Nolan", 1, 1000))
        self.assertEqual(json.loads(item["genres"]), ["Science-Fiction", "Action"])
        self.assertEqual(item["id"], make_id("films", "Inception (2010)/Inception (2010).mkv"))
        self.assertEqual(meta, {"tmdb": "movie/27205", "directors": ["Nolan"], "writers": [], "actors": [{"name": "Leo", "role": "Cobb"}], "person_ids": {}, "date": "2010-07-21", "part": 2,
                                "image": "Inception (2010)/poster.jpg", "tagged": True, "folder": "Inception (2010)", "duration": 8880.0})

    def test_a_file_without_tags_keeps_the_name_of_its_folder_and_its_year(self):
        item = self.scan()[0]["DC"]
        meta = json.loads(item["meta"])
        self.assertEqual((item["year"], item["summary"], item["grp"], meta["tagged"], meta["folder"], meta["image"], meta["duration"], meta["part"]), ("", "", "", False, "_DC", "", 5400.0, None))
        self.assertEqual(self.scan()[0]["Casse"]["year"], "2000")

    def test_a_film_in_several_files_is_one_item_with_a_part_per_file(self):
        items, entries, _ = self.scan()
        item = items["Deux"]
        self.assertEqual((item["count"], item["added"], json.loads(item["meta"])["duration"]), (2, 3000, 1800.0))
        self.assertEqual(item["id"], make_id("films", "Deux (2001)/Deux (2001)/Deux CD1.mkv"))
        first, second = entries["Deux (2001)/Deux CD1.mkv"], entries["Deux (2001)/Deux CD2.mkv"]
        self.assertEqual((first["title"], first["number"], first["duration"], first["mtime"], first["kind"]), ("Deux - partie 1", 1, 600.0, 2000, "video"))
        self.assertEqual((second["title"], second["number"], second["duration"], second["mtime"]), ("Deux - partie 2", 2, 1200.0, 3000))

    def test_a_single_file_has_the_title_of_the_film(self):
        entry = self.scan()[1]["Inception (2010)/Inception (2010).mkv"]
        self.assertEqual((entry["title"], entry["number"], entry["item_id"]), ("Inception", 1, make_id("films", "Inception (2010)/Inception (2010).mkv")))
        self.assertEqual(json.loads(entry["meta"])["info"]["title"], "Inception")

    def test_a_folder_without_a_film_is_not_an_item(self):
        self.assertEqual(sorted(self.scan()[0]), ["Casse", "DC", "Deux", "Inception"])

    def test_a_file_that_cannot_be_read_is_still_listed(self):
        items, entries, _ = self.scan()
        self.assertEqual(json.loads(entries["Casse (2000)/Casse (2000).mkv"]["meta"]), {"info": {"unreadable": True}, "read": video.MOVIE_READ})
        self.assertEqual(items["Casse"]["count"], 1)

    def test_counts_tell_what_was_read_and_what_was_reused(self):
        _, entries, counts = self.scan()
        self.assertEqual((counts.read, counts.reused), (5, 0))
        counts = self.scan({rel: entry for rel, entry in entries.items()})[2]
        self.assertEqual((counts.read, counts.reused), (1, 4))            # le fichier qu'on n'a pas pu lire se relit toujours

    def test_a_film_at_the_root_has_no_folder(self):
        self.touch("Seul (2005).mkv", movie_blocks("Seul"))
        meta = json.loads(self.scan()[0]["Seul"]["meta"])
        self.assertEqual((meta["folder"], meta["image"]), ("", ""))


class TestFindShows(TempCase):
    def test_a_folder_with_seasons_or_videos_is_a_show_and_others_only_store_shows(self):
        self.touch("Dark/Saison 1/01 - A.mkv")
        self.touch("_Marvel/Loki/Loki.S01E01.mkv")
        self.touch("_Marvel/Plus/Autre/Saison 1/01.mkv")
        self.touch("Plat/01 - Un.mkv")
        found = [(path.relative_to(self.root).as_posix(), container) for path, container in video.find_shows(self.root)]
        self.assertEqual(found, [("_Marvel/Loki", "_Marvel"), ("_Marvel/Plus/Autre", "_Marvel"), ("Dark", ""), ("Plat", "")])           # par nom, sans tenir compte de la casse

    def test_special_folders_and_deep_folders_are_skipped(self):
        self.touch("#recycle/Série/01.mkv")
        self.touch("@eadir/Série/01.mkv")
        self.touch(".cache/Série/01.mkv")
        self.touch("__tmp/Série/01.mkv")
        self.touch("Extras/Série/01.mkv")
        self.touch("a/b/c/Trop profond/01.mkv")
        self.touch("a/b/Assez/01.mkv")
        found = [path.relative_to(self.root).as_posix() for path, _ in video.find_shows(self.root)]
        self.assertEqual(found, ["a/b/Assez"])

    def test_extras_and_other_files_are_not_episodes(self):
        self.touch("S/B.mkv")
        self.touch("S/a.mkv")
        self.touch("S/Dark trailer.mkv")
        (self.root / "S" / "notes.txt").write_text("x")
        (self.root / "S" / "dossier.mkv").mkdir()
        self.assertEqual([p.name for p in video._videos(self.root / "S")], ["a.mkv", "B.mkv"])
        self.assertEqual(video._videos(self.root / "absent"), [])

    def test_subfolders_are_listed_without_the_hidden_or_extra_ones(self):
        for name in ("b", "A", "#x", "@y", ".z", "__w", "Extras", "Bonus"):
            (self.root / name).mkdir()
        (self.root / "fichier").write_text("x")
        self.assertEqual([p.name for p in video._subdirs(self.root)], ["A", "b"])
        self.assertEqual(video._subdirs(self.root / "absent"), [])


class TestScanShows(TempCase):
    def setUp(self):
        super().setUp()
        for n, (title, text, date) in enumerate((("Pilote", "Début.", "2017-12-01"), ("Suite", "Après.", "2018-01-02")), 1):
            self.touch(f"Dark/Saison 1/{n:02d} - {title}.mkv", episode_blocks("Dark", 1, n, title, text, date), minutes=50, mtime=1000 * n)
        self.touch("Dark/Saison 2/01 - Retour.mkv", episode_blocks("Dark", 2, 1, "Retour", "", "2019-06-21"), minutes=52, mtime=500)
        (self.root / "Dark" / "folder.png").write_bytes(b"")
        (self.root / "Dark" / "Saison 1" / "poster.jpg").write_bytes(b"")
        self.touch("Dark/Saison 1/Dark trailer.mkv")
        self.touch("_Marvel/Loki/Loki.S02E03.mkv", mtime=4000)
        self.touch("_Marvel/Loki/Loki 5.mkv", mtime=3000)
        self.touch("Plat (2020) {tmdb-12}/04 - Quatre.mkv", episode_blocks("Plat", 3, 4, "Quatre"))
        self.touch("Plat (2020) {tmdb-12}/Sans.mkv")
        (self.root / "Vide").mkdir()
        self.lib = config.Library("series", "Séries", "shows", self.root)

    def scan(self, known=None, enriched=None):
        items, entries, counts = video.scan_shows(self.lib, known or {}, enriched)
        return {i["title"]: i for i in items}, [(e["rel"], e) for e in entries], counts

    def test_the_episodes_are_not_read_again_for_the_ids_of_the_people(self):
        """Les séries n'utilisent pas ces identifiants : relire des milliers d'épisodes pour rien ne se justifie pas."""
        _, entries, _ = video.scan_shows(self.lib, {})
        known = {e["rel"]: e for e in entries}
        self.assertTrue(all("read" not in json.loads(e["meta"]) for e in entries))
        _, _, counts = video.scan_shows(self.lib, known)
        self.assertGreater(counts.reused, 0)
        self.assertEqual(counts.reused + counts.read, len(entries))

    def test_a_show_does_not_keep_the_ids_of_the_people_of_its_episodes(self):
        self.touch("Casting/Saison 1/01 - Un.mkv", [(70, [("TITLE", "Casting")]), (60, [("PART_NUMBER", 1)]), (50, [("TITLE", "Un"), ("PART_NUMBER", 1), ("ACTOR", "Leo (Cobb)"), ("ACTOR/TMDB", "person/6193")])])
        item = self.scan()[0]["Casting"]
        self.assertNotIn("person_ids", json.loads(item["meta"]))

    def test_a_show_is_an_item_with_what_its_episodes_say(self):
        item = self.scan()[0]["Dark"]
        meta = json.loads(item["meta"])
        self.assertEqual((item["type"], item["year"], item["grp"], item["count"], item["summary"], item["added"]), ("show", "2017", "", 3, "", 2000))
        self.assertEqual(item["id"], make_id("series", "Dark"))
        self.assertEqual(meta, {"tmdb_pinned": "", "image": "Dark/folder.png", "seasons": [1, 2], "season_images": {"1": "Dark/Saison 1/poster.jpg"}, "folder": "Dark", "tagged": True})

    def test_a_show_in_a_storing_folder_is_grouped_by_it(self):
        item = self.scan()[0]["Loki"]
        meta = json.loads(item["meta"])
        self.assertEqual((item["grp"], item["year"], item["added"], meta["tagged"], meta["image"], meta["folder"]), ("_Marvel", "", 4000, False, "", "_Marvel/Loki"))

    def test_the_pinned_id_leaves_the_title_and_goes_to_the_meta(self):
        items, _, _ = self.scan()
        meta = json.loads(items["Plat"]["meta"])
        self.assertEqual((meta["tmdb_pinned"], meta["seasons"], meta["folder"]), ("12", [1, 3], "Plat (2020) {tmdb-12}"))

    def test_the_enrichment_of_a_previous_run_gives_its_title_year_and_the_rest(self):
        extra = {"title": "Sombre", "year": "2018", "summary": "Voyage.", "genres": ["Drame"], "poster": "p.jpg", "tmdb": 70523}
        items, _, _ = self.scan(enriched={make_id("series", "Dark"): extra})
        item = items["Sombre"]
        self.assertEqual((item["year"], item["summary"], json.loads(item["genres"])), ("2018", "Voyage.", ["Drame"]))
        meta = json.loads(item["meta"])
        self.assertEqual((meta["poster"], meta["tmdb"]), ("p.jpg", 70523))
        self.assertNotIn("title", meta)
        self.assertNotIn("summary", meta)
        self.assertNotIn("genres", meta)
        self.assertNotIn("year", meta)

    def test_a_show_is_named_after_its_collection_tag_before_its_folder(self):
        self.touch("Dossier/01.mkv", [(70, [("TITLE", "Vrai nom")]), (50, [("TITLE", "Ep")])])
        self.assertIn("Vrai nom", self.scan()[0])

    def test_episodes_go_by_season_then_number_then_name(self):
        dark = [(e["season"], e["number"], e["title"]) for rel, e in self.scan()[1] if rel.startswith("Dark/")]
        self.assertEqual(dark, [(1, 1, "Pilote"), (1, 2, "Suite"), (2, 1, "Retour")])

    def test_the_season_comes_before_the_number_before_the_name_and_no_number_is_last(self):
        for name in ("b - S01E01.mkv", "a - S02E01.mkv", "x - S01E10.mkv", "y - S01E02.mkv", "A sans numéro.mkv"):
            self.touch(f"Tri/{name}")
        self.assertEqual([rel.split("/")[1] for rel, _ in self.scan()[1] if rel.startswith("Tri/")],
                         ["b - S01E01.mkv", "y - S01E02.mkv", "x - S01E10.mkv", "A sans numéro.mkv", "a - S02E01.mkv"])

    def test_an_episode_without_tags_takes_its_season_and_number_from_its_name(self):
        by_rel = dict(self.scan()[1])
        episode = by_rel["_Marvel/Loki/Loki.S02E03.mkv"]
        self.assertEqual((episode["title"], episode["season"], episode["number"], episode["duration"], episode["kind"], episode["summary"]), ("Loki S02E03", 2, 3, 5730.0, "video", ""))
        other = by_rel["_Marvel/Loki/Loki 5.mkv"]
        self.assertEqual((other["title"], other["season"], other["number"]), ("Loki 5", 1, None))

    def test_a_tagged_episode_gives_its_title_synopsis_and_number(self):
        episode = dict(self.scan()[1])["Dark/Saison 1/01 - Pilote.mkv"]
        self.assertEqual((episode["title"], episode["summary"], episode["season"], episode["number"], episode["duration"], episode["mtime"], episode["item_id"]),
                         ("Pilote", "Début.", 1, 1, 3000.0, 1000, make_id("series", "Dark")))

    def test_the_episodes_of_a_flat_show_take_the_season_of_their_tags(self):
        by_rel = dict(self.scan()[1])
        self.assertEqual((by_rel["Plat (2020) {tmdb-12}/04 - Quatre.mkv"]["season"], by_rel["Plat (2020) {tmdb-12}/04 - Quatre.mkv"]["number"]), (3, 4))
        self.assertEqual((by_rel["Plat (2020) {tmdb-12}/Sans.mkv"]["season"], by_rel["Plat (2020) {tmdb-12}/Sans.mkv"]["number"]), (1, None))

    def test_a_folder_without_episodes_is_not_a_show(self):
        self.assertNotIn("Vide", self.scan()[0])

    def test_counts_tell_what_was_read_and_what_was_reused(self):
        items, entries, counts = self.scan()
        self.assertEqual((counts.read, counts.reused), (7, 0))
        counts = self.scan(known={rel: e for rel, e in entries})[2]
        self.assertEqual((counts.read, counts.reused), (0, 7))


if __name__ == "__main__":
    unittest.main()
