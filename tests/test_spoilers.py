"""« Masquer les épisodes non vus » : chaque réponse qui montre un épisode dit, par `progress`, si le compte l'a commencé (absent : pas encore commencé).
Le masquage lui-même est fait par l'interface (voir docs/decisions.md) : le serveur envoie toujours titres, résumés et images."""

from __future__ import annotations

import unittest

from tests.test_server import CAPS_ALL, FFMPEG, ServerCase


class SpoilerCase(ServerCase):
    def setUp(self):
        self.dark = self.detail(self.item_by_title("series", "Dark")["id"])
        self.first, self.second, self.third = self.dark["entries"]         # S1E1, S1E2, S2E1
        self.api.json(f"/api/item/{self.dark['id']}/seen", {"done": False})     # chaque test part d'une série jamais commencée
        self.addCleanup(self.api.json, f"/api/item/{self.dark['id']}/seen", {"done": False})

    def watch(self, entry, position, duration=3000):
        self.assertEqual(self.api.json("/api/progress", {"entry": entry["id"], "position": position, "duration": duration})[0], 200)


class TestContinue(SpoilerCase):
    def card(self):
        found = [c for c in self.api.json("/api/home")[1]["continue"] if c["item"]["id"] == self.dark["id"]]
        self.assertEqual(len(found), 1)
        return found[0]

    def test_the_next_episode_not_yet_started_has_no_progress(self):
        self.watch(self.first, 2950)
        card = self.card()
        self.assertEqual(card["id"], self.second["id"])
        self.assertNotIn("progress", card)

    def test_an_episode_in_progress_says_where_it_stands(self):
        self.watch(self.second, 600)
        card = self.card()
        self.assertEqual((card["id"], card["progress"]["position"], card["progress"]["done"]), (self.second["id"], 600, False))

    def test_the_next_episode_already_started_says_so_after_the_previous_one_is_finished(self):
        self.watch(self.second, 600)
        self.watch(self.first, 2950)
        self.hub.db.execute("UPDATE progress SET updated=updated-100 WHERE entry_id=?", (self.second["id"],))     # l'épisode 2 commencé avant de finir le 1 : « Reprendre » part du dernier touché
        card = self.card()
        self.assertEqual((card["id"], card["resume"], card["progress"]["position"], card["progress"]["done"]), (self.second["id"], False, 600, False))

    def test_a_seen_episode_is_never_offered_the_one_after_it_is(self):
        self.watch(self.first, 2950)
        self.watch(self.second, 2950)
        card = self.card()
        self.assertEqual(card["id"], self.third["id"])
        self.assertNotIn("progress", card)


@unittest.skipUnless(FFMPEG, "ffmpeg absent")
class TestPlayPlan(SpoilerCase):
    def play(self, entry):
        status, data = self.api.json(f"/api/play/{entry['id']}", {"caps": CAPS_ALL})
        self.assertEqual(status, 200, data)
        return data

    def test_an_episode_never_started_and_its_next_have_no_progress(self):
        plan = self.play(self.first)
        self.assertNotIn("progress", plan)
        self.assertEqual(plan["next"]["id"], self.second["id"])
        self.assertNotIn("progress", plan["next"])

    def test_an_episode_in_progress_and_a_next_in_progress_say_so(self):
        self.watch(self.first, 600)
        self.watch(self.second, 300)
        plan = self.play(self.first)
        self.assertEqual((plan["progress"]["position"], plan["progress"]["done"]), (600, False))
        self.assertEqual((plan["next"]["progress"]["position"], plan["next"]["progress"]["done"]), (300, False))

    def test_a_seen_episode_and_a_seen_next_say_so(self):
        self.watch(self.first, 2950)
        self.watch(self.second, 2950)
        plan = self.play(self.first)
        self.assertTrue(plan["progress"]["done"])
        self.assertTrue(plan["next"]["progress"]["done"])


if __name__ == "__main__":
    unittest.main()
