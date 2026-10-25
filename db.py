"""SQLite plan state. One connection, tiny queries, so plain sync calls are fine in the event loop."""
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
"""
con: sqlite3.Connection


def init(path=None):
    global con
    con = sqlite3.connect(path or os.environ.get("DB_PATH", "planbot.db"))
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)


def _person(pid, name):
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
    return con.execute("SELECT * FROM events WHERE channel_id=? AND status!='done' ORDER BY id DESC LIMIT 1",
                       (channel_id,)).fetchone()


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
        segs.append({"id": s["id"], "kind": s["kind"], "in": names})
    n_in = con.execute(
        "SELECT COUNT(DISTINCT q.person_id) FROM participation q JOIN segments s ON s.id=q.segment_id "
        "WHERE s.event_id=? AND q.status='in'", (event_id,)).fetchone()[0]
    planning = n_in >= 2
    if planning and ev["status"] == "collecting":
        con.execute("UPDATE events SET status='planning' WHERE id=?", (event_id,))
        con.commit()
    return {"id": event_id, "proposer_id": ev["proposer_id"], "segments": segs, "planning": planning, "n_in": n_in}
