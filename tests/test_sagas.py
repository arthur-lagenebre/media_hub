"""sagas.json : les sagas qu'on compose soi-même, relues quand le fichier change, écrites d'un bloc, et refusées avec un message quand l'éditeur se trompe."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import sagas
from hub.sagas import SagaError, Sagas


class TempCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.path = self.root / "sagas.json"

    def write(self, data):
        self.path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def refused(self, call, status=400):
        with self.assertRaises(SagaError) as caught:
            call()
        self.assertEqual(caught.exception.status, status)
        return caught.exception.message


class TestFilmId(unittest.TestCase):
    def test_a_tag_or_a_number_gives_the_identifier(self):
        for value, wanted in (("movie/27205", 27205), ("MOVIE/5", 5), (" 42 ", 42), (7, 7), ("123456789012", 123456789012)):
            self.assertEqual(sagas.film_id(value), wanted, value)

    def test_anything_else_is_nothing(self):
        for value in (None, "", True, False, "tv/5", "abc", "1.5", "1234567890123", [1], "movie/"):
            self.assertIsNone(sagas.film_id(value), value)


class TestParse(unittest.TestCase):
    def test_what_cannot_be_read_is_dropped_quietly(self):
        text = json.dumps({"sagas": [{"name": "  Trilogie   Cornetto "}, {"name": "trilogie cornetto", "films": [1]}, {"name": "", "films": [2]}, "x", {"name": "B", "films": ["movie/3", "?", 3, 4, True]},
                                     {"name": "C", "films": "pas une liste"}], "ignore": [9, "movie/9", "?", 10]})
        self.assertEqual(sagas.parse(text), [("Trilogie Cornetto", []), ("B", [3, 4]), ("C", [])])
        self.assertEqual(sagas.parse_ignore(text), [9, 10])

    def test_a_file_that_is_not_a_json_object_has_no_sagas(self):
        for text in ("", "pas du json", "[]", '{"sagas": 3}', '{"ignore": "x"}', "[" * 100000):
            self.assertEqual(sagas.parse(text), [], text[:20])
            self.assertEqual(sagas.parse_ignore(text), [], text[:20])

    def test_a_file_is_damaged_only_when_writing_over_it_would_lose_something(self):
        for text in ("", "  \n"):
            self.assertFalse(sagas.damaged(text), repr(text))
        for text in ('{"sagas": []}', '{"ignore": []}', "{}", '{"autre": 1}'):
            self.assertFalse(sagas.damaged(text), text)
        for text in ("pas du json", "[]", '{"sagas": 3}', '{"ignore": {}}', "3"):
            self.assertTrue(sagas.damaged(text), text)


class TestClean(unittest.TestCase):
    def refused(self, listed):
        with self.assertRaises(SagaError) as caught:
            sagas.clean(listed)
        self.assertEqual(caught.exception.status, 400)
        return caught.exception.message

    def test_a_list_of_sagas_is_checked_and_cleaned(self):
        self.assertEqual(sagas.clean([{"name": " A  b ", "films": ["movie/1", 2, 1]}, {"name": "C"}]), [("A b", [1, 2]), ("C", [])])

    def test_every_mistake_is_refused_with_a_message(self):
        self.assertIn("liste des sagas", self.refused("x"))
        self.assertIn("un objet", self.refused(["x"]))
        self.assertIn("donne un nom", self.refused([{"films": []}]))
        self.assertIn("deux sagas s'appellent « a »", self.refused([{"name": "A"}, {"name": "a"}]))             # le message cite la seconde
        self.assertIn("liste des films", self.refused([{"name": "A", "films": 3}]))
        self.assertIn("illisible (True)", self.refused([{"name": "A", "films": [True]}]))

    def test_the_limits_are_included(self):
        with mock.patch.object(sagas, "MAX_SAGAS", 2), mock.patch.object(sagas, "MAX_FILMS", 3), mock.patch.object(sagas, "MAX_NAME", 5):
            self.assertEqual(len(sagas.clean([{"name": "abcde", "films": [1, 2, 3]}, {"name": "b"}])), 2)
            self.assertIn("2 sagas au plus", self.refused([{"name": "a"}, {"name": "b"}, {"name": "c"}]))
            self.assertIn("3 films au plus", self.refused([{"name": "a", "films": [1, 2, 3, 4]}]))
            self.assertIn("nom trop long (au plus 5 caractères) : abcdef", self.refused([{"name": "abcdef"}]))
            self.assertIn("3 films au plus", self.refused([{"name": "a", "films": [1, 1, 2, 2, 3, 3]}]))          # la limite porte sur la liste envoyée, doublons compris


class TestTmdbOf(unittest.TestCase):
    def test_the_identifier_comes_from_the_meta_of_the_row(self):
        self.assertEqual(sagas.tmdb_of({"meta": '{"tmdb": "movie/27205"}'}), 27205)
        self.assertIsNone(sagas.tmdb_of({"meta": "{}"}))
        self.assertIsNone(sagas.tmdb_of({"meta": None}))
        self.assertIsNone(sagas.tmdb_of({"meta": "pas du json"}))
        self.assertIsNone(sagas.tmdb_of({"meta": "[]"}))

    def test_the_first_film_with_an_identifier_wins(self):
        rows = [{"id": "a", "meta": '{"tmdb": 1}'}, {"id": "b", "meta": '{"tmdb": "movie/1"}'}, {"id": "c", "meta": "{}"}, {"id": "d", "meta": '{"tmdb": 2}'}]
        self.assertEqual({k: r["id"] for k, r in sagas.by_tmdb(rows).items()}, {1: "a", 2: "d"})


class TestReading(TempCase):
    def test_without_a_file_there_is_nothing_and_it_is_not_damaged(self):
        store = Sagas(self.path)
        self.assertEqual((store.all(), store.ignored(), store.damaged(), store.version()), ([], [], False, ""))

    def test_the_file_is_read_again_only_when_it_changed(self):
        self.write({"sagas": [{"name": "A", "films": [1]}]})
        store = Sagas(self.path)
        with mock.patch.object(Path, "read_bytes", autospec=True, side_effect=Path.read_bytes) as read:
            store.all()
            store.ignored()
            store.version()
            self.assertEqual(read.call_count, 1)
            self.write({"sagas": [{"name": "A", "films": [1, 2]}]})
            self.assertEqual(store.all(), [("A", [1, 2])])
            self.assertEqual(read.call_count, 2)

    def test_a_file_removed_after_a_save_is_empty_and_not_damaged(self):
        store = Sagas(self.path)
        store.save([("A", [1])])
        self.assertEqual(store.all(), [("A", [1])])
        store.save([("B", [2])])
        self.path.unlink()
        self.assertEqual((store.all(), store.ignored(), store.damaged(), store.version()), ([], [], False, ""))

    def test_the_version_changes_with_the_content(self):
        store = Sagas(self.path)
        self.write({"sagas": [{"name": "A"}]})
        first = store.version()
        self.assertRegex(first, r"^[0-9a-f]{12}$")
        self.write({"sagas": [{"name": "B"}]})
        self.assertNotEqual(store.version(), first)

    def test_a_file_that_is_not_utf8_is_damaged(self):
        self.path.write_bytes(b'{"sagas": []}\xff\xfe')
        store = Sagas(self.path)
        self.assertEqual((store.all(), store.damaged(), store.version()), ([], True, ""))

    def test_a_file_that_is_not_json_is_damaged(self):
        self.write("pas du json")
        self.assertTrue(Sagas(self.path).damaged())

    def test_the_sagas_of_the_movies_we_own_keep_the_rank_in_the_list(self):
        self.write({"sagas": [{"name": "A", "films": [10, 20, 30]}, {"name": "B", "films": [40]}, {"name": "C", "films": [30]}]})
        movies = [{"id": "m30", "meta": '{"tmdb": 30}'}, {"id": "m10", "meta": '{"tmdb": 10}'}]
        found = Sagas(self.path).of_movies(movies)
        self.assertEqual({name: [(row["id"], rank) for row, rank in here] for name, here in found.items()}, {"A": [("m10", 1), ("m30", 3)], "C": [("m30", 1)]})


class TestSaving(TempCase):
    def test_save_replaces_everything_and_keeps_the_ignored_films(self):
        self.write({"sagas": [{"name": "A", "films": [1]}], "ignore": [9]})
        store = Sagas(self.path)
        store.save([("Éé", [2, 3])])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"sagas": [{"name": "Éé", "films": [2, 3]}], "ignore": [9]})
        self.assertIn("Éé", self.path.read_text(encoding="utf-8"))                      # écrit tel quel, pas en É
        self.assertTrue(self.path.read_text(encoding="utf-8").endswith("}\n"))
        self.assertEqual(store.all(), [("Éé", [2, 3])])

    def test_nothing_ignored_is_not_written(self):
        Sagas(self.path).save([("A", [1])])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"sagas": [{"name": "A", "films": [1]}]})

    def test_the_folder_is_created_when_it_is_missing_and_may_exist(self):
        deep = Sagas(self.root / "a" / "b" / "sagas.json")
        deep.save([("A", [])])
        deep.save([("B", [])])                                  # le dossier existe déjà
        self.assertEqual(deep.all(), [("B", [])])

    def test_an_editor_that_read_an_older_version_is_refused(self):
        store = Sagas(self.path)
        store.save([("A", [1])])
        old = store.version()
        store.save([("A", [1, 2])], version=old)                # la version lue est la bonne
        self.assertIn("ont changé", self.refused(lambda: store.save([("A", [9])], version=old), 409))
        self.assertEqual(store.all(), [("A", [1, 2])])
        store.save([("A", [3])], version=None)                  # sans version : pas de contrôle
        self.assertEqual(store.all(), [("A", [3])])

    def test_a_damaged_file_is_never_overwritten(self):
        self.write("[ pas un objet")
        store = Sagas(self.path)
        for call in (lambda: store.save([("A", [])]), lambda: store.add("A", [1]), lambda: store.ignore(1)):
            self.assertIn("n'est pas lisible", self.refused(call, 409))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "[ pas un objet")

    def test_a_write_that_cannot_happen_says_why_and_leaves_no_temporary_file(self):
        blocker = self.root / "fichier"
        blocker.write_text("x")
        message = self.refused(lambda: Sagas(blocker / "sagas.json").save([("A", [])]), 500)
        self.assertIn("impossible d'écrire sagas.json", message)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["fichier"])

    def test_a_file_held_open_is_retried_a_few_times_then_the_error_is_reported(self):
        store = Sagas(self.path)
        with mock.patch.object(sagas.os, "replace", side_effect=PermissionError(13, "Accès refusé")) as replace, mock.patch.object(sagas.time, "sleep") as sleep:
            message = self.refused(lambda: store.save([("A", [])]), 500)
        self.assertEqual(replace.call_count, 5)
        self.assertEqual([round(call.args[0], 2) for call in sleep.call_args_list], [0.05, 0.1, 0.15, 0.2])
        self.assertEqual(message, "impossible d'écrire sagas.json : Accès refusé")
        self.assertFalse(Path(str(self.path) + ".tmp").exists())

    def test_a_file_held_open_for_a_moment_is_written_in_the_end(self):
        store = Sagas(self.path)
        real = os.replace
        calls = []

        def flaky(source, target):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError(13, "Accès refusé")
            real(source, target)

        with mock.patch.object(sagas.os, "replace", side_effect=flaky), mock.patch.object(sagas.time, "sleep"):
            store.save([("A", [])])
        self.assertEqual((len(calls), store.all()), (3, [("A", [])]))


class TestAdd(TempCase):
    def test_a_saga_is_created_then_completed_without_duplicates(self):
        store = Sagas(self.path)
        self.assertEqual(store.add("  Ma  saga ", [1, 2]), "Ma saga")
        self.assertEqual(store.add("MA SAGA", [2, 3]), "Ma saga")                  # la même, à la casse près : le nom d'origine reste
        self.assertEqual(store.all(), [("Ma saga", [1, 2, 3])])

    def test_the_ignored_films_are_kept(self):
        store = Sagas(self.path)
        store.ignore(9)
        store.add("A", [1])
        self.assertEqual((store.all(), store.ignored()), ([("A", [1])], [9]))

    def test_a_name_is_required_and_bounded(self):
        store = Sagas(self.path)
        self.assertIn("donne un nom", self.refused(lambda: store.add("  ", [1])))
        with mock.patch.object(sagas, "MAX_NAME", 3):
            store.add("abc", [1])
            self.assertIn("nom trop long (au plus 3 caractères)", self.refused(lambda: store.add("abcd", [1])))

    def test_the_number_of_sagas_is_bounded(self):
        store = Sagas(self.path)
        with mock.patch.object(sagas, "MAX_SAGAS", 2):
            store.add("a", [])
            store.add("b", [])
            self.assertIn("2 sagas au plus", self.refused(lambda: store.add("c", [])))
            store.add("B", [5])                                   # une saga qui existe déjà s'ajoute encore
        self.assertEqual(store.all(), [("a", []), ("b", [5])])

    def test_the_number_of_films_of_a_saga_is_bounded_and_nothing_is_written_over_it(self):
        store = Sagas(self.path)
        with mock.patch.object(sagas, "MAX_FILMS", 3):
            store.add("a", [1, 2, 3])
            message = self.refused(lambda: store.add("a", [4]))
            self.assertIn("« a » : 3 films au plus", message)
        self.assertEqual(store.all(), [("a", [1, 2, 3])])


class TestIgnore(TempCase):
    def test_a_film_is_hidden_by_default_then_shown_again(self):
        store = Sagas(self.path)
        store.ignore(5)
        store.ignore(5)                                         # déjà caché : sans effet
        self.assertEqual(store.ignored(), [5])
        store.ignore(5, on=False)
        self.assertEqual(store.ignored(), [])
        store.ignore(5, on=False)                               # déjà visible : sans effet
        self.assertEqual(store.ignored(), [])

    def test_the_number_of_hidden_films_is_bounded(self):
        store = Sagas(self.path)
        with mock.patch.object(sagas, "MAX_IGNORED", 2):
            store.ignore(1)
            store.ignore(2)
            self.assertIn("2 films cachés au plus", self.refused(lambda: store.ignore(3)))
        self.assertEqual(store.ignored(), [1, 2])

    def test_the_sagas_are_kept(self):
        store = Sagas(self.path)
        store.add("A", [1])
        store.ignore(9)
        self.assertEqual((store.all(), store.ignored()), ([("A", [1])], [9]))


if __name__ == "__main__":
    unittest.main()
