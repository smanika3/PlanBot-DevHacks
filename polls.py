"""Conflict and reschedule polls. Pure logic + SQLite, no discord, so it is testable without fakes.

Polls live in their own tables (created lazily) and are persisted with a deadline, so a restart loses nothing.
Notices name people only where needed and never say why anyone is busy.
"""
import json
import os
import time
from collections import Counter
from datetime import datetime, timedelta

import db
import schedule

SCHEMA = """
CREATE TABLE IF NOT EXISTS polls(
  id INTEGER PRIMARY KEY, kind TEXT, event_id INTEGER, segment_id INTEGER, subject_id INTEGER,
  status TEXT DEFAULT 'open', choices TEXT, electorate TEXT, opened_by INTEGER, opened_at REAL, deadline REAL,
  result TEXT, message_id INTEGER, tiebreak TEXT);
CREATE TABLE IF NOT EXISTS poll_votes(
  poll_id INTEGER, person_id INTEGER, choice TEXT, PRIMARY KEY(poll_id, person_id));
"""
DEFAULT = {"conflict": "everyone", "reschedule": "no"}  # what an empty or unbreakable poll falls back to
_ready = None


def _con():
    """db.con with our tables present. Re-runs when the connection changes (tests use fresh ':memory:' ones)."""
    global _ready
    if _ready is not db.con:
        db.con.executescript(SCHEMA)
        _ready = db.con
    return db.con


def _now(now):
    return time.time() if now is None else now


# ---- generic core ---------------------------------------------------------------------------------------------

def open_poll(kind, event_id, segment_id, choices, electorate, opened_by, subject_id=None, now=None, ttl=None):
    """New poll id, or None if a conflict/reschedule poll is already open for this segment."""
    con = _con()
    if con.execute("SELECT 1 FROM polls WHERE segment_id=? AND status='open'", (segment_id,)).fetchone():
        return None
    now = _now(now)
    ttl = ttl if ttl is not None else float(os.environ.get("POLL_TTL_SECONDS", 3600))
    pid = con.execute(
        "INSERT INTO polls(kind,event_id,segment_id,subject_id,choices,electorate,opened_by,opened_at,deadline) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (kind, event_id, segment_id, subject_id, json.dumps(list(choices)), json.dumps(sorted(electorate)),
         opened_by, now, now + ttl)).lastrowid
    con.commit()
    return pid


def get(poll_id) -> dict:
    con = _con()
    r = con.execute("SELECT * FROM polls WHERE id=?", (poll_id,)).fetchone()
    if r is None:
        return None
    votes = {v["person_id"]: v["choice"] for v in con.execute("SELECT * FROM poll_votes WHERE poll_id=?", (poll_id,))}
    return {**dict(r), "choices": json.loads(r["choices"]), "electorate": json.loads(r["electorate"]), "votes": votes}


def cast(poll_id, person_id, choice, now=None) -> bool:
    p = get(poll_id)
    if (not p or p["status"] != "open" or _now(now) >= p["deadline"]
            or person_id not in p["electorate"] or choice not in p["choices"]):
        return False
    _con().execute("INSERT OR REPLACE INTO poll_votes VALUES(?,?,?)", (poll_id, person_id, choice))
    _con().commit()
    return True


def proposer_of(p) -> int:
    return _con().execute("SELECT proposer_id FROM events WHERE id=?", (p["event_id"],)).fetchone()["proposer_id"]


def proposer_break(poll_id, proposer_id, choice) -> bool:
    """The organizer settles a tie even when they are not voting (e.g. they are the one who is busy)."""
    p = get(poll_id)
    if not p or p["status"] != "open" or proposer_id != proposer_of(p) or choice not in p["choices"]:
        return False
    _con().execute("UPDATE polls SET tiebreak=? WHERE id=?", (choice, poll_id))
    _con().commit()
    return True


def decide(poll_id, proposer_id, now=None):
    """('pending'|'decided'|'needs_tiebreak', winner). Pure read: closing is resolve()'s job."""
    p = get(poll_id)
    if p["status"] == "closed":
        return "decided", p["result"]
    n, votes = len(p["electorate"]), p["votes"]
    counts = Counter(votes.values())
    for c, k in counts.items():
        if k * 2 > n:
            return "decided", c
    over = _now(now) >= p["deadline"]
    if len(votes) < n and not over:
        return "pending", None
    default = DEFAULT[p["kind"]]
    if not counts:
        return "decided", default
    top = max(counts.values())
    tied = [c for c in p["choices"] if counts[c] == top]
    if len(tied) == 1:
        return "decided", tied[0]
    for pick in (votes.get(proposer_id), p["tiebreak"]):
        if pick in tied:
            return "decided", pick
    if over:  # nobody broke the tie in time
        return "decided", default if default in tied else tied[0]
    return "needs_tiebreak", None


def close(poll_id, result):
    _con().execute("UPDATE polls SET status='closed', result=? WHERE id=?", (result, poll_id))
    _con().commit()


def resolve(poll_id, now=None):
    """decide() and close the poll if it is decided. Returns ('closed', result) for an already closed poll."""
    p = get(poll_id)
    if p["status"] == "closed":
        return "closed", p["result"]
    state, winner = decide(poll_id, proposer_of(p), now)
    if state == "decided":
        close(poll_id, winner)
    return state, winner


def due(now=None) -> list:
    return [r["id"] for r in _con().execute("SELECT id FROM polls WHERE status='open' AND deadline<=?", (_now(now),))]


def set_message(poll_id, message_id):
    _con().execute("UPDATE polls SET message_id=? WHERE id=?", (message_id, poll_id))
    _con().commit()


# ---- helpers --------------------------------------------------------------------------------------------------

def _seg(segment_id):
    return _con().execute("SELECT * FROM segments WHERE id=?", (segment_id,)).fetchone()


def name_of(person_id) -> str:
    r = _con().execute("SELECT name FROM persons WHERE id=?", (person_id,)).fetchone()
    return r["name"] if r else "someone"


def set_status(person_id, segment_id, status):
    _con().execute("INSERT OR REPLACE INTO participation VALUES(?,?,?)", (person_id, segment_id, status))
    _con().commit()


def _in(segment_id) -> set:
    return {r["person_id"] for r in _con().execute(
        "SELECT person_id FROM participation WHERE segment_id=? AND status='in'", (segment_id,))}


def when(dt: datetime) -> str:
    """'Sat 11am' / 'Sat 6:30pm'."""
    return f"{dt:%a} " + dt.strftime("%-I%p" if dt.minute == 0 else "%-I:%M%p").lower()


# ---- conflict flow --------------------------------------------------------------------------------------------

def _already_polled(segment_id, person_id) -> bool:
    return _con().execute("SELECT 1 FROM polls WHERE kind='conflict' AND segment_id=? AND subject_id=?",
                          (segment_id, person_id)).fetchone() is not None


def detect_conflicts(event_id, now=None) -> list:
    """Run after a recompute. If the leading option of an unlocked segment lists an 'in' participant as blocked,
    mark them tentative_out and open one conflict poll. Returns the new poll ids.
    ponytail: names are assumed unique within an event (blocked_by holds names, not ids)."""
    con = _con()
    ev = con.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not ev or ev["status"] in ("cancelled", "done"):
        return []
    created = []
    for seg in con.execute("SELECT * FROM segments WHERE event_id=? AND status!='locked' ORDER BY ord", (event_id,)).fetchall():
        opts = db.options(seg["id"])
        for name in (opts[0]["blocked_by"] if opts else []):
            r = con.execute("SELECT p.id FROM persons p JOIN participation q ON q.person_id=p.id "
                            "WHERE p.name=? AND q.segment_id=? AND q.status='in'", (name, seg["id"])).fetchone()
            if not r or _already_polled(seg["id"], r["id"]):
                continue
            voters = _in(seg["id"]) - {r["id"]}
            pid = voters and open_poll("conflict", event_id, seg["id"], ("without", "everyone"), voters,
                                       ev["proposer_id"], r["id"], now)
            if pid:  # None or empty: another vote is open here, or nobody is left to vote
                set_status(r["id"], seg["id"], "tentative_out")
                created.append(pid)
            break  # one vote at a time per segment
    return created


def restore(poll_id, person_id, recompute) -> bool:
    """'I can make it': only the tentative-out person, only while the poll is open."""
    p = get(poll_id)
    if not p or p["kind"] != "conflict" or p["status"] != "open" or p["subject_id"] != person_id:
        return False
    set_status(person_id, p["segment_id"], "in")
    close(poll_id, "cancelled")
    recompute(p["event_id"])
    return True


def _later(segment_id):
    s = _seg(segment_id)
    return _con().execute("SELECT * FROM segments WHERE event_id=? AND ord>? ORDER BY ord",
                          (s["event_id"], s["ord"])).fetchall()


def alternatives(segment_id, now: datetime) -> str:
    """Top slots with counts and the knock-on effect on later segments. Only slots the schedule already ranks."""
    s = _seg(segment_id)
    segs = _con().execute("SELECT * FROM segments WHERE event_id=? ORDER BY ord", (s["event_id"],)).fetchall()
    people = [db.people_for_segment(x["id"]) for x in segs]
    i = [x["id"] for x in segs].index(segment_id)
    opts = schedule.plan_segments([(x["kind"], p) for x, p in zip(segs, people)], now)[i]
    if not opts:
        return f"No open slots found for {s['kind']} in the next week."
    n, lines = len(people[i]), []
    for o in opts:
        line = f"{when(o.start)}: {len(o.ok)} of {n} free" + (f", {len(o.unknown)} unconfirmed" if o.unknown else "")
        line += _knock_on(segs, people, i, o, now)
        lines.append("• " + line)
    ask = f"\nStill blocked at the top slot: {', '.join(opts[0].blocked)}. Please share more times." if opts[0].blocked else ""
    return f"Best times for {s['kind']}:\n" + "\n".join(lines) + ask


def _knock_on(segs, people, i, slot, now) -> str:
    moves, earliest = [], slot.end + schedule.TRAVEL
    for j in range(i + 1, len(segs)):
        lead = schedule.options_for(segs[j]["kind"], people[j], now, earliest, 1)
        if not lead:
            moves.append(f"{segs[j]['kind']} has no slot after that")
            continue
        cur = db.options(segs[j]["id"])
        if not cur or datetime.fromisoformat(cur[0]["start"]) != lead[0].start:
            moves.append(f"{segs[j]['kind']} would move to {when(lead[0].start)}")
        earliest = lead[0].end + schedule.TRAVEL
    return " (" + "; ".join(moves) + ")" if moves else ""


# ---- reschedule flow ------------------------------------------------------------------------------------------

def pick_segment(event_id, person_id, activity=None):
    """(segment_id, None) or (None, message) for /reschedule."""
    rows = _con().execute(
        "SELECT s.id, s.kind FROM segments s JOIN participation q ON q.segment_id=s.id "
        "WHERE s.event_id=? AND q.person_id=? AND q.status='in' ORDER BY s.ord", (event_id, person_id)).fetchall()
    if not rows:
        return None, "Only people who are in an activity can ask to reschedule it."
    if activity:
        hit = [r["id"] for r in rows if r["kind"] == activity.strip().lower()]
        return (hit[0], None) if hit else (None, f"You're not in '{activity}'. You're in: {', '.join(r['kind'] for r in rows)}.")
    if len(rows) == 1:
        return rows[0]["id"], None
    return None, f"Which one? Use `/reschedule activity:<name>` with one of: {', '.join(r['kind'] for r in rows)}."


def request_reschedule(segment_id, person_id, now=None):
    """Poll id, or None if the person is not in the segment or a vote is already open for it."""
    voters = db.segment_participants(segment_id)
    if person_id not in voters:
        return None
    return open_poll("reschedule", db.event_of_segment(segment_id), segment_id, ("yes", "no"), voters, person_id, now=now)


def reopens(segment_id) -> list:
    return [r["kind"] for r in _later(segment_id)]


def blocked_now(segment_id) -> list:
    opts = db.options(segment_id)
    return opts[0]["blocked_by"] if opts else []


# ---- rendering and outcomes -----------------------------------------------------------------------------------

def render(poll_id) -> str:
    """Open-poll text: live counts only, never who voted what."""
    p = get(poll_id)
    kind = _seg(p["segment_id"])["kind"]
    if p["kind"] == "conflict":
        opts = db.options(p["segment_id"])
        at = f" at {when(datetime.fromisoformat(opts[0]['start']))}" if opts else ""
        head = (f"**{name_of(p['subject_id'])}** may not make **{kind}**{at} and is tentatively out for now. "
                "Go without them, or find a time for everyone?")
    else:
        later = reopens(p["segment_id"])
        head = (f"**{name_of(p['opened_by'])}** asked to reschedule **{kind}**. Move it?"
                + (f" If yes, {', '.join(later)} reopens too." if later else ""))
    return f"{head}\n{len(p['votes'])} of {len(p['electorate'])} voted · closes <t:{int(p['deadline'])}:R>"


def finish(poll_id, winner, recompute, now: datetime):
    """Apply a decided poll. Returns (outcome line for the poll message, follow-up message or None).
    recompute(event_id) is the client's schedule_recompute."""
    p = get(poll_id)
    seg, kind = p["segment_id"], _seg(p["segment_id"])["kind"]
    if p["kind"] == "conflict":
        who = name_of(p["subject_id"])
        if winner == "without":
            set_status(p["subject_id"], seg, "out")
            recompute(p["event_id"])
            return f"Decided: going without {who} for {kind}.", None
        set_status(p["subject_id"], seg, "in")
        recompute(p["event_id"])
        return f"Decided: finding a time for everyone for {kind}.", alternatives(seg, now)
    if winner != "yes":
        return f"Reschedule of {kind} did not pass.", None
    later, blocked = reopens(seg), blocked_now(seg)
    db.unlock_segment(seg)
    recompute(p["event_id"])
    note = f"Reschedule passed: {kind} is open again" + (f", and {', '.join(later)} reopens with it." if later else ".")
    note += " Your earlier times still count; only share what changed."
    if blocked:
        note += f" Blocked at the old time: {', '.join(blocked)}."
    return note, None
