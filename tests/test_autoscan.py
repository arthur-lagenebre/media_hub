"""Le rescan périodique (HUB_AUTOSCAN_HOURS) : un fil qui dort, puis relance un scan si rien d'autre ne tourne."""

from __future__ import annotations

import types
import unittest
from unittest import mock

from tests import fixtures
from hub import config

fixtures.REPO  # noqa: B018  (rend `libraries` importable)
config.use_media_library(fixtures.REPO)

from hub.server import app  # noqa: E402


class Stop(Exception):
    """Fait sortir la boucle infinie du fil."""


def hub(hours, busy=False):
    return types.SimpleNamespace(cfg=types.SimpleNamespace(autoscan_hours=hours), jobs=mock.Mock(busy=mock.Mock(return_value=busy)))


class TestAutoscan(unittest.TestCase):
    def test_without_hours_no_thread_is_started(self):
        for hours in (0, -3):
            with mock.patch.object(app.threading, "Thread") as thread:
                app.Hub.autoscan(hub(hours))
            thread.assert_not_called()

    def start(self, fake):
        """Lance autoscan et rend le travail du fil, sans fil."""
        with mock.patch.object(app.threading, "Thread") as thread:
            app.Hub.autoscan(fake)
        thread.assert_called_once()
        self.assertEqual((thread.call_args.kwargs["name"], thread.call_args.kwargs["daemon"]), ("autoscan", True))
        thread.return_value.start.assert_called_once_with()
        return thread.call_args.kwargs["target"]

    def test_the_thread_sleeps_for_the_period_then_scans_everything(self):
        fake = hub(2)
        loop = self.start(fake)
        with mock.patch.object(app.time, "sleep", side_effect=[None, Stop]) as sleep, self.assertRaises(Stop):
            loop()
        self.assertEqual([call.args for call in sleep.call_args_list], [(7200,), (7200,)])
        fake.jobs.scan_all.assert_called_once_with()

    def test_a_scan_already_running_is_not_doubled(self):
        fake = hub(1, busy=True)
        loop = self.start(fake)
        with mock.patch.object(app.time, "sleep", side_effect=[None, Stop]), self.assertRaises(Stop):
            loop()
        fake.jobs.scan_all.assert_not_called()


if __name__ == "__main__":
    unittest.main()
