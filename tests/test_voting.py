import asyncio
from datetime import datetime
from types import SimpleNamespace

import bot
import db
import engine
import plan
import schedule
import venue

NOW = datetime(2026, 10, 24, 12, 0)
V = venue.load()


def opt(start, vid="v1", end=None):
    return {"start": start, "end": end or start.replace("T10", "T14"), "venue_id": vid, "venue_name": vid.upper(),
            "address": "a", "source_url": "http://x", "hours_status": "unverified", "checked_at": None,
            "blocked_by": [], "unknown_n": 0}


OPTS = [opt("2026-10-25T10:00", "a"), opt("2026-10-25T11:00", "b"), opt("2026-10-25T12:00", "c")]


def keys():
    return [db.option_key(o) for o in OPTS]


def setup(kinds=("golf",), people=(11, 12)):
    db.init(":memory:")
    eid = db.create_event(1, 10, "Ann", list(kinds))
    segs = db.snapshot(eid)["segments"]
    for s in segs:
        for p in people:
            db.toggle(s["id"], p, f"P{p}")
        db.save_options(s["id"], OPTS)
    return eid, [s["id"] for s in segs]


def seg(eid, i=0):
    return db.snapshot(eid)["segments"][i]


def test_everyone_votes_then_lock_to_winner():
    eid, (sid,) = setup()
    a, b, c = keys()
    db.vote_toggle(sid, a, 10); db.vote_toggle(sid, b, 10)
    db.vote_toggle(sid, b, 11)
    assert db.settle(sid) == "open"  # 12 has not voted
    db.vote_toggle(sid, c, 12); db.vote_toggle(sid, b, 12)
    assert db.settle(sid) == "locked"
    s = seg(eid)
    assert s["status"] == "locked" and s["locked"]["venue_id"] == "b"


def test_tie_revote_then_proposer_breaks():
    eid, (sid,) = setup(people=(11,))
    a, b, c = keys()
    db.vote_toggle(sid, a, 10); db.vote_toggle(sid, b, 11)
    assert db.settle(sid) == "revote"
    s = seg(eid)
    assert s["tiebreak"] == {"round": 1, "keys": sorted([a, b])} and s["voted"] == 0
    db.vote_toggle(sid, a, 10); db.vote_toggle(sid, b, 11)
    assert db.settle(sid) == "proposer"
    assert not db.pick_tie(sid, c)  # not a tied option
    assert db.pick_tie(sid, b)
    assert seg(eid)["locked"]["venue_id"] == "b"


def test_votes_survive_recompute_and_vanished_options_drop():
    eid, (sid,) = setup()
    a, b, c = keys()
    db.vote_toggle(sid, a, 10); db.vote_toggle(sid, c, 10)
    db.save_options(sid, [OPTS[0], opt("2026-10-25T15:00", "z")])  # c vanished, a reappears
    assert db.approvals(sid) == {a: {10}}
    assert seg(eid)["options"][0]["approvals"] == 1


def test_toggle_out_completes_vote_but_never_unlocks():
    eid, (sid,) = setup()
    a = keys()[0]
    db.vote_toggle(sid, a, 10); db.vote_toggle(sid, a, 11)
    assert db.settle_event(eid) == []
    db.toggle(sid, 12, "P12")  # 12 leaves
    assert db.settle_event(eid) == [sid]
    db.toggle(sid, 12, "P12")  # comes back in
    db.settle_event(eid)
    assert seg(eid)["status"] == "locked" and db.segment_participants(sid) == {10, 11, 12}


class FakeResp:
    def __init__(self):
        self.sent, self.edited = [], None

    async def send_message(self, text, **kw):
        self.sent.append(text)

    async def edit_message(self, **kw):
        self.edited = kw


def click(item, uid):
    resp = FakeResp()
    client = SimpleNamespace(card_status={}, recomputes=[], refreshed=[])
    client.schedule_recompute = client.recomputes.append

    async def refresh(eid):
        client.refreshed.append(eid)
    client.refresh_card = refresh
    i = SimpleNamespace(user=SimpleNamespace(id=uid, display_name="x"), response=resp, client=client,
                        message=SimpleNamespace(pinned=True))
    asyncio.run(item.callback(i))
    return resp, client


def test_button_click_and_stale_hash():
    eid, (sid,) = setup()
    h = db.key_hash(keys()[0])
    resp, client = click(bot.VoteButton(sid, 0, h), 11)
    assert resp.edited and db.approvals(sid) == {keys()[0]: {11}}
    assert client.recomputes == [eid]
    resp, _ = click(bot.VoteButton(sid, 0, h), 99)  # not in the segment
    assert resp.sent == ["You're not in this activity, tap I'm in first."]
    resp, client = click(bot.VoteButton(sid, 0, "deadbe"), 11)
    assert resp.sent == ["Options changed, please vote again."] and client.refreshed == [eid]
    assert db.approvals(sid) == {keys()[0]: {11}}


def test_pick_button_proposer_only():
    eid, (sid,) = setup(people=(11,))
    a, b, _ = keys()
    for _ in range(2):
        db.vote_toggle(sid, a, 10); db.vote_toggle(sid, b, 11); db.settle(sid)
    resp, _ = click(bot.PickButton(sid, 1, db.key_hash(b)), 11)
    assert resp.sent == ["Only the organizer can break this tie."] and seg(eid)["status"] != "locked"
    click(bot.PickButton(sid, 1, db.key_hash(b)), 10)
    assert seg(eid)["locked"]["venue_id"] == "b"


def test_4x3_layout_fits_discord_limits():
    eid, sids = setup(("golf", "dinner", "bowling", "study"))
    v = bot.card_view(db.snapshot(eid))
    assert len(v.children) == 16 <= 25 and {c.row for c in v.children} == {0, 1, 2, 3, 4}
    assert sum(c.row == 0 for c in v.children) == 4
    v.to_components()  # discord validates the layout when building
    db.lock_segment(sids[0], OPTS[0])
    assert len(bot.card_view(db.snapshot(eid)).children) == 4 + 9  # locked segment loses its buttons


def test_locked_segment_skipped_and_dinner_follows_it():
    db.init(":memory:")
    eid = db.create_event(1, 10, "Ann", ["golf", "dinner"])
    g, d = [s["id"] for s in db.snapshot(eid)["segments"]]
    for sid in (g, d):
        db.toggle(sid, 11, "Bo")
    free = '{"free": [{"start": "2026-10-25T07:00", "end": "2026-10-25T22:00"}]}'
    for pid, n in ((10, "Ann"), (11, "Bo")):
        db.apply(eid, pid, n, plan.parse_listen(free))
    hours = lambda v, s, e, now: {"status": "unverified", "checked_at": None, "source": v["website"]}
    db.lock_segment(g, opt("2026-10-25T07:00", "ken-mcdonald", end="2026-10-25T11:00"))
    engine.run(eid, NOW, V, hours_fn=hours)
    golf, dinner = db.snapshot(eid)["segments"]
    assert golf["options"] == [] and golf["locked"]["end"] == "2026-10-25T11:00"
    end = datetime(2026, 10, 25, 11, 0) + schedule.TRAVEL
    assert dinner["options"] and all(datetime.fromisoformat(o["start"]) >= end for o in dinner["options"])


def test_unlock_reopens_later_segments():
    eid, (a, b) = setup(("golf", "dinner"))
    for sid in (a, b):
        db.lock_segment(sid, OPTS[0])
        db.vote_toggle(sid, keys()[0], 10)
    db.unlock_segment(b)
    assert [s["status"] for s in db.snapshot(eid)["segments"]] == ["locked", "planning"]
    db.unlock_segment(a)
    snap = db.snapshot(eid)
    assert [s["status"] for s in snap["segments"]] == ["planning", "planning"]
    assert all(s["locked"] is None for s in snap["segments"]) and db.approvals(a) == {} == db.approvals(b)


def test_snapshot_never_leaks_voters():
    eid, (sid,) = setup()
    db.vote_toggle(sid, keys()[0], 11)
    snap = db.snapshot(eid)
    s = snap["segments"][0]
    assert s["options"][0]["approvals"] == 1 and (s["voted"], s["total"]) == (1, 3)
    assert not any(isinstance(v, (set, list)) and 11 in v for o in s["options"] for v in o.values())
    assert "voters" not in repr(s) and "person" not in repr(s)
    assert "1 of 3 voted" in repr(plan.render(snap).to_dict())
