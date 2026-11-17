"""Le lanceur en parallèle (tests/parallel.py) : la répartition des classes de tests sur les processus, le décompte des résultats et la mémoire des durées."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tests import parallel


class TestSplit(unittest.TestCase):
    def test_the_longest_classes_go_first_and_each_to_the_least_loaded_process(self):
        durations = {"a.A": 9.0, "a.B": 7.0, "a.C": 6.0, "a.D": 5.0, "a.E": 4.0, "a.F": 1.0}
        buckets = parallel.split(list(durations), durations, 3)
        self.assertEqual(buckets, [["a.A", "a.F"], ["a.B", "a.E"], ["a.C", "a.D"]])

    def test_every_class_is_run_exactly_once(self):
        classes = [f"m.C{i}" for i in range(17)]
        durations = {name: float(i % 5) for i, name in enumerate(classes)}
        buckets = parallel.split(classes, durations, 4)
        self.assertEqual(sorted(name for bucket in buckets for name in bucket), sorted(classes))

    def test_no_empty_process_when_there_are_fewer_classes_than_processes(self):
        buckets = parallel.split(["m.A", "m.B"], {"m.A": 3.0, "m.B": 1.0}, 6)
        self.assertEqual(buckets, [["m.A"], ["m.B"]])

    def test_a_class_without_duration_counts_as_the_median_of_the_known_ones(self):
        durations = {"m.A": 2.0, "m.B": 10.0, "m.C": 4.0}      # médiane : 4
        self.assertEqual(parallel.estimate("m.nouvelle", durations), 4.0)
        # B (10) seule d'un côté ; de l'autre C (4), la nouvelle (comptée 4) puis A (2) : 10 contre 10
        buckets = parallel.split(["m.A", "m.B", "m.C", "m.nouvelle"], durations, 2)
        self.assertEqual(buckets, [["m.B"], ["m.C", "m.nouvelle", "m.A"]])

    def test_with_no_duration_at_all_every_class_weighs_the_same(self):
        self.assertEqual(parallel.estimate("m.A", {}), parallel.DEFAULT_SECONDS)
        buckets = parallel.split(["m.A", "m.B", "m.C", "m.D"], {}, 2)
        self.assertEqual([len(bucket) for bucket in buckets], [2, 2])

    def test_the_split_does_not_depend_on_the_order_of_the_classes(self):
        durations = {"m.A": 3.0, "m.B": 3.0, "m.C": 3.0, "m.D": 1.0}
        self.assertEqual(parallel.split(["m.D", "m.C", "m.B", "m.A"], durations, 2), parallel.split(["m.A", "m.B", "m.C", "m.D"], durations, 2))

    def test_nothing_to_run_gives_no_process(self):
        self.assertEqual(parallel.split([], {}, 6), [])


class TestTotals(unittest.TestCase):
    def test_the_counts_of_every_class_add_up(self):
        results = {
            "m.A": {"run": 5, "failures": 1, "errors": 0, "skipped": 2, "seconds": 1.5},
            "m.B": {"run": 3, "failures": 0, "errors": 2, "skipped": 0, "seconds": 0.5},
        }
        self.assertEqual(parallel.totals(results), {"run": 8, "failures": 1, "errors": 2, "skipped": 2})

    def test_only_a_clean_run_exits_with_zero(self):
        self.assertEqual(parallel.exit_code({"run": 8, "failures": 0, "errors": 0, "skipped": 8}, crashed=0), 0)
        self.assertEqual(parallel.exit_code({"run": 8, "failures": 1, "errors": 0, "skipped": 0}, crashed=0), 1)
        self.assertEqual(parallel.exit_code({"run": 8, "failures": 0, "errors": 1, "skipped": 0}, crashed=0), 1)
        self.assertEqual(parallel.exit_code({"run": 8, "failures": 0, "errors": 0, "skipped": 0}, crashed=1), 1)


class TestDurationsFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / ".metrics" / "test-durations.json"

    def test_a_missing_or_broken_file_remembers_nothing(self):
        self.assertEqual(parallel.load_durations(self.path), {})
        self.path.parent.mkdir()
        self.path.write_text("{pas du json", encoding="utf-8")
        self.assertEqual(parallel.load_durations(self.path), {})

    def test_new_measures_replace_old_ones_and_the_others_are_kept(self):
        parallel.save_durations(self.path, {"m.A": 5.0, "m.B": 2.0})
        parallel.save_durations(self.path, {"m.A": 1.25})
        self.assertEqual(parallel.load_durations(self.path), {"m.A": 1.25, "m.B": 2.0})


class TestDiscovery(unittest.TestCase):
    def test_the_classes_of_a_module_are_found_without_the_ones_it_only_imports(self):
        # test_popups importe UiCase (sans test) de test_ui : seules ses deux classes comptent
        self.assertEqual(sorted(parallel.discover(["tests.test_popups"])), ["tests.test_popups.TestPopupsInTheBrowser", "tests.test_popups.TestPopupsShareOneTemplate"])

    def test_a_module_that_does_not_exist_is_reported(self):
        with self.assertRaises(SystemExit):
            parallel.discover(["tests.nexiste_pas"])


class TestRun(unittest.TestCase):
    def test_a_small_module_runs_in_two_processes_and_its_durations_are_kept(self):
        """Le lanceur de bout en bout : deux processus, le décompte affiché, le code 0, les durées écrites."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "durations.json"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = parallel.main(["-j", "2", "--durations", str(path), "tests.test_throttle", "tests.test_runlock"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("tests", out.getvalue())
        self.assertIn("sautés", out.getvalue())
        self.assertEqual(sorted(json.loads(path.read_text(encoding="utf-8"))), ["tests.test_runlock.TestRunLock", "tests.test_throttle.TestThrottle"])

    def test_a_failing_test_gives_a_non_zero_exit_and_its_report(self):
        """Un échec dans un processus : sa sortie complète est affichée, le total le compte, le code est 1."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "faux_tests.py").write_text("import unittest\nclass TestFaux(unittest.TestCase):\n    def test_ok(self):\n        pass\n    def test_ko(self):\n        self.fail('exprès')\n", encoding="utf-8")
        sys.path.insert(0, str(tmp))
        self.addCleanup(sys.path.remove, str(tmp))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(tmp)}), contextlib.redirect_stdout(out):
            code = parallel.main(["-j", "2", "--durations", str(tmp / "d.json"), "faux_tests"])
        self.assertEqual(code, 1)
        self.assertIn("exprès", out.getvalue())
        self.assertIn("2 tests, 1 échecs, 0 erreurs, 0 sautés", out.getvalue())
