import json

import pytest

import booking
import db

T0 = 1000.0


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    db.init(":memory:")
    monkeypatch.delenv("BOOK_TTL_SECONDS", raising=False)


def locked_event(kind="dinner"):
    """Ann (1), Bo (2), Cy (3) in one locked segment; returns (event_id, segment_id)."""
    eid = db.create_event(5, 1, "Ann", [kind])
    sid = db.con.execute("SELECT id FROM segments WHERE event_id=?", (eid,)).fetchone()[0]
    for k, n in ((2, "Bo"), (3, "Cy")):
        db._person(k, n)
        db.con.execute("INSERT INTO participation VALUES(?,?,'in')", (k, sid))
    db.con.commit()
    db.lock_segment(sid, {"start": "2026-10-25T18:30", "end": "2026-10-25T20:30", "venue_id": "v1",
                          "venue_name": "Luigi's", "address": "1 Main St", "source_url": "https://luigis.example",
                          "hours_status": "verified", "checked_at": None})
    return eid, sid


def test_first_volunteer_wins_second_rejected():
    _, sid = locked_event()
    booking.open_poll(sid, T0)
    assert booking.volunteer(sid, 2, T0) == (True, "Bo")
    assert booking.volunteer(sid, 3, T0) == (False, "Bo")
    assert booking.status(sid) == "volunteered"


def test_non_participant_cannot_volunteer():
    _, sid = locked_event()
    booking.open_poll(sid, T0)
    db._person(9, "Zed")
    assert booking.volunteer(sid, 9, T0)[0] is False and booking.status(sid) == "open"


def test_open_poll_once():
    _, sid = locked_event()
    assert booking.open_poll(sid, T0) == T0 + 3600
    assert booking.open_poll(sid, T0) is None


def test_hand_back_reopens_and_only_volunteer():
    _, sid = locked_event()
    booking.open_poll(sid, T0)
    booking.volunteer(sid, 2, T0)
    assert not booking.hand_back(sid, 3, T0)
    assert booking.hand_back(sid, 2, T0)
    assert booking.status(sid) == "open"
    assert booking.volunteer(sid, 3, T0) == (True, "Cy")


def test_booked_by_after_done():
    _, sid = locked_event()
    booking.open_poll(sid, T0)
    assert booking.booked_by(sid) is None
    booking.volunteer(sid, 2, T0)
    assert not booking.mark_booked(sid, 3)
    assert booking.mark_booked(sid, 2)
    assert booking.booked_by(sid) == "Bo" and booking.status(sid) == "booked"
    assert not booking.hand_back(sid, 2, T0)


def test_deadline_reminder_due_once(monkeypatch):
    monkeypatch.setenv("BOOK_TTL_SECONDS", "100")
    _, sid = locked_event()
    booking.open_poll(sid, T0)
    assert booking.due(T0 + 99) == []
    assert booking.due(T0 + 100) == [sid]
    assert booking.due(T0 + 5000) == []
    assert booking.status(sid) == "open"  # still volunteerable after the reminder


def test_no_reminder_when_volunteered():
    _, sid = locked_event()
    booking.open_poll(sid, T0)
    booking.volunteer(sid, 2, T0)
    assert booking.due(T0 + 99999) == []


def test_hand_back_rearms_reminder():
    _, sid = locked_event()
    booking.open_poll(sid, T0)
    booking.volunteer(sid, 2, T0)
    booking.hand_back(sid, 2, T0 + 10)
    assert booking.due(T0 + 3609) == [] and booking.due(T0 + 3610) == [sid]


def test_reset_after_unlock():
    _, sid = locked_event()
    booking.open_poll(sid, T0)
    booking.volunteer(sid, 2, T0)
    booking.mark_booked(sid, 2)
    db.unlock_segment(sid)
    booking.reset(sid)
    assert booking.status(sid) == "none" and booking.booked_by(sid) is None
    assert booking.volunteer(sid, 2, T0) == (False, None)  # buttons dead while unlocked
    assert not booking.mark_booked(sid, 2)


def test_stale_state_dead_even_without_reset():
    _, sid = locked_event()
    booking.open_poll(sid, T0)
    db.unlock_segment(sid)
    assert booking.status(sid) == "none" and booking.due(T0 + 99999) == []
    assert booking.volunteer(sid, 2, T0) == (False, None)


def test_cancelled_event_ignored():
    eid, sid = locked_event()
    db.cancel(eid)
    assert booking.open_poll(sid, T0) is None and booking.status(sid) == "none"
    eid, sid = locked_event()
    booking.open_poll(sid, T0)
    booking.volunteer(sid, 2, T0)
    db.cancel(eid)
    assert not booking.mark_booked(sid, 2) and booking.due(T0 + 99999) == []


def test_unlocked_segment_gets_no_poll():
    eid = db.create_event(5, 1, "Ann", ["golf"])
    sid = db.con.execute("SELECT id FROM segments WHERE event_id=?", (eid,)).fetchone()[0]
    assert booking.open_poll(sid, T0) is None


def test_dm_text_aggregated_diet_no_names():
    eid, sid = locked_event()
    for pid, diet in ((2, ["vegan"]), (3, ["nut allergy", "vegan"])):
        db.con.execute("INSERT INTO prefs(person_id,diet,diet_hard) VALUES(?,?,1)", (pid, json.dumps(diet)))
    db.con.commit()
    t = booking.dm_text(sid)
    assert "nut allergy, vegan" in t and "Party size: 3" in t
    assert not any(n in t for n in ("Ann", "Bo", "Cy"))


def test_dm_text_link_address_and_no_booking_line():
    _, sid = locked_event()
    t = booking.dm_text(sid)
    assert "https://luigis.example" in t and "Luigi's" in t and "1 Main St" in t and "Sun 6:30pm" in t
    assert "will not book or pay" in t


def test_dm_text_no_diet_for_non_meal():
    _, sid = locked_event("golf")
    db.con.execute("INSERT INTO prefs(person_id,diet,diet_hard) VALUES(2,?,1)", (json.dumps(["vegan"]),))
    db.con.commit()
    assert "vegan" not in booking.dm_text(sid)


def test_relock_without_reset_starts_fresh():
    _, sid = locked_event()
    booking.open_poll(sid, T0)
    booking.volunteer(sid, 2, T0)
    db.unlock_segment(sid)
    import time
    time.sleep(0.01)
    db.lock_segment(sid, {"start": "2026-10-26T18:30", "end": "2026-10-26T20:30", "venue_id": "v1", "venue_name": "X",
                          "address": None, "source_url": None, "hours_status": "verified", "checked_at": None})
    assert booking.status(sid) == "none"
    assert booking.open_poll(sid, T0) is not None


def test_booked_by_shows_on_card_and_unlock_clears_it():
    import plan
    eid = db.create_event(1, 10, "Ann", ["golf"])
    sid = db.snapshot(eid)["segments"][0]["id"]
    opt = {k: None for k in db.LOCK_FIELDS} | {"start": "2026-10-25T10:00", "end": "2026-10-25T14:00",
                                               "venue_name": "Ken McDonald", "hours_status": "unverified"}
    db.lock_segment(sid, opt)
    booking.open_poll(sid)
    assert booking.volunteer(sid, 10)[0]
    booking.mark_booked(sid, 10)
    snap = db.snapshot(eid)
    assert snap["segments"][0]["booked_by"] == "Ann"
    assert "Booked by Ann" in plan.render(snap).to_dict()["fields"][0]["value"]
    db.unlock_segment(sid)
    assert db.snapshot(eid)["segments"][0]["booked_by"] is None
