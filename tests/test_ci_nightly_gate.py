"""La règle de saut de la passe de nuit (`.github/scripts/nightly_gate.py`) : quand un commit est-il déjà traité ?"""
import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("nightly_gate", ROOT / ".github" / "scripts" / "nightly_gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

SHA = "abc123"
RUNS = f"repos/me/hub/actions/workflows/nightly.yml/runs?branch=main&head_sha={SHA}&status=completed&per_page=30"


def crapper(conclusion="success"):
    return {"name": "crapper", "conclusion": conclusion, "steps": []}


def part(name, step="success", job="success"):
    return {"name": f"mutator ({name})", "conclusion": job,
            "steps": [{"name": "Fichiers à muter", "conclusion": "success"}, {"name": "Mutator", "conclusion": step}]}


class TestIsTreated(unittest.TestCase):
    def test_everything_went_through(self):
        self.assertTrue(gate.is_treated([crapper(), part("serveur"), part("reste")]))

    def test_survivors_leave_the_job_red_but_the_commit_is_treated(self):
        self.assertTrue(gate.is_treated([crapper(), part("serveur", job="failure"), part("reste")]))

    def test_a_failed_crapper_is_not_treated(self):
        self.assertFalse(gate.is_treated([crapper("failure"), part("serveur")]))

    def test_a_skipped_pass_is_not_treated(self):
        self.assertFalse(gate.is_treated([crapper("skipped"), {"name": "mutator", "conclusion": "skipped", "steps": []}]))

    def test_a_part_stopped_at_the_job_limit_is_not_treated(self):
        self.assertFalse(gate.is_treated([crapper(), part("serveur", "cancelled", "cancelled"), part("reste")]))

    def test_a_part_whose_machine_was_lost_is_not_treated(self):
        self.assertFalse(gate.is_treated([crapper(), part("scan", None, "failure"), part("reste")]))

    def test_a_broken_baseline_is_not_treated(self):
        self.assertFalse(gate.is_treated([crapper(), part("media", "failure", "failure")]))

    def test_a_pass_without_mutator_is_not_treated(self):
        self.assertFalse(gate.is_treated([crapper()]))

    def test_a_pass_without_crapper_is_not_treated(self):
        self.assertFalse(gate.is_treated([part("serveur")]))


class TestDecision(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.output = Path(tmp.name) / "output"

    def decide(self, event, api):
        env = {"GITHUB_EVENT_NAME": event, "GITHUB_REPOSITORY": "me/hub", "GITHUB_SHA": SHA,
               "GITHUB_OUTPUT": str(self.output)}
        with mock.patch.dict(os.environ, env), mock.patch("builtins.print"):
            self.assertEqual(gate.main(api), 0)
        return self.output.read_text(encoding="utf-8")

    @staticmethod
    def api_with(jobs_by_run):
        answers = {RUNS: {"workflow_runs": [{"id": run} for run in jobs_by_run]}}
        for run, jobs in jobs_by_run.items():
            answers[f"repos/me/hub/actions/runs/{run}/jobs?per_page=100"] = {"jobs": jobs}
        return answers.__getitem__

    def test_a_manual_run_always_goes_without_asking_github(self):
        self.assertEqual(self.decide("workflow_dispatch", mock.Mock(side_effect=AssertionError)), "run=true\n")

    def test_the_night_is_skipped_when_one_pass_treated_this_commit(self):
        api = self.api_with({2: [crapper(), part("serveur", "cancelled")], 1: [crapper(), part("serveur")]})
        self.assertEqual(self.decide("schedule", api), "run=false\n")

    def test_the_night_goes_when_no_pass_treated_this_commit(self):
        api = self.api_with({1: [crapper(), part("serveur", "cancelled")]})
        self.assertEqual(self.decide("schedule", api), "run=true\n")

    def test_the_night_goes_on_a_new_commit(self):
        self.assertEqual(self.decide("schedule", self.api_with({})), "run=true\n")

    def test_the_night_goes_when_github_does_not_answer(self):
        # Mieux vaut une passe de trop qu'une nuit perdue sur un incident de l'API.
        api = mock.Mock(side_effect=subprocess.CalledProcessError(1, "gh"))
        self.assertEqual(self.decide("schedule", api), "run=true\n")


if __name__ == "__main__":
    unittest.main()
