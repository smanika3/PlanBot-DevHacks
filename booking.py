"""Booking coordination after a segment locks. Pure logic + SQLite, no discord.

The bot never books or pays: it asks who will, tells that person the details, and reports their word.
State lives in one row per segment, created lazily, so a restart loses nothing.
"""
import os
import time
from datetime import datetime

import db
import polls
import venue

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings(
  segment_id INTEGER PRIMARY KEY, state TEXT, volunteer_id INTEGER, deadline REAL, reminded INTEGER DEFAULT 0,
  locked_at REAL);
"""
_ready = None


def _con():
    """db.con with our table present. Re-runs when the connection changes (tests use fresh ':memory:' ones)."""
    global _ready
    if _ready is not db.con:
        db.con.executescript(SCHEMA)
        _ready = db.con
    return db.con


def _now(now):
    return time.time() if now is None else now


def _ttl():
    return float(os.environ.get("BOOK_TTL_SECONDS", 3600))


def live(segment_id) -> bool:
    """Segment still locked and its event not cancelled. Every action checks this, so stale buttons go quiet."""
    r = _con().execute("SELECT e.status FROM segments s JOIN events e ON e.id=s.event_id WHERE s.id=?",
                       (segment_id,)).fetchone()
    return bool(r) and r["status"] != "cancelled" and db.locked(segment_id) is not None


def state(segment_id):
    """Raw row as a dict, or None."""
    r = _con().execute("SELECT * FROM bookings WHERE segment_id=?", (segment_id,)).fetchone()
    if r and r["locked_at"] != _locked_at(segment_id):  # unlocked and re-locked since: old state is void
        reset(segment_id)
        return None
    return dict(r) if r else None


def _locked_at(segment_id):
    r = _con().execute("SELECT locked_at FROM locks WHERE segment_id=?", (segment_id,)).fetchone()
    return r and r["locked_at"]


def status(segment_id) -> str:
    """'none' | 'open' | 'volunteered' | 'booked'."""
    r = state(segment_id)
    return r["state"] if r and live(segment_id) else "none"


def booked_by(segment_id):
    r = state(segment_id)
    return polls.name_of(r["volunteer_id"]) if r and r["state"] == "booked" and live(segment_id) else None


def open_poll(segment_id, now=None):
    """Deadline (epoch seconds) of the new volunteer poll, or None if not live or one already exists."""
    if not live(segment_id) or state(segment_id):
        return None
    deadline = _now(now) + _ttl()
    _con().execute("INSERT INTO bookings(segment_id,state,deadline,locked_at) VALUES(?,'open',?,?)",
                  (segment_id, deadline, _locked_at(segment_id)))
    _con().commit()
    return deadline


def volunteer(segment_id, person_id, now=None):
    """(True, their name) if this person is now the volunteer, else (False, current volunteer's name or None)."""
    r = state(segment_id)
    if not live(segment_id) or not r or person_id not in db.segment_participants(segment_id):
        return False, None
    if r["state"] == "open":
        cur = _con().execute("UPDATE bookings SET state='volunteered', volunteer_id=? WHERE segment_id=? AND state='open'",
                             (person_id, segment_id))
        _con().commit()
        if cur.rowcount:
            return True, polls.name_of(person_id)
        r = state(segment_id)
    return False, polls.name_of(r["volunteer_id"]) if r["volunteer_id"] else None


def _move(segment_id, person_id, frm, sets, args=()) -> bool:
    """Only the volunteer, only from state `frm`, only while live."""
    if not live(segment_id):
        return False
    cur = _con().execute(f"UPDATE bookings SET {sets} WHERE segment_id=? AND state=? AND volunteer_id=?",
                         (*args, segment_id, frm, person_id))
    _con().commit()
    return cur.rowcount > 0


def hand_back(segment_id, person_id, now=None) -> bool:
    """Reopen the poll with a fresh deadline and one more reminder."""
    return _move(segment_id, person_id, "volunteered", "state='open', volunteer_id=NULL, reminded=0, deadline=?",
                 (_now(now) + _ttl(),))


def mark_booked(segment_id, person_id) -> bool:
    # ponytail: takes the volunteer's word; no confirmation number or day-before check.
    return _move(segment_id, person_id, "volunteered", "state='booked'")


def reset(segment_id):
    """Forget all booking state. Call after db.unlock_segment, for every segment it reopens."""
    _con().execute("DELETE FROM bookings WHERE segment_id=?", (segment_id,))
    _con().commit()


def due(now=None) -> list:
    """Segments whose poll passed its deadline with no volunteer. Each is returned once, then never nagged again."""
    ids = [r["segment_id"] for r in _con().execute(
        "SELECT segment_id FROM bookings WHERE state='open' AND reminded=0 AND deadline<=?", (_now(now),)).fetchall()
        if live(r["segment_id"]) and state(r["segment_id"])]
    for sid in ids:
        _con().execute("UPDATE bookings SET reminded=1 WHERE segment_id=?", (sid,))
    _con().commit()
    return ids


def kind_of(segment_id) -> str:
    return _con().execute("SELECT kind FROM segments WHERE id=?", (segment_id,)).fetchone()["kind"]


def dm_text(segment_id) -> str:
    """Details for the volunteer. Diet is the group aggregate (meals only); nobody is named."""
    lock, kind = db.locked(segment_id), kind_of(segment_id)
    lines = [f"You volunteered to book {kind}. Here is what the group agreed:"]
    lines.append(f"Venue: {lock['venue_name'] or 'none chosen'}" + (f" ({lock['source_url']})" if lock["source_url"] else ""))
    if lock["address"]:
        lines.append(f"Address: {lock['address']}")
    lines.append(f"When: {polls.when(datetime.fromisoformat(lock['start']))}")
    lines.append(f"Party size: {len(db.segment_participants(segment_id))}")
    diet = db.constraints(db.event_of_segment(segment_id))["diet"] if kind in venue.MEAL else []
    if diet:
        lines.append("Dietary needs in the group: " + ", ".join(diet))
    lines.append("I will not book or pay for anything. Please book it yourself, then tap 'I booked it'. "
                 "If you can't, tap 'Hand back'.")
    return "\n".join(lines)
