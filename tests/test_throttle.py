"""L'attente imposée aux essais de connexion ratés : chaque échec la rallonge, et passé la limite plus rien n'est vérifié."""

from __future__ import annotations

import unittest

from hub.accounts import AccountError, Throttle


class TestThrottle(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.throttle = Throttle(clock=lambda: self.now)

    def fail(self, times, key="a"):
        for _ in range(times):
            self.throttle.failed(key)

    def test_the_wait_doubles_after_the_free_attempts_up_to_the_cap(self):
        waits = []
        for fails in range(0, 10):
            self.now = 1000.0
            throttle = Throttle(clock=lambda: self.now)
            for _ in range(fails):
                throttle.failed("a")
            waits.append(throttle.delay("a"))
        self.assertEqual(waits, [0.4, 0.4, 0.4, 0.4, 0.8, 1.6, 3.2, 5.0, 5.0, 5.0])

    def test_after_the_limit_nothing_is_verified_and_the_message_says_when_to_come_back(self):
        self.fail(1)
        self.now += 10
        self.fail(9)
        self.now += 90
        with self.assertRaises(AccountError) as caught:
            self.throttle.delay("a")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.message, "trop d'essais : réessaie dans 201 s")            # le plus ancien échec date de 100 s : 300 - 100, puis la seconde commencée

    def test_one_failure_less_than_the_limit_is_still_allowed(self):
        self.fail(9)
        self.assertEqual(self.throttle.delay("a"), 5.0)

    def test_a_failure_older_than_the_window_is_forgotten(self):
        self.fail(10)
        self.now += 299.9
        with self.assertRaises(AccountError):
            self.throttle.delay("a")
        self.now = 1000.0 + 300.0                                   # pile la fenêtre : le plus ancien échec ne compte plus
        self.assertEqual(self.throttle.delay("a"), 0.4)

    def test_each_address_has_its_own_count(self):
        self.fail(10, "a")
        self.assertEqual(self.throttle.delay("b"), 0.4)

    def test_a_success_starts_the_count_again(self):
        self.fail(10)
        self.throttle.passed("a")
        self.assertEqual(self.throttle.delay("a"), 0.4)
        self.throttle.passed("inconnue")                            # sans effet


if __name__ == "__main__":
    unittest.main()
