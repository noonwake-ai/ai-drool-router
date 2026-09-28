# SPDX-FileCopyrightText: 2026 NoonWake.AI
# SPDX-License-Identifier: LGPL-3.0-or-later

from datetime import datetime
import unittest

from detector.schedule import BEIJING, current_slot, next_slot, window_slots


def at(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=BEIJING).timestamp())


class ScheduleTests(unittest.TestCase):
    def test_regular_and_quiet_windows_have_exact_boundaries(self):
        checks = [('03:15','04:00'),('04:00','05:30'),('05:30','07:00'),('07:00','08:00'),
                  ('08:00','08:45'),('08:45','09:30'),('23:00','23:45')]
        for before, after in checks:
            with self.subTest(before=before):
                now=at('2026-09-14T'+before)
                self.assertEqual(current_slot(now),now)
                self.assertEqual(next_slot(now),at('2026-09-14T'+after))

    def test_midnight_and_in_between_ticks_do_not_create_new_cycles(self):
        self.assertEqual(next_slot(at('2026-09-13T23:45')),at('2026-09-14T00:15'))
        self.assertEqual(current_slot(at('2026-09-14T00:00')),at('2026-09-13T23:45'))
        self.assertEqual(current_slot(at('2026-09-14T05:15')),at('2026-09-14T04:00'))

    def test_rolling_history_is_24_hours_not_24_cycles(self):
        now=at('2026-09-14T12:13')
        slots=window_slots(now)
        self.assertEqual(len(slots),30)
        self.assertTrue(all(now-86400<=slot<=now for slot in slots))
        self.assertEqual(len(slots),len(set(slots)))


if __name__=='__main__':unittest.main()
