import db
import plan


def test_parse_intent():
    assert plan.parse_intent('{"plan": true, "segments": ["Golf", "dinner"]}') == ["golf", "dinner"]
    assert plan.parse_intent('ok {"plan": true, "segments": []}') == ["hangout"]
    assert plan.parse_intent('{"plan": false, "segments": ["golf"]}') is None
    assert plan.parse_intent("not json") is None
    assert len(plan.parse_intent('{"plan": true, "segments": ["a","b","c","d"]}')) == 3


def test_hint_gate():
    assert plan.HINT.search("free this weekend, wanna golf? then dinner after")
    assert not plan.HINT.search("the weather is nice today")


def test_participation_and_planning_start():
    db.init(":memory:")
    eid = db.create_event(1, 10, "Ann", ["golf", "dinner"])
    snap = db.snapshot(eid)
    assert not snap["planning"] and snap["segments"][0]["in"] == ["Ann"]
    golf = snap["segments"][0]["id"]
    db.toggle(golf, 11, "Bo")
    snap = db.snapshot(eid)
    assert snap["planning"] and snap["segments"][0]["in"] == ["Ann", "Bo"] and snap["segments"][1]["in"] == ["Ann"]
    db.toggle(golf, 11, "Bo")  # toggles back out
    assert db.snapshot(eid)["segments"][0]["in"] == ["Ann"]
