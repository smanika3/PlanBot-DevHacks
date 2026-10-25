"""SQLite plan state. One connection, tiny queries, so plain sync calls are fine in the event loop."""
import hashlib
import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS persons(id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, channel_id INTEGER, proposer_id INTEGER,
  created_at REAL, status TEXT DEFAULT 'collecting', card_message_id INTEGER);
CREATE TABLE IF NOT EXISTS segments(
  id INTEGER PRIMARY KEY, event_id INTEGER, kind TEXT, ord INTEGER, status TEXT DEFAULT 'planning');
CREATE TABLE IF NOT EXISTS participation(
  person_id INTEGER, segment_id INTEGER, status TEXT, PRIMARY KEY(person_id, segment_id));
CREATE TABLE IF NOT EXISTS prefs(
  person_id INTEGER PRIMARY KEY, diet TEXT, diet_hard INTEGER DEFAULT 0, budget INTEGER,
  area TEXT, area_hard INTEGER DEFAULT 0, updated_at REAL);
CREATE TABLE IF NOT EXISTS avail(
  id INTEGER PRIMARY KEY, event_id INTEGER, person_id INTEGER, kind TEXT, start TEXT, end TEXT);
CREATE TABLE IF NOT EXISTS options(
  id INTEGER PRIMARY KEY, segment_id INTEGER, ord INTEGER, start TEXT, end TEXT, venue_id TEXT, venue_name TEXT,
  address TEXT, source_url TEXT, hours_status TEXT, checked_at TEXT, blocked_by TEXT, unknown_n INTEGER);
CREATE TABLE IF NOT EXISTS votes(
  segment_id INTEGER, option_key TEXT, person_id INTEGER, PRIMARY KEY(segment_id, option_key, person_id));
CREATE TABLE IF NOT EXISTS locks(
  segment_id INTEGER PRIMARY KEY, start TEXT, end TEXT, venue_id TEXT, venue_name TEXT, address TEXT,
  source_url TEXT, hours_status TEXT, checked_at TEXT, locked_at REAL);
CREATE TABLE IF NOT EXISTS tiebreak(segment_id INTEGER PRIMARY KEY, round INTEGER, keys TEXT);
CREATE TABLE IF NOT EXISTS hours_cache(
  url TEXT, weekday TEXT, open TEXT, close TEXT, checked_at TEXT, PRIMARY KEY(url, weekday));
"""
con: sqlite3.Connection


def init(path=None):
    global con
    # ponytail: one shared connection also used by the engine's worker thread; writes are tiny and idempotent.
    # Upgrade to a connection per thread if writes ever get large or concurrent.
    con = sqlite3.connect(path or os.environ.get("DB_PATH", "planbot.db"), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)


def _person(pid, name):
    """Upsert. A name another person already uses gets a short id suffix, so labels stay unique per person."""
    if con.execute("SELECT 1 FROM persons WHERE name=? AND id!=?", (name, pid)).fetchone():
        name = f"{name} ({str(pid)[-4:]})"
    con.execute("INSERT INTO persons(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name", (pid, name))


def create_event(channel_id, proposer_id, proposer_name, kinds) -> int:
    """Proposer is auto-in to every segment."""
    _person(proposer_id, proposer_name)
    eid = con.execute("INSERT INTO events(channel_id,proposer_id,created_at) VALUES(?,?,?)",
                      (channel_id, proposer_id, time.time())).lastrowid
    for i, k in enumerate(kinds):
        sid = con.execute("INSERT INTO segments(event_id,kind,ord) VALUES(?,?,?)", (eid, k, i)).lastrowid
        con.execute("INSERT INTO participation VALUES(?,?,'in')", (proposer_id, sid))
    con.commit()
    return eid


def set_card(event_id, message_id):
    con.execute("UPDATE events SET card_message_id=? WHERE id=?", (message_id, event_id))
    con.commit()


def open_event(channel_id):
    return con.execute("SELECT * FROM events WHERE channel_id=? AND status NOT IN ('done','cancelled') "
                       "ORDER BY id DESC LIMIT 1", (channel_id,)).fetchone()


def cancel(event_id):
    import polls
    polls.cancel_open(event_id=event_id)
    con.execute("UPDATE events SET status='cancelled' WHERE id=?", (event_id,))
    con.commit()


def set_segments(event_id, kinds, organizer_id):
    """Replace the activity list. Kept activities keep their participants; new ones start with the organizer in."""
    cur = {r["kind"]: r["id"] for r in con.execute("SELECT id, kind FROM segments WHERE event_id=?", (event_id,))}
    for kind, sid in cur.items():
        if kind not in kinds:
            import polls
            polls.cancel_open(segment_id=sid)
            for t in ("options", "votes", "locks", "tiebreak"):
                con.execute(f"DELETE FROM {t} WHERE segment_id=?", (sid,))
            con.execute("DELETE FROM participation WHERE segment_id=?", (sid,))
            con.execute("DELETE FROM segments WHERE id=?", (sid,))
    for i, kind in enumerate(kinds):
        if kind in cur:
            con.execute("UPDATE segments SET ord=? WHERE id=?", (i, cur[kind]))
        else:
            sid = con.execute("INSERT INTO segments(event_id,kind,ord) VALUES(?,?,?)", (event_id, kind, i)).lastrowid
            con.execute("INSERT INTO participation VALUES(?,?,'in')", (organizer_id, sid))
    con.commit()


def event_of_segment(segment_id):
    return con.execute("SELECT event_id FROM segments WHERE id=?", (segment_id,)).fetchone()["event_id"]


def toggle(segment_id, person_id, name) -> int:
    """in <-> out for this person and segment. Returns the event id."""
    _person(person_id, name)
    row = con.execute("SELECT status FROM participation WHERE person_id=? AND segment_id=?",
                      (person_id, segment_id)).fetchone()
    new = "out" if row and row["status"] == "in" else "in"
    con.execute("INSERT OR REPLACE INTO participation VALUES(?,?,?)", (person_id, segment_id, new))
    con.commit()
    return con.execute("SELECT event_id FROM segments WHERE id=?", (segment_id,)).fetchone()["event_id"]


def snapshot(event_id) -> dict:
    """Event, segments with who is in, and whether planning has started (proposer + one more)."""
    ev = con.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    segs = []
    for s in con.execute("SELECT * FROM segments WHERE event_id=? ORDER BY ord", (event_id,)):
        names = [r["name"] for r in con.execute(
            "SELECT p.name FROM participation q JOIN persons p ON p.id=q.person_id "
            "WHERE q.segment_id=? AND q.status='in' ORDER BY p.name", (s["id"],))]
        who = segment_participants(s["id"])
        appr = approvals(s["id"])
        opts = [{**o, "key": option_key(o), "approvals": len(appr.get(option_key(o), set()) & who)}
                for o in options(s["id"])]
        voters = set().union(*appr.values()) & who  # counts only, never who voted for what
        tb = con.execute("SELECT * FROM tiebreak WHERE segment_id=?", (s["id"],)).fetchone()
        segs.append({"id": s["id"], "kind": s["kind"], "in": names, "options": opts, "status": s["status"],
                     "locked": locked(s["id"]), "booked_by": _booked_by(s["id"]), "voted": len(voters), "total": len(who),
                     "tiebreak": tb and {"round": tb["round"], "keys": json.loads(tb["keys"])}})
    n_in = con.execute(
        "SELECT COUNT(DISTINCT q.person_id) FROM participation q JOIN segments s ON s.id=q.segment_id "
        "WHERE s.event_id=? AND q.status='in'", (event_id,)).fetchone()[0]
    planning = n_in >= 2
    if planning and ev["status"] == "collecting":
        con.execute("UPDATE events SET status='planning' WHERE id=?", (event_id,))
        con.commit()
    return {"id": event_id, "proposer_id": ev["proposer_id"], "status": ev["status"], "segments": segs, "planning": planning,
            "n_in": n_in, "constraints": constraints(event_id), "replied": replied(event_id)}


def replied(event_id) -> int:
    """People on the roster who have told us when they are free or busy."""
    return con.execute(
        "SELECT COUNT(DISTINCT a.person_id) FROM avail a WHERE a.event_id=? AND a.person_id IN "
        "(SELECT q.person_id FROM participation q JOIN segments s ON s.id=q.segment_id "
        "WHERE s.event_id=? AND q.status='in')", (event_id, event_id)).fetchone()[0]


def people_for_segment(segment_id) -> dict:
    """{name: {"free": [(start, end)], "busy": [...]}} for everyone in this segment, as datetimes."""
    from datetime import datetime
    event_id = event_of_segment(segment_id)
    out = {}
    for p in con.execute("SELECT p.id, p.name FROM participation q JOIN persons p ON p.id=q.person_id "
                         "WHERE q.segment_id=? AND q.status='in'", (segment_id,)):
        av = {"free": [], "busy": []}
        for r in con.execute("SELECT kind,start,end FROM avail WHERE event_id=? AND person_id=?", (event_id, p["id"])):
            av[r["kind"]].append((datetime.fromisoformat(r["start"]), datetime.fromisoformat(r["end"])))
        out[p["name"]] = av
    return out


def save_options(segment_id, opts: list):
    con.execute("DELETE FROM options WHERE segment_id=?", (segment_id,))
    keys = {option_key(o) for o in opts}
    for r in con.execute("SELECT DISTINCT option_key FROM votes WHERE segment_id=?", (segment_id,)).fetchall():
        if r[0] not in keys:  # votes for vanished options are dropped; reappearing options keep theirs
            con.execute("DELETE FROM votes WHERE segment_id=? AND option_key=?", (segment_id, r[0]))
    tb = con.execute("SELECT keys FROM tiebreak WHERE segment_id=?", (segment_id,)).fetchone()
    if tb and not set(json.loads(tb[0])) <= keys:
        con.execute("DELETE FROM tiebreak WHERE segment_id=?", (segment_id,))
    con.execute("UPDATE segments SET status=? WHERE id=? AND status!='locked'", ("provisional" if opts else "planning", segment_id))
    for i, o in enumerate(opts):
        con.execute("INSERT INTO options(segment_id,ord,start,end,venue_id,venue_name,address,source_url,hours_status,"
                    "checked_at,blocked_by,unknown_n) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (segment_id, i, o["start"], o["end"], o["venue_id"], o["venue_name"], o["address"], o["source_url"],
                     o["hours_status"], o["checked_at"], json.dumps(o["blocked_by"]), o["unknown_n"]))
    con.commit()


def options(segment_id) -> list:
    rows = con.execute("SELECT * FROM options WHERE segment_id=? ORDER BY ord", (segment_id,)).fetchall()
    return [{**dict(r), "blocked_by": json.loads(r["blocked_by"])} for r in rows]


def get_hours(url, weekday, newer_than):
    """Cached hours row, or None if absent or older than newer_than. open is NULL when the page stated none."""
    r = con.execute("SELECT * FROM hours_cache WHERE url=? AND weekday=?", (url, weekday)).fetchone()
    return r if r and r["checked_at"] >= newer_than.isoformat(timespec="minutes") else None


def put_hours(url, weekday, hours, checked_at):
    o, c = (hours[0].isoformat(timespec="minutes"), hours[1].isoformat(timespec="minutes")) if hours else (None, None)
    con.execute("INSERT OR REPLACE INTO hours_cache VALUES(?,?,?,?,?)",
                (url, weekday, o, c, checked_at.isoformat(timespec="minutes")))
    con.commit()


def event_for_person(person_id):
    """Open event this person is part of (used to route DMs)."""
    return con.execute(
        "SELECT DISTINCT e.* FROM events e JOIN segments s ON s.event_id=e.id "
        "JOIN participation q ON q.segment_id=s.id WHERE q.person_id=? AND e.status NOT IN ('done','cancelled') "
        "ORDER BY e.id DESC LIMIT 1", (person_id,)).fetchone()


def apply(event_id, person_id, name, p: dict):
    """Store a parsed message. Later availability replaces earlier; prefs only change if stated."""
    _person(person_id, name)
    old = con.execute("SELECT * FROM prefs WHERE person_id=?", (person_id,)).fetchone()
    diet = json.dumps(p["diet"]) if p["diet"] else (old["diet"] if old else None)
    diet_hard = int(p["diet_hard"]) if p["diet"] else (old["diet_hard"] if old else 0)
    budget = p["budget"] if p["budget"] is not None else (old["budget"] if old else None)
    area, area_hard = (p["area"], int(p["area_hard"])) if p["area"] else (
        (old["area"], old["area_hard"]) if old else (None, 0))
    con.execute("INSERT OR REPLACE INTO prefs VALUES(?,?,?,?,?,?,?)",
                (person_id, diet, diet_hard, budget, area, area_hard, time.time()))
    if p["free"] or p["busy"]:
        con.execute("DELETE FROM avail WHERE event_id=? AND person_id=?", (event_id, person_id))
        for kind in ("free", "busy"):
            for s, e in p[kind]:
                con.execute("INSERT INTO avail(event_id,person_id,kind,start,end) VALUES(?,?,?,?,?)",
                            (event_id, person_id, kind, s, e))
    for kind, status in p["segments"]:
        seg = con.execute("SELECT id FROM segments WHERE event_id=? AND kind=?", (event_id, kind)).fetchone()
        if seg:
            con.execute("INSERT OR REPLACE INTO participation VALUES(?,?,?)", (person_id, seg["id"], status))
    con.commit()


def constraints(event_id) -> dict:
    """Aggregate only: no names leave this function. Diet is the union, budget the lowest cap, area the latest."""
    rows = con.execute(
        "SELECT f.* FROM prefs f WHERE f.person_id IN (SELECT q.person_id FROM participation q "
        "JOIN segments s ON s.id=q.segment_id WHERE s.event_id=? AND q.status='in') ORDER BY f.updated_at",
        (event_id,)).fetchall()
    diet = sorted({d for r in rows if r["diet"] for d in json.loads(r["diet"])})
    budgets = [r["budget"] for r in rows if r["budget"]]
    areas = [(r["area"], bool(r["area_hard"])) for r in rows if r["area"]]
    return {"diet": diet, "diet_hard": any(r["diet"] and r["diet_hard"] for r in rows),
            "budget": min(budgets) if budgets else None, "area": areas[-1] if areas else None}


# ---- voting and locking -------------------------------------------------------------------------------------

LOCK_FIELDS = ("start", "end", "venue_id", "venue_name", "address", "source_url", "hours_status", "checked_at")


def option_key(o) -> str:
    """Options are recreated on every recompute, so votes follow what the option is, not its row id."""
    return f"{o['start']}|{o['venue_id']}"


def key_hash(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:6]


def segment_participants(segment_id) -> set:
    return {r[0] for r in con.execute("SELECT person_id FROM participation WHERE segment_id=? AND status='in'",
                                      (segment_id,))}


def vote_toggle(segment_id, option_key, person_id) -> bool:
    """Approve or un-approve. Returns True if the person now approves it."""
    args = (segment_id, option_key, person_id)
    on = con.execute("SELECT 1 FROM votes WHERE segment_id=? AND option_key=? AND person_id=?", args).fetchone()
    if on:
        con.execute("DELETE FROM votes WHERE segment_id=? AND option_key=? AND person_id=?", args)
    else:
        con.execute("INSERT INTO votes VALUES(?,?,?)", args)
    con.commit()
    return not on


def approvals(segment_id) -> dict:
    out = {}
    for r in con.execute("SELECT option_key, person_id FROM votes WHERE segment_id=?", (segment_id,)):
        out.setdefault(r["option_key"], set()).add(r["person_id"])
    return out


def locked(segment_id):
    r = con.execute("SELECT * FROM locks WHERE segment_id=?", (segment_id,)).fetchone()
    return {k: r[k] for k in LOCK_FIELDS} if r else None


def lock_segment(segment_id, option_dict):
    """Store a copy of the chosen option, so later recomputes cannot change what was agreed."""
    con.execute("INSERT OR REPLACE INTO locks VALUES(?,?,?,?,?,?,?,?,?,?)",
                (segment_id, *(option_dict[k] for k in LOCK_FIELDS), time.time()))
    con.execute("DELETE FROM tiebreak WHERE segment_id=?", (segment_id,))
    con.execute("UPDATE segments SET status='locked' WHERE id=?", (segment_id,))
    con.commit()


def _booked_by(segment_id):
    import booking  # lazy: booking imports db
    return booking.booked_by(segment_id)


def unlock_segment(segment_id):
    """Reopen this segment and every later one of the event: they were scheduled around it."""
    import booking  # lazy: booking imports db
    s = con.execute("SELECT event_id, ord FROM segments WHERE id=?", (segment_id,)).fetchone()
    for r in con.execute("SELECT id FROM segments WHERE event_id=? AND ord>=?", (s["event_id"], s["ord"])).fetchall():
        for t in ("votes", "locks", "tiebreak"):
            con.execute(f"DELETE FROM {t} WHERE segment_id=?", (r["id"],))
        con.execute("UPDATE segments SET status='planning' WHERE id=?", (r["id"],))
        booking.reset(r["id"])  # a reopened activity must not keep a stale booking
    con.commit()


def settle(segment_id) -> str:
    """Lock the segment if everyone voted and one option won. Returns locked | open | revote | proposer.
    Tie: re-vote among the tied options once, then the proposer picks (pick_tie)."""
    import vote
    seg = con.execute("SELECT status FROM segments WHERE id=?", (segment_id,)).fetchone()
    if seg is None or seg["status"] == "locked":
        return "locked"
    tb = con.execute("SELECT * FROM tiebreak WHERE segment_id=?", (segment_id,)).fetchone()
    if tb and tb["round"] == 2:
        return "proposer"
    who = segment_participants(segment_id)
    opts = {option_key(o): o for o in options(segment_id)}
    allowed = set(json.loads(tb["keys"])) if tb else set(opts)
    appr = {k: p & who for k, p in approvals(segment_id).items() if k in allowed and k in opts}
    if not vote.all_voted(who, appr):
        return "open"
    winners, _ = vote.tally(appr)
    if len(winners) == 1:
        lock_segment(segment_id, opts[winners[0]])
        return "locked"
    nxt = 2 if tb else 1
    con.execute("DELETE FROM votes WHERE segment_id=?", (segment_id,))
    con.execute("INSERT OR REPLACE INTO tiebreak VALUES(?,?,?)", (segment_id, nxt, json.dumps(sorted(winners))))
    con.commit()
    return "proposer" if nxt == 2 else "revote"


def pick_tie(segment_id, key) -> bool:
    """Proposer's choice in round 2. Only a tied option is accepted."""
    tb = con.execute("SELECT * FROM tiebreak WHERE segment_id=?", (segment_id,)).fetchone()
    opts = {option_key(o): o for o in options(segment_id)}
    if not tb or tb["round"] != 2 or key not in json.loads(tb["keys"]) or key not in opts:
        return False
    lock_segment(segment_id, opts[key])
    return True


def settle_event(event_id) -> list:
    """Settle every segment; returns ids that just locked."""
    return [r["id"] for r in con.execute("SELECT id FROM segments WHERE event_id=? AND status!='locked' ORDER BY ord",
                                         (event_id,)).fetchall() if settle(r["id"]) == "locked"]
