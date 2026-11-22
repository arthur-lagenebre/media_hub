"""Le lanceur de mutator de la passe de nuit (`.github/scripts/mutate_part.py`) : ce qu'il lance, et ce qu'il conclut."""
import importlib.util
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("mutate_part", ROOT / ".github" / "scripts" / "mutate_part.py")
part = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(part)


def option(command, name):
    return command[command.index(name) + 1]


class ProjectCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name in ("tests/test_alpha.py", "tests/test_alpha_more.py", "tests/test_beta.py", "tests/test_cli.py",
                     "tests/test_enrich_people.py", "tests/test_person_films.py"):
            self.write(name)

    def write(self, name, lines=1):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n" * lines, encoding="utf-8")

    def plan(self, paths, tests="", **options):
        return part.plan_part(self.root, paths, tests, python="/venv/python", mutator="mutator", **options)


class TestTestCommand(ProjectCase):
    def test_given_patterns_are_the_tests_of_every_file(self):
        self.write("hub/a.py")
        self.write("hub/b.py")
        commands = self.plan(["hub/a.py", "hub/b.py"], "tests/test_beta.py tests/test_alpha*.py")
        expected = "/venv/python -m unittest tests.test_alpha tests.test_alpha_more tests.test_beta"
        self.assertEqual([option(c, "--test-command") for c in commands], [expected, expected])

    def test_without_patterns_a_file_gets_the_tests_named_after_it(self):
        self.write("hub/alpha.py")
        command = self.plan(["hub/alpha.py"])[0]
        self.assertEqual(option(command, "--test-command"), "/venv/python -m unittest tests.test_alpha tests.test_alpha_more")

    def test_the_two_slow_modules_have_their_own_tests(self):
        self.write("hub/__main__.py")
        self.write("hub/people.py")
        commands = self.plan(["hub/__main__.py", "hub/people.py"])
        self.assertEqual(option(commands[0], "--test-command"), "/venv/python -m unittest tests.test_cli")
        self.assertEqual(option(commands[1], "--test-command"),
                         "/venv/python -m unittest tests.test_enrich_people tests.test_person_films")

    def test_enrich_also_gets_the_tests_of_the_filmographies_it_builds(self):
        self.write("hub/enrich.py")
        self.write("tests/test_enrich.py")
        self.assertEqual(option(self.plan(["hub/enrich.py"])[0], "--test-command"),
                         "/venv/python -m unittest tests.test_enrich tests.test_enrich_people tests.test_person_films")

    def test_a_file_without_tests_falls_back_on_the_whole_suite(self):
        self.write("hub/urls.py")
        self.assertEqual(option(self.plan(["hub/urls.py"])[0], "--test-command"), "/venv/python -m unittest discover")

    def test_a_memory_limit_wraps_the_test_command(self):
        self.write("hub/alpha.py")
        command = self.plan(["hub/alpha.py"], memory_mb=4096)[0]
        self.assertEqual(option(command, "--test-command"),
                         "ulimit -v 4194304; /venv/python -m unittest tests.test_alpha tests.test_alpha_more")


class TestFilesAndCoverage(ProjectCase):
    def test_a_directory_is_expanded_to_its_python_files(self):
        self.write("hub/server/b.py")
        self.write("hub/server/a.py")
        self.write("hub/server/notes.txt")
        self.assertEqual([c[-1] for c in self.plan(["hub/server"], "tests/test_beta.py")],
                         ["hub/server/a.py", "hub/server/b.py"])

    def test_coverage_is_measured_once_then_reused(self):
        for name in ("hub/a.py", "hub/b.py", "hub/c.py"):
            self.write(name)
        commands = self.plan(["hub/a.py", "hub/b.py", "hub/c.py"], "tests/test_beta.py")
        self.assertEqual(["--reuse-coverage" in c for c in commands], [False, True, True])

    def test_the_worker_limit_is_passed_on(self):
        self.write("hub/a.py")
        self.assertEqual(option(self.plan(["hub/a.py"], "tests/test_beta.py", workers=2)[0], "--max-workers"), "2")
        self.assertNotIn("--max-workers", self.plan(["hub/a.py"], "tests/test_beta.py")[0])


class TestBatches(ProjectCase):
    def test_a_short_file_runs_whole(self):
        self.write("hub/a.py", part.BATCH_LINES)
        commands = self.plan(["hub/a.py"], "tests/test_beta.py")
        self.assertEqual(len(commands), 1)
        self.assertNotIn("--lines", commands[0])

    def test_a_long_file_runs_in_batches_that_cover_every_line_once(self):
        total = part.BATCH_LINES * 2 + 1
        self.write("hub/a.py", total)
        commands = self.plan(["hub/a.py"], "tests/test_beta.py")
        batches = [[int(n) for n in option(c, "--lines").split(",")] for c in commands]
        self.assertEqual([len(b) for b in batches], [part.BATCH_LINES, part.BATCH_LINES, 1])
        self.assertEqual([n for b in batches for n in b], list(range(1, total + 1)))
        self.assertEqual(["--reuse-coverage" in c for c in commands], [False, True, True])


class TestShares(ProjectCase):
    def units(self, commands):
        return [(c[-1], option(c, "--lines").split(",")[0] if "--lines" in c else None) for c in commands]

    def test_a_share_takes_one_unit_in_n_and_measures_coverage_first(self):
        self.write("hub/server/a.py", part.BATCH_LINES * 2 + 1)
        self.write("hub/server/b.py")
        commands = self.plan(["hub/server"], "tests/test_beta.py", share=(2, 2))
        self.assertEqual(self.units(commands), [("hub/server/a.py", str(part.BATCH_LINES + 1)), ("hub/server/b.py", None)])
        self.assertEqual(["--reuse-coverage" in c for c in commands], [False, True])

    def test_the_shares_together_run_every_unit_once(self):
        self.write("hub/server/a.py", part.BATCH_LINES * 3)
        self.write("hub/server/b.py")
        whole = self.units(self.plan(["hub/server"], "tests/test_beta.py"))
        shared = [unit for k in (1, 2, 3) for unit in self.units(self.plan(["hub/server"], "tests/test_beta.py", share=(k, 3)))]
        self.assertEqual(sorted(shared, key=whole.index), whole)

    def test_a_share_beyond_the_units_has_nothing_to_run(self):
        self.write("hub/server/a.py")
        self.assertEqual(self.plan(["hub/server"], "tests/test_beta.py", share=(2, 2)), [])

    def test_a_share_is_read_as_k_over_n(self):
        self.assertEqual(part.parse_share("3/10"), (3, 10))
        self.assertIsNone(part.parse_share(""))
        for wrong in ("0/10", "11/10", "3", "a/b"):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                part.parse_share(wrong)


class TestOutcome(unittest.TestCase):
    def run_plan(self, codes):
        seen = []

        def execute(command):
            seen.append(command)
            code = codes[len(seen) - 1]
            return code, [(f"hub/{command[0]}.py", 1, "True -> False")] if code == 3 else []

        return part.run_plan([[str(i)] for i in range(len(codes))], execute), len(seen)

    def test_everything_killed(self):
        self.assertEqual(self.run_plan([0, 0]), ((0, False, []), 2))

    def test_a_survivor_is_reported_without_failing_and_the_rest_still_runs(self):
        self.assertEqual(self.run_plan([0, 3, 0]), ((0, True, [("hub/1.py", 1, "True -> False")]), 3))

    def test_a_broken_baseline_fails_but_the_other_files_still_run(self):
        self.assertEqual(self.run_plan([0, 2, 3]), ((2, True, [("hub/2.py", 1, "True -> False")]), 3))

    def test_a_failure_of_the_first_command_stops_everything(self):
        # Il mesure la couverture : sans elle, les suivants n'auraient rien à lire.
        self.assertEqual(self.run_plan([1, 0, 0]), ((1, False, []), 1))


class TestEquivalentMutants(ProjectCase):
    """Les mutants acceptés (`.github/mutants-equivalents.json`) : reconnus par le fichier, le texte de la ligne et la mutation, pas par le numéro de ligne."""

    WHY = "sans effet : la raison est écrite"

    def setUp(self):
        super().setUp()
        (self.root / "hub").mkdir()
        (self.root / "hub" / "base.py").write_text("import sqlite3\n\nconn = sqlite3.connect(name, uri=True)\n", encoding="utf-8")
        self.accepted = [{"file": "hub/base.py", "line": "conn = sqlite3.connect(name, uri=True)", "mutation": "True -> False", "why": self.WHY}]

    def test_mutator_lines_give_the_survivors_and_nothing_else(self):
        out = ["KILLED    hub/base.py:1 == -> !=\n", "SURVIVED  hub/base.py:3 True -> False\n", "UNCOVERED hub/base.py:3 and -> or\n", "Total  1  1  0  2  50.0%\n"]
        self.assertEqual(part.survivors(out), [("hub/base.py", 3, "True -> False")])

    def test_an_accepted_survivor_is_recognised_wherever_its_line_has_moved(self):
        self.assertEqual(part.unaccepted([("hub/base.py", 3, "True -> False")], self.root, self.accepted), [])
        (self.root / "hub" / "base.py").write_text("import sqlite3\nimport json\n\n    conn = sqlite3.connect(name, uri=True)\n", encoding="utf-8")
        self.assertEqual(part.unaccepted([("hub/base.py", 4, "True -> False")], self.root, self.accepted), [])

    def test_another_mutation_another_line_or_another_file_is_not_accepted(self):
        others = [("hub/base.py", 3, "delete not"), ("hub/base.py", 1, "True -> False"), ("hub/other.py", 3, "True -> False")]
        self.assertEqual(part.unaccepted(others, self.root, self.accepted), others)

    def test_each_accepted_mutant_says_why(self):
        accepted = part.load_equivalents(ROOT / ".github" / "mutants-equivalents.json")
        self.assertTrue(accepted)
        for entry in accepted:
            self.assertEqual(set(entry), {"file", "line", "mutation", "why"}, entry)
            self.assertTrue(entry["why"].strip(), entry)
            lines = [text.strip() for text in (ROOT / entry["file"]).read_text(encoding="utf-8").splitlines()]
            self.assertIn(entry["line"], lines, entry)                       # la ligne existe encore telle quelle : sinon l'entrée est périmée

    def test_a_part_stays_red_only_for_survivors_that_are_not_accepted(self):
        def run(codes, printed):
            def execute(command):
                return codes.pop(0), printed.pop(0)
            return part.conclude(part.run_plan([["a"], ["b"]], execute), self.root, self.accepted)

        self.assertEqual(run([0, 3], [[], [("hub/base.py", 3, "True -> False")]]), (0, False, []))
        self.assertEqual(run([0, 3], [[], [("hub/base.py", 3, "delete not")]]), (0, True, [("hub/base.py", 3, "delete not")]))
        self.assertEqual(run([0, 3], [[], []]), (0, True, []))            # mutator dit « survivant » sans qu'on lise lequel : rouge, par prudence


class TestServerSharesOfTheNightlyPass(unittest.TestCase):
    """Les parts « serveur » de nightly.yml, sur le vrai hub/server : une tranche de app.py par job, et rien d'autre avec elle.
    Une tranche prend jusqu'à 4 h 30 ; deux dans le même job, ou une tranche avec jobs.py, dépassent la limite de 350 min (run 44890954001)."""

    def test_each_tranche_of_app_py_has_a_job_to_itself(self):
        workflow = (ROOT / ".github" / "workflows" / "nightly.yml").read_text(encoding="utf-8")
        shares = {int(n) for n in re.findall(r"share: \d+/(\d+), paths: hub/server", workflow)}
        self.assertEqual(len(shares), 1, "toutes les parts « serveur » doivent avoir le même N")
        n = shares.pop()
        self.assertEqual(sorted(int(k) for k in re.findall(rf"share: (\d+)/{n}, paths: hub/server", workflow)), list(range(1, n + 1)))
        for k in range(1, n + 1):
            files = [command[-1] for command in part.plan_part(ROOT, ["hub/server"], "", python="python", mutator="mutator", share=(k, n))]
            if "hub/server/app.py" in files:
                self.assertEqual(files, ["hub/server/app.py"],
                                 f"la part {k}/{n} a une tranche de app.py et autre chose : ajouter une part « serveur » dans nightly.yml")


if __name__ == "__main__":
    unittest.main()
