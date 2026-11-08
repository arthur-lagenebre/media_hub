"""Le contrôle de version de la CI (`.github/scripts/check_version.py`) : ce qu'il lit, ce qu'il refuse, ce dont il avertit."""
import importlib.util
import unittest
from pathlib import Path

from hub import __version__, config, db
from tests import fixtures

config.use_media_library(fixtures.REPO)

from hub.server import app  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("check_version", ROOT / ".github" / "scripts" / "check_version.py")
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)

HUB_FILE = ["hub/server/app.py"]


def constants(version="0.2.0", api=1, schema=4, migrations=(4,)):
    return {"version": version, "api": api, "schema": schema, "migrations": set(migrations)}


class TestReadConstants(unittest.TestCase):
    def test_reads_the_four_values(self):
        init = '"""doc"""\n\n__version__ = "0.2.0"\n'
        app = "API_VERSION = 1             # le contrat\n"
        db = 'MIGRATIONS = {4: _add_play}      # ex. 3: lambda conn: conn.execute("ALTER")\nSCHEMA_VERSION = 4   # 2 : users\n'
        self.assertEqual(check.read_constants(init, app, db), constants())

    def test_several_migrations_on_several_lines(self):
        db = "MIGRATIONS = {\n    4: _a,\n    5: _b,\n}\nSCHEMA_VERSION = 5\n"
        self.assertEqual(check.read_constants('__version__ = "1.0.0"', "API_VERSION = 2", db)["migrations"], {4, 5})

    def test_a_missing_value_is_reported_by_name(self):
        with self.assertRaisesRegex(ValueError, "SCHEMA_VERSION"):
            check.read_constants('__version__ = "0.2.0"', "API_VERSION = 1", "MIGRATIONS = {}")

    def test_the_real_files_are_readable_and_agree_with_the_code(self):
        read = check.read_constants(*(check.read_text(ROOT / name) for name in check.CONSTANT_FILES))
        self.assertEqual((read["version"], read["api"], read["schema"]), (__version__, app.API_VERSION, db.SCHEMA_VERSION))
        self.assertEqual(read["migrations"], set(db.MIGRATIONS))


class TestJudge(unittest.TestCase):
    def judge(self, files, before, after):
        return check.judge(files, before, after)

    def test_nothing_changed_in_hub_is_fine(self):
        self.assertEqual(self.judge(["docs/api.md", "tests/test_x.py"], constants(), constants()), ([], []))

    def test_a_change_in_hub_without_a_new_version_is_a_warning_not_an_error(self):
        errors, warnings = self.judge(HUB_FILE, constants(), constants())
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("__version__", warnings[0])

    def test_a_change_in_hub_with_a_minor_bump_is_fine(self):
        self.assertEqual(self.judge(HUB_FILE, constants(), constants(version="0.3.0")), ([], []))

    def test_a_patch_bump_is_fine(self):
        self.assertEqual(self.judge(HUB_FILE, constants(), constants(version="0.2.1")), ([], []))

    def test_a_new_version_without_a_change_in_hub_is_a_warning(self):
        errors, warnings = self.judge(["docs/api.md"], constants(), constants(version="0.2.1"))
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)

    def test_a_version_that_goes_down_is_an_error(self):
        errors, _ = self.judge(HUB_FILE, constants(version="0.3.0"), constants(version="0.2.9"))
        self.assertEqual(len(errors), 1)

    def test_the_same_number_is_not_going_down(self):
        self.assertEqual(self.judge(HUB_FILE, constants(version="0.10.0"), constants(version="0.10.0"))[0], [])

    def test_numbers_are_compared_as_numbers_not_as_text(self):
        self.assertEqual(self.judge(HUB_FILE, constants(version="0.9.0"), constants(version="0.10.0")), ([], []))

    def test_a_version_that_is_not_x_y_z_is_an_error(self):
        errors, _ = self.judge(HUB_FILE, constants(), constants(version="0.3"))
        self.assertEqual(len(errors), 1)

    def test_going_to_one_dot_zero_is_a_warning_for_the_owner(self):
        errors, warnings = self.judge(HUB_FILE, constants(version="0.9.0"), constants(version="1.0.0"))
        self.assertEqual(errors, [])
        self.assertTrue(any("1.0.0" in warning or "majeure" in warning for warning in warnings))

    def test_a_schema_that_goes_down_is_an_error(self):
        errors, _ = self.judge(HUB_FILE, constants(schema=5, migrations=(4, 5)), constants(schema=4, migrations=(4, 5), version="0.3.0"))
        self.assertEqual(len(errors), 1)

    def test_a_new_schema_without_a_migration_is_a_warning(self):
        errors, warnings = self.judge(HUB_FILE, constants(), constants(schema=5, version="0.3.0"))
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("MIGRATIONS", warnings[0])

    def test_a_new_schema_with_its_migration_is_fine(self):
        self.assertEqual(self.judge(HUB_FILE, constants(), constants(schema=5, migrations=(4, 5), version="0.3.0")), ([], []))

    def test_a_new_api_version_needs_at_least_a_minor_bump(self):
        _, warnings = self.judge(HUB_FILE, constants(), constants(api=2, version="0.2.1"))
        self.assertEqual(len(warnings), 1)
        self.assertIn("API_VERSION", warnings[0])
        self.assertEqual(self.judge(HUB_FILE, constants(), constants(api=2, version="0.3.0")), ([], []))

    def test_a_new_api_version_without_any_bump_says_it_once_for_each_problem(self):
        _, warnings = self.judge(HUB_FILE, constants(), constants(api=2))
        self.assertEqual(len(warnings), 2)


class TestChangedFiles(unittest.TestCase):
    def test_only_the_server_package_counts(self):
        self.assertTrue(check.touches_hub(["README.md", "hub/web/js/core.js"]))
        self.assertFalse(check.touches_hub(["README.md", "tests/test_hub.py", "deploy/docker-compose.yml", ".claude/agents/x.md", ".github/workflows/a.yml"]))

    def test_a_folder_that_only_starts_like_hub_does_not_count(self):
        self.assertFalse(check.touches_hub(["hubris/x.py"]))


if __name__ == "__main__":
    unittest.main()
