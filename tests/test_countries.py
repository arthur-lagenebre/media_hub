"""Le pays d'un artiste, en français sur sa fiche."""

from __future__ import annotations

import unittest

from tests import fixtures
from hub import config, countries

fixtures.REPO  # noqa: B018  (rend `libraries` importable)
config.use_media_library(fixtures.REPO)

from hub.server import app  # noqa: E402


class TestCountries(unittest.TestCase):
    def test_a_country_is_given_in_french(self):
        self.assertEqual([countries.french(c) for c in ("Sweden", "United States", "Germany")], ["Suède", "États-Unis", "Allemagne"])

    def test_what_is_not_a_country_keeps_its_name(self):
        self.assertEqual((countries.french("Stockholm"), countries.french(None), countries.french("")), ("Stockholm", "", ""))

    def test_the_sheet_shows_the_country_in_french(self):
        info = {"type": "Group", "area": "Sweden", "begin": "1998", "end": "2016", "ended": True}
        self.assertEqual(app.artist_sheet(info)["facts"], "Groupe · Suède · 1998 – 2016")


if __name__ == "__main__":
    unittest.main()
