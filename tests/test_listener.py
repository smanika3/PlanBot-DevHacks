import db
import plan

OUT = """{"free": [{"start": "2026-10-25T09:00", "end": "2026-10-25T13:00"}], "busy": [],
 "diet": ["Vegetarian"], "diet_hard": true, "budget": 2, "area": "Tempe", "area_hard": false,
 "segments": [{"kind": "golf", "status": "out"}, {"kind": "dinner", "status": "in"}]}"""


def test_parse_listen_validates():
    p = plan.parse_listen(OUT)
    assert p["free"] == [("2026-10-25T09:00", "2026-10-25T13:00")]
    assert p["diet"] == ["vegetarian"] and p["diet_hard"] and p["budget"] == 2 and not p["area_hard"]
    assert p["segments"] == [("golf", "out"), ("dinner", "in")]
    assert plan.parse_listen("nope") is None
    assert plan.parse_listen('{"free": [{"start": "bad", "end": "x"}], "budget": 9}') is None  # nothing valid


def test_gate_catches_diet_and_money_wording():
    for t in ["also no peanuts, allergic", "i'm allergic to shellfish", "keep it under $20", "I'm vegetarian",
              "gluten-free please", "can't do saturday"]:
        assert plan.LISTEN.search(t), t
    assert not plan.LISTEN.search("lol that movie was great")


def test_cancel_closes_event():
    db.init(":memory:")
    eid = db.create_event(1, 10, "Ann", ["golf"])
    assert db.open_event(1) and db.event_for_person(10)
    db.cancel(eid)
    assert db.open_event(1) is None and db.event_for_person(10) is None
    assert db.snapshot(eid)["status"] == "cancelled"


def test_echo_never_puts_diet_in_public():
    pub, priv = plan.echo(plan.parse_listen(OUT))
    assert "vegetarian" not in pub.lower() and "vegetarian" in priv
    assert "out for golf" in pub and "area Tempe (preferably)" in pub


def test_aggregate_constraints_have_no_names():
    db.init(":memory:")
    eid = db.create_event(1, 10, "Ann", ["golf", "dinner"])
    golf, dinner = [s["id"] for s in db.snapshot(eid)["segments"]]
    db.apply(eid, 10, "Ann", plan.parse_listen(OUT))  # Ann: out of golf, in for dinner
    db.toggle(dinner, 11, "Bo")
    db.apply(eid, 11, "Bo", plan.parse_listen('{"diet": ["gluten-free"], "budget": 1}'))
    snap = db.snapshot(eid)
    assert snap["constraints"] == {"diet": ["gluten-free", "vegetarian"], "budget": 1, "area": ("Tempe", False)}
    needs = [f for f in plan.render(snap).to_dict()["fields"] if f["name"] == "Group needs"][0]["value"]
    assert "Dietary: gluten-free, vegetarian" in needs and "Ann" not in needs and "Bo" not in needs
    segs = {s["kind"]: s["in"] for s in snap["segments"]}
    assert segs["golf"] == [] and segs["dinner"] == ["Ann", "Bo"]
    # a later availability statement replaces the earlier one
    db.apply(eid, 10, "Ann", plan.parse_listen('{"busy": [{"start": "2026-10-26T18:00", "end": "2026-10-26T23:00"}]}'))
    rows = db.con.execute("SELECT kind FROM avail WHERE person_id=10").fetchall()
    assert [r["kind"] for r in rows] == ["busy"]
