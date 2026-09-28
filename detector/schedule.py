# SPDX-FileCopyrightText: 2026 NoonWake.AI
# SPDX-License-Identifier: LGPL-3.0-or-later

"""Slot schedule derived from config: a regular cadence plus a quieter night window.

Defaults to Beijing time, a 45-minute cadence, and 90 minutes between 04:00 and
08:00. Every timestamp is a Unix second so the worker can be restarted at any
moment without replaying a completed slot.
"""
from datetime import datetime, timedelta, timezone
import time

from . import config

_TZ_OFFSETS = {'Asia/Shanghai': 8, 'Asia/Tokyo': 9, 'Asia/Singapore': 8, 'UTC': 0}
_schedule = config.get('schedule') or {}
TIMEZONE = _schedule.get('timezone') or 'Asia/Shanghai'
BEIJING = timezone(timedelta(hours=_TZ_OFFSETS.get(TIMEZONE, 8)))

REGULAR_MINUTES = max(1, int(_schedule.get('regular_minutes') or 45))
QUIET_MINUTES = max(1, int(_schedule.get('quiet_minutes') or 90))
HISTORY_SECONDS = max(1, int(config.get('retention.artifacts_hours', 24))) * 3600


def _minutes(text, fallback):
    try:
        hours, minutes = str(text).split(':', 1)
        value = int(hours) * 60 + int(minutes)
    except (AttributeError, ValueError):
        return fallback
    return value if 0 <= value < 1440 else fallback


QUIET_START = str(_schedule.get('quiet_start') or '04:00')
QUIET_END = str(_schedule.get('quiet_end') or '08:00')
QUIET_START_MINUTES = _minutes(QUIET_START, 240)
QUIET_END_MINUTES = _minutes(QUIET_END, 480)


def cycle_slots(day):
    """All cadence anchors for one calendar day, in ascending order."""
    midnight = datetime.combine(day, datetime.min.time(), tzinfo=BEIJING)
    quiet_start = midnight + timedelta(minutes=QUIET_START_MINUTES)
    quiet_end = midnight + timedelta(minutes=QUIET_END_MINUTES)
    if quiet_end <= quiet_start:
        quiet_end += timedelta(days=1)
    regular = timedelta(minutes=REGULAR_MINUTES)
    quiet = timedelta(minutes=QUIET_MINUTES)
    slots = []
    moment = quiet_start
    while moment > midnight:
        moment -= regular
        if moment >= midnight:
            slots.append(moment)
    moment = quiet_start
    while moment < quiet_end:
        slots.append(moment)
        moment += quiet
    moment = quiet_end
    end = midnight + timedelta(days=1)
    while moment < end:
        slots.append(moment)
        moment += regular
    return [int(moment.timestamp()) for moment in sorted(slots)]


def nearby_slots(now=None):
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now, BEIJING).date()
    return sorted(slot for offset in (-2, -1, 0, 1) for slot in cycle_slots(today + timedelta(days=offset)))


def current_slot(now=None):
    now = time.time() if now is None else now
    return max(slot for slot in nearby_slots(now) if slot <= now)


def next_slot(now=None):
    now = time.time() if now is None else now
    return min(slot for slot in nearby_slots(now) if slot > now)


def window_slots(now=None):
    now = time.time() if now is None else now
    return [slot for slot in nearby_slots(now) if now - HISTORY_SECONDS <= slot <= now]


def schedule_info(now=None):
    now = time.time() if now is None else now
    slot, following = current_slot(now), next_slot(now)
    return {'timezone': TIMEZONE, 'regular_seconds': REGULAR_MINUTES * 60,
            'quiet_seconds': QUIET_MINUTES * 60,
            'quiet_start': QUIET_START, 'quiet_end': QUIET_END,
            'sync_time': QUIET_START,
            'current_slot': slot, 'next_slot': following,
            'interval_seconds': following - slot}
