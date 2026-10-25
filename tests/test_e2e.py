"""Offline end-to-end scenario (acceptance script) through the real db/engine/schedule/polls/voting code.
No LLM, no network, no Discord: hours_fn is fake, parse_listen gets canned JSON. Tests marked xfail(strict) document
confirmed bugs found by the M4 review; remove the marker when the bug is fixed."""
import asyncio
import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest

import bot
import db
import engine
import plan
import polls
import schedule
import ui_polls
import venue

NOW = datetime(2026, 10, 24, 12, 0)  # Saturday noon
SUN = "2026-10-25"
V = venue.load()
ANN, BO, CY, DI = 10, 11, 12, 13


def fake_hours(v, start, end, now):
    """Papago and Picazzo's 'confirmed', everything else unverified (as when a page cannot be read)."""
    if v["id"] in ("papago", "picazzos-tempe"):
        return {"status": "open", "checked_at": now.isoformat(timespec="minutes"), "source": v["hours_url"]}
    return {"status": "unverified", "checked_at": None, "source": v.get("website")}


def say(person, name, eid, **fields):
    """Feed a canned model reply through the real parser and store it."""
    base = {"free": [], "busy": [], "diet": [], "diet_hard": False, "budget": None, "area": None,
            "area_hard": False, "segments": []}
    p = plan.parse_listen(json.dumps({**base, **fields}))
    db.apply(eid, person, name, p)
    db.settle_event(eid)
    return plan.echo(p)


def win(a, b, day=SUN):
    return {"start": f"{day}T{a}", "end": f"{day}T{b}"}


def card_text(eid):
    e = plan.render(db.snapshot(eid))
    return "\n".join([e.title or "", e.description or "", *(f"{f.name}\n{f.value}" for f in e.fields),
                      e.footer.text or ""])


def segs(eid):
    return [s["id"] for s in db.snapshot(eid)["segments"]]


def recompute(eid):
    return engine.run(eid, NOW, V, fake_hours)


@pytest.fixture(autouse=True)
def fresh():
    db.init(":memory:")


def test_acceptance_script():
    public = []  # everything the group channel would ever see

    # 1. kickoff with golf + dinner, proposer auto-in, planning not started
    eid = db.create_event(1, ANN, "Ann", ["golf", "dinner"])
    golf, dinner = segs(eid)
    assert not db.snapshot(eid)["planning"]
    assert "Planning starts once one more person is in" in card_text(eid)

    # 2. second person in: planning starts, "0 of 2 shared times"
    db.toggle(golf, BO, "Bo"); db.toggle(dinner, BO, "Bo")
    assert db.snapshot(eid)["planning"] and "0 of 2 shared times" in card_text(eid)

    # 3. intake: availability, diet (private), area (public, preferred)
    pub, priv = say(ANN, "Ann", eid, free=[win("07:00", "12:00"), win("17:00", "22:00")])
    public.append(pub)
    pub, priv = say(BO, "Bo", eid, free=[win("07:00", "12:00"), win("17:00", "22:00")], diet=["vegetarian"],
                    diet_hard=True, area="Tempe", area_hard=False)
    public.append(pub)
    assert "vegetarian" not in pub and "diet" not in pub and "vegetarian" in priv  # diet only in the private echo
    assert "area Tempe (preferably)" in pub
    card = card_text(eid)
    assert "Dietary: vegetarian" in card and "Area: Tempe (preferred)" in card
    assert "Bo" in card  # participant names are fine; but diet is never tied to a name
    assert not any("vegetarian" in line and "Bo" in line for line in card.splitlines())

    # 4. Cy: out of golf, in for dinner (per-segment participation)
    db.toggle(dinner, CY, "Cy")
    pub, _ = say(CY, "Cy", eid, free=[win("16:00", "23:00")], segments=[{"kind": "golf", "status": "out"},
                                                                          {"kind": "dinner", "status": "in"}])
    public.append(pub)
    assert "out for golf" in pub and "in for dinner" in pub
    s = {x["kind"]: x for x in db.snapshot(eid)["segments"]}
    assert s["golf"]["in"] == ["Ann", "Bo"] and s["dinner"]["in"] == ["Ann", "Bo", "Cy"]

    # 5. provisional options with source + checked-at / unverified
    status = recompute(eid)
    assert set(status) == {"Preferences", "Schedule", "Venue"}
    s = {x["kind"]: x for x in db.snapshot(eid)["segments"]}
    assert 1 <= len(s["golf"]["options"]) <= 3 and 1 <= len(s["dinner"]["options"]) <= 3
    card = card_text(eid)
    assert "Provisional, not locked" in card and "([source](" in card
    assert "hours confirmed" in card
    assert all(o["venue_id"] in ("picazzos-tempe",) for o in s["dinner"]["options"])  # hard vegetarian filter
    # golf: first two venues, one confirmed one unverified
    assert {o["hours_status"] for o in s["golf"]["options"]} <= {"verified", "unverified"}
    assert "unverified" in card or all(o["hours_status"] == "verified" for o in s["golf"]["options"])
    assert "of 3 replied" in card or "replied" in card

    # 6. Di joins both, can only golf late on Sunday -> blocked at the leading golf slot -> tentative_out + vote
    db.toggle(golf, DI, "Di"); db.toggle(dinner, DI, "Di")
    say(DI, "Di", eid, free=[win("13:00", "23:00")])
    recompute(eid)
    lead = db.options(golf)[0]
    assert lead["blocked_by"] == ["Di"]
    pids = polls.detect_conflicts(eid, now=1000.0)
    assert len(pids) == 1
    pid = pids[0]
    assert db.con.execute("SELECT status FROM participation WHERE person_id=? AND segment_id=?", (DI, golf)).fetchone()[0] == "tentative_out"
    assert db.con.execute("SELECT status FROM participation WHERE person_id=? AND segment_id=?", (DI, dinner)).fetchone()[0] == "in"
    assert polls.detect_conflicts(eid, now=1000.0) == []  # no second poll for the same person/segment
    p = polls.get(pid)
    assert p["electorate"] == [ANN, BO] and p["choices"] == ["without", "everyone"]
    txt = polls.render(pid)
    public.append(txt)
    assert "Di" in txt and "may not make" in txt
    assert polls.cast(pid, BO, "everyone", 1001.0)
    assert not polls.cast(pid, DI, "without", 1001.0)   # the subject cannot vote
    assert not polls.cast(pid, CY, "without", 1001.0)   # not in golf
    public.append(polls.render(pid))
    assert polls.resolve(pid, 1001.0) == ("pending", None)
    assert polls.cast(pid, ANN, "everyone", 1002.0)
    state, winner = polls.resolve(pid, 1002.0)
    assert (state, winner) == ("decided", "everyone")
    line, alt = polls.finish(pid, winner, recompute, NOW)  # Di is back in; alternatives follow
    public += [line, alt]
    assert "finding a time for everyone" in line
    assert alt.startswith("Best times for golf") and "of 3 free" in alt
    assert "dinner would move" in alt or "dinner has no slot" in alt  # knock-on effect on dinner
    s = {x["kind"]: x for x in db.snapshot(eid)["segments"]}
    assert s["golf"]["in"] == ["Ann", "Bo", "Di"]

    # 7. votes lock golf, then dinner (approval voting; one option approved by all)
    g_opts, d_opts = db.options(golf), db.options(dinner)
    gk, dk = db.option_key(g_opts[0]), db.option_key(d_opts[0])
    db.vote_toggle(golf, gk, ANN)
    assert db.settle(golf) == "open"
    db.vote_toggle(golf, gk, BO)
    assert db.settle(golf) == "open"  # Di is back in golf and has not voted
    db.vote_toggle(golf, gk, DI)
    assert db.settle(golf) == "locked"
    snap = {x["kind"]: x for x in db.snapshot(eid)["segments"]}
    assert snap["golf"]["locked"]["start"] == g_opts[0]["start"] and snap["dinner"]["locked"] is None
    locked_golf = db.locked(golf)

    # locked segments are not recomputed, even when availability changes; dinner now follows the lock
    before = db.options(golf)
    say(ANN, "Ann", eid, free=[win("07:00", "12:00"), win("17:00", "22:00")])
    recompute(eid)
    assert db.options(golf) == before and db.locked(golf) == locked_golf
    d_start = datetime.fromisoformat(db.options(dinner)[0]["start"])
    assert d_start >= datetime.fromisoformat(locked_golf["end"]) + schedule.TRAVEL
    dk = db.option_key(db.options(dinner)[0])
    for who in (ANN, BO, CY):
        db.vote_toggle(dinner, dk, who)
    assert db.settle(dinner) == "open"  # Di (in dinner) has not voted
    db.vote_toggle(dinner, dk, DI)
    assert db.settle(dinner) == "locked"
    assert all(x["locked"] for x in db.snapshot(eid)["segments"])
    assert "🔒 **Locked:**" in card_text(eid)
    assert "Provisional" not in card_text(eid).split("2. Dinner")[1].split("Group needs")[0]

    # 8. reschedule poll passes by majority and reopens golf and the later dinner
    rid = polls.request_reschedule(golf, ANN, now=2000.0)
    assert rid and polls.request_reschedule(golf, BO, now=2000.0) is None  # one open vote per segment
    head = polls.render(rid)
    assert "dinner reopens too" in head
    assert polls.cast(rid, ANN, "yes", 2001.0) and polls.resolve(rid, 2001.0) == ("pending", None)
    assert polls.cast(rid, BO, "yes", 2002.0)
    state, winner = polls.resolve(rid, 2002.0)  # 2 of 3 is a majority
    assert (state, winner) == ("decided", "yes")
    line, _ = polls.finish(rid, winner, recompute, NOW)
    public.append(line)
    assert "dinner reopens" in line
    snap = {x["kind"]: x for x in db.snapshot(eid)["segments"]}
    assert snap["golf"]["locked"] is None and snap["dinner"]["locked"] is None
    assert snap["golf"]["options"] and snap["dinner"]["options"]
    assert db.approvals(golf) == {} and db.approvals(dinner) == {}

    # privacy across everything the channel saw
    blob = "\n".join(public) + card_text(eid)
    for secret in ("vegetarian",):
        assert not any(secret in x for x in public)
    assert "diet" not in " ".join(public).lower().replace("dietary", "")
    for why in ("dentist", "sick", "doctor", "because"):
        assert why not in blob.lower()


def test_poll_text_never_names_voters():
    eid = db.create_event(1, ANN, "Ann", ["golf"])
    (golf,) = segs(eid)
    for pid_, n in ((BO, "Bo"), (CY, "Cy"), (DI, "Di")):
        db.toggle(golf, pid_, n)
    pid = polls.open_poll("conflict", eid, golf, ("without", "everyone"), {ANN, BO, CY}, ANN, DI, now=1.0, ttl=100)
    polls.cast(pid, BO, "without", 2.0)
    t = polls.render(pid)
    assert "Bo" not in t and "Cy" not in t and "1 of 3 voted" in t


def test_without_path_recomputes_without_the_subject():
    eid = db.create_event(1, ANN, "Ann", ["golf"])
    (golf,) = segs(eid)
    for pid_, n in ((BO, "Bo"), (DI, "Di")):
        db.toggle(golf, pid_, n)
    say(ANN, "Ann", eid, free=[win("07:00", "12:00")])
    say(BO, "Bo", eid, free=[win("07:00", "12:00")])
    say(DI, "Di", eid, free=[win("13:00", "23:00")])
    recompute(eid)
    (pid,) = polls.detect_conflicts(eid, now=1.0)
    polls.cast(pid, ANN, "without", 2.0); polls.cast(pid, BO, "without", 2.0)
    state, winner = polls.resolve(pid, 2.0)
    line, more = polls.finish(pid, winner, recompute, NOW)
    assert (state, winner, more) == ("decided", "without", None) and "going without Di" in line
    assert db.segment_participants(golf) == {ANN, BO}
    assert db.options(golf)[0]["blocked_by"] == []


def test_migration_of_existing_m3_database(tmp_path):
    """A planbot.db created by M3 (no votes/locks/tiebreak/polls tables) opens and works after init()."""
    f = tmp_path / "old.db"
    con = sqlite3.connect(f)
    con.executescript(db.SCHEMA.split("CREATE TABLE IF NOT EXISTS votes")[0].replace(" IF NOT EXISTS", ""))
    con.execute("INSERT INTO persons VALUES(1,'Ann')")
    con.execute("INSERT INTO events(id,channel_id,proposer_id,created_at) VALUES(1,5,1,0)")
    con.execute("INSERT INTO segments(id,event_id,kind,ord) VALUES(1,1,'golf',0)")
    con.execute("INSERT INTO participation VALUES(1,1,'in')")
    con.commit(); con.close()
    db.init(str(f))
    db.init(str(f))  # idempotent
    assert db.snapshot(1)["segments"][0]["locked"] is None
    assert polls.request_reschedule(1, 1, now=1.0) is not None  # polls tables are created lazily


def test_card_components_and_embed_limits():
    eid = db.create_event(1, ANN, "Ann", ["a" * 20, "b" * 20, "c" * 20, "d" * 20])
    for sid in segs(eid):
        db.toggle(sid, BO, "B" * 32)
        db.save_options(sid, [{"start": f"{SUN}T{h}:00", "end": f"{SUN}T{h+1}:00", "venue_id": f"v{h}", "venue_name": "V" * 40,
                               "address": "x", "source_url": "http://example.com/" + "p" * 100, "hours_status": "verified",
                               "checked_at": "2026-10-24T12:00", "blocked_by": ["N" * 32] * 3, "unknown_n": 1}
                              for h in (10, 11, 12)])
    snap = db.snapshot(eid)
    v = bot.card_view(snap)
    items = v.children
    assert len(items) <= 25 and max(i.item.row for i in items) <= 4
    assert all(len(i.item.custom_id) <= 100 and len(i.item.label) <= 80 for i in items)
    e = plan.render(snap, {"Preferences": "x" * 300, "Schedule": "y" * 300, "Venue": "z" * 600})
    assert len(e) <= 6000 and all(len(f.value) <= 1024 and len(f.name) <= 256 for f in e.fields)


def test_stale_poll_and_vote_buttons_after_restart_are_harmless():
    """Buttons for rows that no longer exist answer politely instead of raising (restart with a fresh db)."""
    resp = _Resp()
    i = SimpleNamespace(user=SimpleNamespace(id=1), response=resp, client=SimpleNamespace())
    asyncio.run(ui_polls.handle_press(i, 999, "yes"))
    assert resp.sent == ["This vote is closed."]


class _Resp:
    def __init__(self):
        self.sent, self.edited = [], None

    async def send_message(self, text=None, **kw):
        self.sent.append(text)

    async def edit_message(self, **kw):
        self.edited = kw

    async def defer(self, **kw):
        pass


class _Chan:
    def __init__(self):
        self.calls = []
        self.id = 5

    async def send(self, content=None, **kw):
        self.calls.append(("send", content, kw))
        return SimpleNamespace(id=77)

    async def fetch_message(self, mid):
        chan = self

        class M:
            async def edit(self, **kw):
                chan.calls.append(("edit", kw.get("content"), kw))
        return M()


def _client(chan):
    c = SimpleNamespace(recomputes=[], card_status={})
    c.schedule_recompute = c.recomputes.append
    c.get_channel = lambda _id: chan
    return c


def _opt(start, vid, blocked=()):
    return {"start": start, "end": start.replace("T10", "T14"), "venue_id": vid, "venue_name": vid, "address": "a",
            "source_url": None, "hours_status": "unverified", "checked_at": None, "blocked_by": list(blocked), "unknown_n": 0}


# ---- confirmed bugs (strict xfail: they flip to failures once fixed, so remove the marker then) ----------------

@pytest.mark.xfail(strict=True, reason="BUG: tentative_out shrinks the electorate but nobody calls settle(); if everyone "
                                       "left already voted the segment never locks until someone re-votes")
def test_bug_conflict_tentative_out_completes_vote():
    eid = db.create_event(1, ANN, "Ann", ["golf"])
    (golf,) = segs(eid)
    for pid_, n in ((BO, "Bo"), (CY, "Cy")):
        db.toggle(golf, pid_, n)
    o = _opt(f"{SUN}T10:00", "a", blocked=["Cy"])
    db.save_options(golf, [o])
    db.vote_toggle(golf, db.option_key(o), ANN); db.vote_toggle(golf, db.option_key(o), BO)
    assert db.settle(golf) == "open"  # Cy has not voted
    chan = _Chan()
    asyncio.run(ui_polls.on_recompute_done(_client(chan), eid))
    assert db.segment_participants(golf) == {ANN, BO}  # Cy is tentatively out
    assert db.snapshot(eid)["segments"][0]["status"] == "locked"  # everyone left has voted


@pytest.mark.xfail(strict=True, reason="BUG: 'go without them' removes the person but never settles, same stuck vote")
def test_bug_without_outcome_completes_vote():
    eid = db.create_event(1, ANN, "Ann", ["golf"])
    (golf,) = segs(eid)
    for pid_, n in ((BO, "Bo"), (CY, "Cy")):
        db.toggle(golf, pid_, n)
    o = _opt(f"{SUN}T10:00", "a")
    db.save_options(golf, [o])
    db.vote_toggle(golf, db.option_key(o), ANN); db.vote_toggle(golf, db.option_key(o), BO)
    pid = polls.open_poll("conflict", eid, golf, ("without", "everyone"), {ANN, BO}, ANN, CY, now=1.0, ttl=100)
    polls.set_status(CY, golf, "tentative_out")
    polls.cast(pid, ANN, "without", 2.0); polls.cast(pid, BO, "without", 2.0)
    chan = _Chan()
    asyncio.run(ui_polls.finalize(_client(chan), pid, "without"))
    assert db.snapshot(eid)["segments"][0]["status"] == "locked"


@pytest.mark.xfail(strict=True, reason="BUG: VoteButton requires the clicker to be in the segment, so an organizer who "
                                       "toggled out can never break a round-2 tie (permanent deadlock)")
def test_bug_organizer_out_cannot_break_tie():
    eid = db.create_event(1, ANN, "Ann", ["golf"])
    (golf,) = segs(eid)
    db.toggle(golf, BO, "Bo")
    a, b = _opt(f"{SUN}T10:00", "a"), _opt(f"{SUN}T11:00", "b")
    db.save_options(golf, [a, b])
    for _ in range(2):
        db.vote_toggle(golf, db.option_key(a), ANN); db.vote_toggle(golf, db.option_key(b), BO); db.settle(golf)
    assert db.snapshot(eid)["segments"][0]["tiebreak"]["round"] == 2
    db.toggle(golf, ANN, "Ann")  # organizer leaves this activity
    i = SimpleNamespace(user=SimpleNamespace(id=ANN, display_name="Ann"), response=_Resp(), message=SimpleNamespace(pinned=True),
                        client=_client(_Chan()))

    async def refresh(_e):
        pass
    i.client.refresh_card = refresh
    asyncio.run(bot.PickButton(golf, 0, db.key_hash(db.option_key(a))).callback(i))
    assert db.locked(golf) is not None


@pytest.mark.xfail(strict=True, reason="BUG: /edit deleting a segment leaves its open poll behind; polls.render/finish then "
                                       "crash on the missing segment (TypeError) and the poll message keeps live buttons")
def test_bug_edit_removing_segment_orphans_poll():
    eid = db.create_event(1, ANN, "Ann", ["golf", "dinner"])
    golf, _ = segs(eid)
    db.toggle(golf, BO, "Bo")
    pid = polls.request_reschedule(golf, ANN, now=1.0)
    db.set_segments(eid, ["dinner"], ANN)
    polls.render(pid)  # raises TypeError today
    assert polls.get(pid)["status"] == "closed"


@pytest.mark.xfail(strict=True, reason="BUG: people are keyed by display name in people_for_segment/blocked_by; two members "
                                       "called the same collapse into one (one person's availability is lost)")
def test_bug_duplicate_display_names_collapse():
    eid = db.create_event(1, ANN, "Ann", ["golf"])
    (golf,) = segs(eid)
    db.toggle(golf, 21, "Sam"); db.toggle(golf, 22, "Sam")
    assert len(db.people_for_segment(golf)) == 3


@pytest.mark.xfail(strict=True, reason="BUG: finalize()/restore edits and follow-up sends carry user display names but no "
                                       "allowed_mentions, so a name like '@everyone' or '<@id>' can ping")
def test_bug_finalize_allowed_mentions():
    eid = db.create_event(1, ANN, "Ann", ["golf"])
    (golf,) = segs(eid)
    db.toggle(golf, BO, "Bo"); db.toggle(golf, CY, "@everyone")
    pid = polls.open_poll("conflict", eid, golf, ("without", "everyone"), {ANN, BO}, ANN, CY, now=1.0, ttl=100)
    polls.set_message(pid, 55)
    polls.cast(pid, ANN, "without", 2.0); polls.cast(pid, BO, "without", 2.0)
    polls.resolve(pid, 2.0)
    chan = _Chan()
    asyncio.run(ui_polls.finalize(_client(chan), pid, "without"))
    named = [c for c in chan.calls if "@everyone" in (c[1] or "")]
    assert named and all("allowed_mentions" in c[2] for c in named)


@pytest.mark.xfail(strict=True, reason="BUG: polls on a cancelled plan still accept votes / reschedule requests and apply "
                                       "outcomes (no cancelled-event guard in cast/request_reschedule/handle_press)")
def test_bug_poll_on_cancelled_event_still_open():
    eid = db.create_event(1, ANN, "Ann", ["golf"])
    (golf,) = segs(eid)
    db.toggle(golf, BO, "Bo")
    pid = polls.request_reschedule(golf, ANN, now=1.0)
    db.cancel(eid)
    assert not polls.cast(pid, BO, "yes", 2.0)


@pytest.mark.xfail(strict=True, reason="BUG: plan.parse_intent does not validate segment names (parse_kinds does); an LLM "
                                       "kind over ~70 chars makes the 'I'm in: <kind>' label exceed Discord's 80 and kickoff fails")
def test_bug_intent_kind_unvalidated_breaks_card():
    kinds = plan.parse_intent(json.dumps({"plan": True, "segments": ["x" * 90]}))
    eid = db.create_event(1, ANN, "Ann", kinds)
    assert all(len(i.item.label) <= 80 for i in bot.card_view(db.snapshot(eid)).children)


@pytest.mark.xfail(strict=True, reason="BUG: 'Group needs' field is not capped at 1024 chars (area/diet text from the model is "
                                       "unbounded); Discord rejects the edit so the card stops updating")
def test_bug_group_needs_field_over_1024():
    eid = db.create_event(1, ANN, "Ann", ["golf"])
    db.toggle(segs(eid)[0], BO, "Bo")
    say(BO, "Bo", eid, area="Tempe " * 200)
    e = plan.render(db.snapshot(eid))
    assert all(len(f.value) <= 1024 for f in e.fields)


@pytest.mark.xfail(strict=True, reason="BUG: render() cuts a long segment field at 1020 chars from the end, so with many "
                                       "participants the options and the Provisional label are silently dropped")
def test_bug_many_participants_truncate_options():
    eid = db.create_event(1, ANN, "Ann", ["golf"])
    (golf,) = segs(eid)
    for k in range(40):
        db.toggle(golf, 100 + k, f"Participant-number-{k:02d}-long-name")
    db.save_options(golf, [_opt(f"{SUN}T10:00", "a")])
    e = plan.render(db.snapshot(eid))
    assert "Provisional, not locked" in e.fields[0].value


@pytest.mark.xfail(strict=True, reason="BUG: locking is per segment with no ordering check; with dinner locked first, golf "
                                       "options (computed first, unconstrained) can end after the locked dinner starts")
def test_bug_golf_option_overlaps_locked_dinner():
    eid = db.create_event(1, ANN, "Ann", ["golf", "dinner"])
    golf, dinner = segs(eid)
    db.toggle(golf, BO, "Bo"); db.toggle(dinner, BO, "Bo")
    for who, n in ((ANN, "Ann"), (BO, "Bo")):
        say(who, n, eid, free=[win("13:00", "23:00")])
    d = _opt(f"{SUN}T17:00", "d")
    d["end"] = f"{SUN}T18:30"
    db.lock_segment(dinner, d)
    recompute(eid)
    g = db.options(golf)[0]
    assert datetime.fromisoformat(g["end"]) + schedule.TRAVEL <= datetime.fromisoformat(d["start"])
