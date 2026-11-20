"""Les recommandations vues par la route : ce qu'un rayon montre à un compte (le détail du calcul est dans test_recommend, que la passe de nuit de mutator ne lance pas pour le serveur)."""

from __future__ import annotations

import unittest

from tests.test_recommend import FILMS, RecommendCase, film


class TestWhatTheRouteShows(RecommendCase):
    def test_a_card_carries_the_opinion_of_the_account_and_one_without_opinion_carries_none(self):
        self.rate(self.alice, "a1", "a2")
        self.rate(self.alice, "c_dir", value="mid")
        self.rate(self.bob, "c_act", value="up")                                # l'avis d'un autre compte ne se voit pas
        cards = {c["id"]: c for s in self.shelves(self.alice) if s["anchor"]["id"] == "a1" for c in s["items"]}
        self.assertEqual(cards["c_dir"]["rating"], "mid")
        self.assertNotIn("rating", cards["c_act"])

    def test_only_what_the_account_can_play_is_proposed(self):
        for who, secret in ((self.alice, True), (self.bob, False)):
            self.rate(who, "a1", "a2")
            shown = {i for items in self.by_anchor(who).values() for i in items}
            self.assertEqual(bool(shown & {"p1", "p2"}), secret, who["id"])      # Bob voit « prive » mais ne peut pas y lire : rien n'en sort pour lui
            self.assertTrue(shown, who["id"])

    def test_a_rating_of_the_tmdb_lifts_among_equals(self):
        rows = {r["id"]: r for r in FILMS}
        rows["c_sf4"] = film("c_sf4", "Tenet", genres=["SF"], year="2020", vote=9.0)
        self.fill("films", list(rows.values()))
        self.rate(self.alice, "a1", "a2")
        order = [i for i in self.by_anchor(self.alice)["a1"] if i.startswith("c_sf")]
        self.assertEqual(order, ["c_sf4", "c_sf2", "c_sf1", "c_sf3"])


if __name__ == "__main__":
    unittest.main()
