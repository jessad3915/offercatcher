"""Offline regression coverage for Apple Reminders due-date parsing."""

import datetime as dt
import unittest

from scripts import apple_reminders_bridge


class ParseDueTests(unittest.TestCase):
    def test_accepts_documented_minute_and_second_precision(self) -> None:
        self.assertEqual(
            dt.datetime(2026, 4, 1, 9, 0),
            apple_reminders_bridge.parse_due("2026-04-01 09:00"),
        )
        self.assertEqual(
            dt.datetime(2026, 4, 1, 9, 0, 30),
            apple_reminders_bridge.parse_due("2026-04-01 09:00:30"),
        )

    def test_rejects_empty_and_invalid_due_values(self) -> None:
        for raw_due in (None, "", "2026-02-29 09:00", "2026-04-01 09:00 ", "重要事件"):
            with self.subTest(raw_due=raw_due):
                self.assertIsNone(apple_reminders_bridge.parse_due(raw_due))


if __name__ == "__main__":
    unittest.main()
