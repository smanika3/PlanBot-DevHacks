"""Schedule agent: deterministic slot candidates, blocked-by, ranking. No LLM, no I/O.

Times are naive local datetimes in the group's single time zone (America/Phoenix for the demo).
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

DURATION = {"golf": 240, "dinner": 90, "bowling": 90, "study": 120}  # minutes
DEFAULT_DURATION = 120
START_HOURS = {"golf": (7, 13), "dinner": (17, 20), "bowling": (12, 20), "study": (9, 17)}  # earliest, latest start
DEFAULT_START_HOURS = (9, 20)
TRAVEL = timedelta(minutes=30)
STEP = timedelta(minutes=30)
DAYS = 7
SPACING = timedelta(hours=2)  # options on the same day must start at least this far apart


@dataclass
class Slot:
    start: datetime
    end: datetime
    ok: list = field(default_factory=list)       # names confirmed free
    unknown: list = field(default_factory=list)  # names with no availability given yet
    blocked: list = field(default_factory=list)  # names who cannot (never says why)


def _overlaps(s, e, windows):
    return any(s < we and ws < e for ws, we in windows)


def status(avail: dict, s: datetime, e: datetime) -> str:
    """'ok' | 'unknown' | 'blocked' for one person. avail = {"free": [(s,e)], "busy": [(s,e)]}."""
    if _overlaps(s, e, avail["busy"]):
        return "blocked"
    if avail["free"]:
        return "ok" if any(ws <= s and e <= we for ws, we in avail["free"]) else "blocked"
    return "unknown"


def candidates(kind: str, people: dict, now: datetime, earliest: datetime = None) -> list:
    """Every slot in the next DAYS days for this kind. people = {name: avail}."""
    dur = timedelta(minutes=DURATION.get(kind, DEFAULT_DURATION))
    lo, hi = START_HOURS.get(kind, DEFAULT_START_HOURS)
    floor = max(now, earliest) if earliest else now
    out = []
    for d in range(DAYS + 1):
        day = (now + timedelta(days=d)).replace(hour=0, minute=0, second=0, microsecond=0)
        t = day + timedelta(hours=lo)
        while t <= day + timedelta(hours=hi):
            if t >= floor:
                slot = Slot(t, t + dur)
                for name, av in people.items():
                    getattr(slot, status(av, t, t + dur)).append(name)
                out.append(slot)
            t += STEP
    return out


def rank_key(s: Slot):
    return (len(s.blocked), -len(s.ok), s.start)


def options_for(kind: str, people: dict, now: datetime, earliest: datetime = None, n: int = 3) -> list:
    """Top n well-spread slots: fewest blocked, then most confirmed, then earliest."""
    chosen = []
    for s in sorted(candidates(kind, people, now, earliest), key=rank_key):
        if all(abs(s.start - c.start) >= SPACING or s.start.date() != c.start.date() for c in chosen):
            chosen.append(s)
        if len(chosen) == n:
            break
    return chosen


def plan_segments(segments: list, now: datetime, n: int = 3) -> list:
    """segments = [(kind, people)] or [(kind, people, locked_end)] in order. Later segments start after the leading
    option of the previous one ends plus travel, so a shifted golf slot visibly moves dinner. A locked segment
    yields no options and its real end is used instead of a leading option's."""
    result, earliest = [], None
    for kind, people, *fixed in segments:
        if fixed and fixed[0]:
            result.append([])
            earliest = fixed[0] + TRAVEL
            continue
        opts = options_for(kind, people, now, earliest, n)
        result.append(opts)
        earliest = opts[0].end + TRAVEL if opts else earliest
    return result
