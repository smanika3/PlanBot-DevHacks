"""Recompute the provisional options for one event: Preferences -> Schedule -> Venue -> saved options.

The roles are separate modules with distinct inputs and outputs: db.constraints (Preferences, aggregate only),
schedule (ranked slots, blocked-by names), venue (shortlist and hours check). This module only wires them.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import db
import schedule
import venue

MIN_REPLIED = 2  # people who have shared availability before options are shown


def local_now() -> datetime:
    """Naive local time in the group's single time zone."""
    return datetime.now(ZoneInfo(os.environ.get("TZ_NAME", "America/Phoenix"))).replace(tzinfo=None)


def ready(snap: dict) -> bool:
    return snap["planning"] and snap["replied"] >= MIN_REPLIED


def run(event_id: int, now: datetime, venues: list, hours_fn=venue.check_hours) -> dict:
    """Compute and save options for every segment. Returns the status lines shown on the card."""
    snap = db.snapshot(event_id)
    c = snap["constraints"]
    segs = snap["segments"]
    slots = schedule.plan_segments([(s["kind"], db.people_for_segment(s["id"]),
                                     s["locked"] and datetime.fromisoformat(s["locked"]["end"])) for s in segs], now)
    checked = unverified = 0
    notes = []
    for seg, seg_slots in zip(segs, slots):
        if seg["locked"]:
            continue
        shortlist, seg_notes = venue.shortlist(seg["kind"], c, venues)
        notes += [f"{seg['kind']}: {n}" for n in seg_notes]
        used, opts = set(), []
        for slot in seg_slots:
            if not shortlist:
                opts.append(_option(slot, None, {"status": "unverified", "checked_at": None, "source": None}))
                continue
            for v in sorted(shortlist, key=lambda v: v["id"] in used):  # prefer a venue not already offered
                h = hours_fn(v, slot.start, slot.end, now)
                checked += 1
                unverified += h["status"] == "unverified"
                if h["status"] != "closed":
                    used.add(v["id"])
                    opts.append(_option(slot, v, h))
                    break
        db.save_options(seg["id"], opts)
    n_shared = len(c["diet"]) + bool(c["budget"]) + bool(c["area"])
    return {
        "Preferences": f"{n_shared} group need(s) shared, no names",
        "Schedule": f"ranked slots for {len(segs)} activit{'y' if len(segs) == 1 else 'ies'}",
        "Venue": f"checked {checked} venue-slot pair(s), {unverified} unverified" + (f"; {'; '.join(notes)}" if notes else ""),
    }


def _option(slot, v, h) -> dict:
    return {
        "start": slot.start.isoformat(timespec="minutes"), "end": slot.end.isoformat(timespec="minutes"),
        "venue_id": v and v["id"], "venue_name": v and v["name"], "address": v and v["address"],
        "source_url": h["source"] or (v and v["website"]),
        "hours_status": "verified" if h["status"] == "open" else h["status"], "checked_at": h["checked_at"],
        "blocked_by": slot.blocked, "unknown_n": len(slot.unknown),
    }
