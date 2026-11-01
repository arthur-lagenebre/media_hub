"""L'en-tête Range : ce qu'on envoie d'un fichier quand le lecteur n'en demande qu'une partie."""

from __future__ import annotations

import unittest

from tests import fixtures
from hub import config

fixtures.REPO  # noqa: B018  (rend `libraries` importable)
config.use_media_library(fixtures.REPO)

from hub.server.app import range_bounds  # noqa: E402


class TestRangeBounds(unittest.TestCase):
    def test_without_a_usable_header_the_whole_file_is_sent(self):
        for header in (None, "", " ", "items=0-9", "bytes=abc", "bytes=-", "BYTES=0-1", "bytes=0-1,5-6"):
            self.assertEqual(range_bounds(header, 100), (0, 99, 200), header)

    def test_a_part_is_a_206(self):
        self.assertEqual(range_bounds("bytes=0-9", 100), (0, 9, 206))
        self.assertEqual(range_bounds(" bytes=10-19 ", 100), (10, 19, 206))
        self.assertEqual(range_bounds("bytes=90-", 100), (90, 99, 206))                       # jusqu'à la fin
        self.assertEqual(range_bounds("bytes=90-5000", 100), (90, 99, 206))                   # la fin est ramenée à celle du fichier
        self.assertEqual(range_bounds("bytes=-10", 100), (90, 99, 206))                       # les dix derniers octets
        self.assertEqual(range_bounds("bytes=-5000", 100), (0, 99, 206))                      # plus que le fichier : tout, mais en 206

    def test_a_part_outside_the_file_is_refused(self):
        for header in ("bytes=100-", "bytes=999999999-", "bytes=50-10"):
            self.assertIsNone(range_bounds(header, 100), header)

    def test_an_empty_file_has_no_part_to_ask_for(self):
        self.assertEqual(range_bounds(None, 0), (0, -1, 200))
        self.assertIsNone(range_bounds("bytes=0-", 0))
        self.assertIsNone(range_bounds("bytes=-5", 0))


if __name__ == "__main__":
    unittest.main()
