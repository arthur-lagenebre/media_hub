"""Les hooks de `.claude/hooks/` : la confirmation de nuit pour les agents, et le garde-fou git qui applique en dur les règles de /ship et /merged."""
import datetime
import importlib.util
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / ".claude" / "hooks"


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), HOOKS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


night = load("confirm-night")
guard = load("guard-git")


def at(hour):
    return datetime.datetime(2026, 11, 10, hour, 30)


def agent(name):
    return {"tool_input": {"subagent_type": name}}


class TestConfirmNight(unittest.TestCase):
    def test_an_implementer_launched_at_night_asks_for_confirmation(self):
        for hour in (23, 0, 5):
            answer = night.decide(agent("hub-server"), at(hour))
            self.assertEqual(answer["hookSpecificOutput"]["permissionDecision"], "ask", hour)

    def test_the_night_ends_at_six(self):
        self.assertIsNone(night.decide(agent("hub-ui"), at(6)))
        self.assertIsNone(night.decide(agent("hub-ui"), at(22)))

    def test_a_read_only_agent_is_never_asked(self):
        self.assertIsNone(night.decide(agent("hub-reviewer"), at(3)))
        self.assertIsNone(night.decide(agent("hub-architect"), at(3)))


def verdict(command, branch="accueil"):
    """Ce que le garde-fou répond pour une commande Bash lancée sur `branch`."""
    event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "."}
    answer = guard.decide(event, lambda cwd: branch)
    return answer["hookSpecificOutput"]["permissionDecision"] if answer else None


class TestGuardRefuses(unittest.TestCase):
    def assertDenied(self, command, branch="accueil"):
        self.assertEqual(verdict(command, branch), "deny", command)

    def test_a_forced_push_in_any_spelling(self):
        for command in (
            "git push --force",
            "git push -f origin accueil",
            "git push origin accueil --force-with-lease",
            "git push -uf origin accueil",
            "git push origin +accueil",
        ):
            self.assertDenied(command)

    def test_deleting_a_remote_branch(self):
        self.assertDenied("git push origin --delete accueil")
        self.assertDenied("git push origin :accueil")

    def test_pushing_to_main(self):
        self.assertDenied("git push origin main")
        self.assertDenied("git push origin HEAD:main")
        self.assertDenied("git push origin HEAD:refs/heads/main")

    def test_skipping_the_hooks(self):
        self.assertDenied("git commit --no-verify -m x")
        self.assertDenied("git commit -n -m x")
        self.assertDenied("git push --no-verify")

    def test_the_hard_branch_delete(self):
        self.assertDenied("git branch -D accueil")
        self.assertDenied("git branch -vD accueil")

    def test_staging_everything(self):
        for command in ("git add -A", "git add --all", "git add .", "git add -Av"):
            self.assertDenied(command)

    def test_a_commit_on_main(self):
        self.assertDenied("git commit -m x", branch="main")
        self.assertDenied("git add a.py && git commit -m x", branch="main")

    def test_merging_a_pull_request(self):
        self.assertDenied("gh pr merge 12 --squash")

    def test_the_command_is_found_after_a_prefix_or_a_separator(self):
        self.assertDenied("cd /d && git push -f")
        self.assertDenied("GIT_TERMINAL_PROMPT=0 git push -f")
        self.assertDenied("git -c core.editor=true push -f")
        self.assertDenied("git status\ngit branch -D x")

    def test_a_here_string_does_not_hide_the_rest_of_the_command(self):
        self.assertDenied("cat <<<texte\ngit push -f")

    def test_the_reason_is_in_french_and_says_what_to_do(self):
        event = {"tool_name": "Bash", "tool_input": {"command": "git push -f"}, "cwd": "."}
        reason = guard.decide(event, lambda cwd: "x")["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("lui-même", reason)


class TestGuardLetsThrough(unittest.TestCase):
    def assertAllowed(self, command, branch="accueil"):
        self.assertIsNone(verdict(command, branch), command)

    def test_the_everyday_git_commands(self):
        for command in (
            "git status --short",
            "git add hub/db.py tests/test_database.py",
            "git commit -m 'Accueil : 24 nouveautés'",
            "git push -u origin accueil",
            "git push origin v0.3.0",
            "git branch -d accueil",
            "git switch main && git pull --ff-only origin main",
            "git fetch --prune",
            "git diff main...HEAD -- hub",
            "gh pr create --title x",
            "gh pr view 12",
        ):
            self.assertAllowed(command)

    def test_a_commit_off_main_and_a_pull_on_main(self):
        self.assertAllowed("git commit -m x", branch="accueil")
        self.assertAllowed("git pull --ff-only origin main", branch="main")

    def test_a_dangerous_word_inside_a_quoted_message_is_not_a_command(self):
        self.assertAllowed('git commit -m "Ne plus faire git push -f ni git add -A"')
        self.assertAllowed("git commit -m 'git branch -D est interdit'")

    def test_a_dangerous_word_inside_a_heredoc_is_not_a_command(self):
        body = "git commit -F - <<'EOF'\nAccueil : x\n\ngit push --force\ngit add -A\nEOF"
        self.assertAllowed(body)

    def test_a_branch_created_in_the_same_command_is_not_judged_on_the_old_branch(self):
        self.assertAllowed("git switch -c accueil && git commit -m x", branch="main")
        self.assertAllowed("cd ../media_hub-lot2 && git commit -m x", branch="main")

    def test_a_detached_head_or_an_unreadable_repository_is_not_blocked(self):
        self.assertAllowed("git commit -m x", branch="")
        self.assertAllowed("git commit -m x", branch=None)

    def test_other_tools_and_unreadable_events_are_ignored(self):
        self.assertIsNone(guard.decide({"tool_name": "Edit", "tool_input": {}}, lambda cwd: "main"))
        self.assertIsNone(guard.decide({"tool_name": "Bash", "tool_input": {}}, lambda cwd: "main"))
        self.assertIsNone(guard.decide({}, lambda cwd: "main"))


if __name__ == "__main__":
    unittest.main()
