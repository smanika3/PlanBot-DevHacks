import asyncio
from datetime import datetime

import pytest

import db
import polls

NOW = datetime(2026, 10, 24, 12, 0)  # Saturday noon
T0 = 1000.0
SUN = datetime(2026, 10, 25)
WHY = ("sick", "doctor", "work", "because", "why", "date", "meeting")


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    db.init(":memory:")
    calls = {"unlock": [], "recompute": []}
    monkeypatch.setattr(db, "unlock_segment", lambda s: calls["unlock"].append(s), raising=False)
    monkeypatch.setattr(db, "segment_participants", lambda s: polls._in(s), raising=False)
    return calls


def event(names=("Ann", "Bo", "Cy", "Di"), kinds=("golf", "dinner")):
    """Ann (id 1) proposes and is in everything; others are in every segment."""
    eid = db.create_event(5, 1, names[0], list(kinds))
    segs = [r["id"] for r in db.con.execute("SELECT id FROM segments WHERE event_id=? ORDER BY ord", (eid,))]
    for k, n in enumerate(names[1:], 2):
        db._person(k, n)
        for s in segs:
            db.con.execute("INSERT INTO participation VALUES(?,?,'in')", (k, s))
    db.con.commit()
    return eid, segs


def option(start, blocked=(), h=11):
    s = SUN.replace(hour=h)
    return {"start": s.isoformat(timespec="minutes"), "end": s.replace(hour=h + 4).isoformat(timespec="minutes"),
            "venue_id": None, "venue_name": None, "address": None, "source_url": None, "hours_status": "unverified",
            "checked_at": None, "blocked_by": list(blocked), "unknown_n": 0}


def status(person, seg):
    return db.con.execute("SELECT status FROM participation WHERE person_id=? AND segment_id=?", (person, seg)).fetchone()[0]


def poll(eid, seg, electorate=(2, 3, 4), kind="conflict", choices=("without", "everyone")):
    return polls.open_poll(kind, eid, seg, choices, electorate, 1, 2, now=T0, ttl=100)


# ---- core -----------------------------------------------------------------------------------------------------

def test_majority_passes_immediately():
    eid, (g, _) = event()
    p = poll(eid, g)
    polls.cast(p, 2, "without", T0)
    assert polls.decide(p, 1, T0) == ("pending", None)
    polls.cast(p, 3, "without", T0)  # 2 of 3 is more than half
    assert polls.decide(p, 1, T0) == ("decided", "without")


def test_last_vote_replaces():
    eid, (g, _) = event()
    p = poll(eid, g)
    polls.cast(p, 2, "without", T0)
    polls.cast(p, 2, "everyone", T0)
    assert polls.get(p)["votes"] == {2: "everyone"}


def test_tie_goes_to_proposer_vote():
    eid, (g, _) = event()
    p = poll(eid, g, electorate=(1, 2))
    polls.cast(p, 1, "everyone", T0)
    polls.cast(p, 2, "without", T0)
    assert polls.decide(p, 1, T0) == ("decided", "everyone")


def test_needs_tiebreak_then_proposer_breaks():
    eid, (g, _) = event()
    p = poll(eid, g, electorate=(2, 3))
    polls.cast(p, 2, "without", T0)
    polls.cast(p, 3, "everyone", T0)
    assert polls.decide(p, 1, T0) == ("needs_tiebreak", None)
    assert not polls.proposer_break(p, 2, "without")  # only the organizer
    assert polls.proposer_break(p, 1, "without")
    assert polls.resolve(p, T0) == ("decided", "without")
    assert polls.get(p)["status"] == "closed"


def test_deadline_via_due():
    eid, (g, _) = event()
    p = poll(eid, g)
    polls.cast(p, 2, "without", T0)
    assert polls.due(T0 + 50) == []
    assert polls.due(T0 + 100) == [p]
    assert polls.resolve(p, T0 + 100) == ("decided", "without")  # most votes wins at the deadline
    assert polls.due(T0 + 100) == []
    assert not polls.cast(p, 3, "everyone", T0 + 101)  # closed


def test_deadline_defaults():
    eid, (g, d) = event()
    c, r = poll(eid, g), poll(eid, d, kind="reschedule", choices=("yes", "no"))
    assert polls.decide(c, 1, T0 + 100) == ("decided", "everyone")
    assert polls.decide(r, 1, T0 + 100) == ("decided", "no")  # a reschedule fails if nobody backs it


def test_unbroken_tie_at_deadline_falls_back():
    eid, (g, _) = event()
    p = poll(eid, g, electorate=(2, 3))
    polls.cast(p, 2, "without", T0)
    polls.cast(p, 3, "everyone", T0)
    assert polls.decide(p, 1, T0 + 100) == ("decided", "everyone")


def test_non_electorate_and_bad_choice_rejected():
    eid, (g, _) = event()
    p = poll(eid, g)
    assert not polls.cast(p, 1, "without", T0)  # proposer is not an elector here
    assert not polls.cast(p, 2, "maybe", T0)
    assert not polls.cast(p, 2, "without", T0 + 100)  # past deadline
    assert polls.get(p)["votes"] == {}


def test_one_open_poll_per_segment():
    eid, (g, d) = event()
    assert poll(eid, g)
    assert poll(eid, g, kind="reschedule", choices=("yes", "no")) is None
    assert poll(eid, d)  # other segment is independent


def test_tables_follow_new_connection():
    eid, (g, _) = event()
    poll(eid, g)
    db.init(":memory:")
    eid, (g, _) = event()
    assert poll(eid, g)


# ---- conflict flow --------------------------------------------------------------------------------------------

def test_conflict_marks_tentative_out_and_opens_one_poll():
    eid, (g, d) = event()
    db.save_options(g, [option(0, blocked=["Bo"])])
    ids = polls.detect_conflicts(eid, T0)
    assert len(ids) == 1 and status(2, g) == "tentative_out"
    p = polls.get(ids[0])
    assert p["subject_id"] == 2 and p["electorate"] == [1, 3, 4] and p["choices"] == ["without", "everyone"]
    assert polls.detect_conflicts(eid, T0) == []  # still one


def test_no_second_poll_for_same_person_even_after_resolution():
    eid, (g, _) = event()
    db.save_options(g, [option(0, blocked=["Bo"])])
    (p,) = polls.detect_conflicts(eid, T0)
    polls.close(p, "everyone")
    polls.set_status(2, g, "in")
    assert polls.detect_conflicts(eid, T0) == []


def test_maybe_and_trailing_options_do_not_trigger():
    eid, (g, d) = event()
    polls.set_status(2, g, "maybe")
    db.save_options(g, [option(0, blocked=["Bo"]), option(1, blocked=["Cy"], h=14)])  # only option 0 counts
    assert polls.detect_conflicts(eid, T0) == []
    assert status(2, g) == "maybe"


def test_locked_segment_and_empty_electorate_skipped():
    eid, (g, d) = event(names=("Ann", "Bo"))
    db.save_options(g, [option(0, blocked=["Bo"])])
    db.con.execute("UPDATE segments SET status='locked' WHERE id=?", (g,))
    assert polls.detect_conflicts(eid, T0) == []
    db.con.execute("UPDATE segments SET status='planning' WHERE id=?", (g,))
    polls.set_status(1, g, "out")  # nobody left to vote
    assert polls.detect_conflicts(eid, T0) == []
    assert status(2, g) == "in"


def test_guardrail_leaves_second_person_in():
    eid, (g, _) = event()
    db.save_options(g, [option(0, blocked=["Bo", "Cy"])])
    assert len(polls.detect_conflicts(eid, T0)) == 1
    assert status(2, g) == "tentative_out" and status(3, g) == "in"


def test_without_sets_out_and_recomputes():
    eid, (g, _) = event()
    db.save_options(g, [option(0, blocked=["Bo"])])
    (p,) = polls.detect_conflicts(eid, T0)
    rec = []
    line, more = polls.finish(p, "without", rec.append, NOW)
    assert status(2, g) == "out" and more is None and rec == [eid] and "Bo" in line


def free(person, seg_event, day, h1, h2):
    db.con.execute("INSERT INTO avail(event_id,person_id,kind,start,end) VALUES(?,?,?,?,?)",
                   (seg_event, person, "free", day.replace(hour=h1).isoformat(), day.replace(hour=h2).isoformat()))


def test_everyone_gives_alternatives_with_counts_and_knock_on():
    eid, (g, d) = event()
    # three can golf Sunday 7-13; Bo only from 11 on, so 11am is the one slot where all four are free
    for p in (1, 3, 4):
        free(p, eid, SUN, 7, 21)
    free(2, eid, SUN, 11, 21)
    db.con.commit()
    db.save_options(g, [option(0, blocked=["Bo"], h=7)])
    db.save_options(d, [option(0, h=12)])  # dinner currently sits at noon, so any shift is a visible move
    (p,) = polls.detect_conflicts(eid, T0)
    rec = []
    line, more = polls.finish(p, "everyone", rec.append, NOW)
    assert status(2, g) == "in" and rec == [eid]
    assert "Sun 11am: 4 of 4 free" in more and "dinner would move to Sun 5pm" in more
    assert not any(w in (line + more).lower() for w in WHY)


def test_alternatives_ask_blocked_people_by_name():
    eid, (g, d) = event()
    free(1, eid, SUN, 7, 11)
    free(2, eid, SUN, 12, 16)
    free(3, eid, SUN, 7, 11)
    free(4, eid, SUN, 7, 11)
    db.con.commit()
    text = polls.alternatives(g, NOW)
    assert "3 of 4 free" in text and "Bo" in text and "share more times" in text


def test_i_can_make_it_restores():
    eid, (g, _) = event()
    db.save_options(g, [option(0, blocked=["Bo"])])
    (p,) = polls.detect_conflicts(eid, T0)
    rec = []
    assert not polls.restore(p, 3, rec.append)  # only Bo
    assert polls.restore(p, 2, rec.append)
    assert status(2, g) == "in" and polls.get(p)["result"] == "cancelled" and rec == [eid]
    assert not polls.restore(p, 2, rec.append)  # already closed


def test_render_shows_counts_not_votes_or_reasons():
    eid, (g, _) = event()
    db.save_options(g, [option(0, blocked=["Bo"])])
    (p,) = polls.detect_conflicts(eid, T0)
    polls.cast(p, 3, "without", T0)
    text = polls.render(p)
    assert "1 of 3 voted" in text and "Bo" in text and "Cy" not in text and "Sun 11am" in text
    assert not any(w in text.lower() for w in WHY)


# ---- reschedule flow ------------------------------------------------------------------------------------------

def test_reschedule_pass_unlocks_and_recomputes(fresh):
    eid, (g, d) = event()
    db.save_options(g, [option(0, blocked=["Cy"])])
    p = polls.request_reschedule(g, 3, T0)
    assert polls.get(p)["electorate"] == [1, 2, 3, 4]  # includes whoever would miss out
    polls.cast(p, 1, "yes", T0)
    polls.cast(p, 2, "yes", T0)
    assert polls.resolve(p, T0)[0] == "pending"
    polls.cast(p, 4, "yes", T0)
    state, winner = polls.resolve(p, T0)
    rec = []
    line, _ = polls.finish(p, winner, rec.append, NOW)
    assert (state, winner) == ("decided", "yes") and fresh["unlock"] == [g] and rec == [eid]
    assert "dinner" in line and "Cy" in line
    assert not any(w in line.lower() for w in WHY)


def test_reschedule_fail_and_guardrail(fresh):
    eid, (g, _) = event()
    p = polls.request_reschedule(g, 2, T0)
    assert polls.request_reschedule(g, 3, T0) is None  # one vote at a time
    polls.cast(p, 1, "no", T0)
    polls.cast(p, 2, "yes", T0)
    assert polls.resolve(p, T0 + 3600) == ("decided", "no")  # 1-1 at the deadline, organizer voted no
    line, _ = polls.finish(p, "no", lambda e: None, NOW)
    assert fresh["unlock"] == [] and "did not pass" in line


def test_reschedule_only_for_participants_and_pick_segment():
    eid, (g, d) = event()
    polls.set_status(2, g, "out")
    assert polls.request_reschedule(g, 2, T0) is None
    assert polls.pick_segment(eid, 2, None) == (d, None)  # only one left
    assert polls.pick_segment(eid, 3, None)[0] is None  # two: asks which
    assert polls.pick_segment(eid, 3, "Golf") == (g, None)
    assert polls.pick_segment(eid, 3, "bowling")[0] is None
    assert polls.pick_segment(eid, 99, None)[0] is None


def test_sweep_closes_due_poll(fresh):
    import ui_polls

    class Chan:
        async def send(self, *a, **k):
            self.sent = a

    class Client:
        recomputed = []
        ch = Chan()

        def get_channel(self, _):
            return self.ch

        def schedule_recompute(self, e):
            self.recomputed.append(e)

    eid, (g, _) = event()
    p = polls.request_reschedule(g, 2)  # real clock
    polls.cast(p, 1, "yes")
    db.con.execute("UPDATE polls SET deadline=1 WHERE id=?", (p,))
    c = Client()
    asyncio.run(ui_polls.sweep(c))
    assert polls.get(p)["status"] == "closed" and polls.get(p)["result"] == "yes"
    assert fresh["unlock"] == [g] and c.recomputed == [eid] and "Reschedule passed" in c.ch.sent[0]
