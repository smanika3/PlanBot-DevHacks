"""Planning-intent detection and the kickoff card."""
import json
import logging
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

import llm

log = logging.getLogger("plan")

# Cheap gate so ordinary chatter never costs an LLM call.
HINT = re.compile(r"\b(wanna|want to|let'?s|down to|should we|who'?s (in|down)|we should|anyone (want|up))\b", re.I)
SYSTEM = ('You classify one Discord message. Reply with JSON only: {"plan": true|false, "segments": ["golf","dinner"]}. '
          "plan is true only if the author proposes a group activity. "
          "segments: ordered one-word lowercase activity kinds, at most 3.")


def parse_intent(out: str):
    """LLM output -> list of segment kinds, or None if not a plan."""
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return None
    try:
        j = json.loads(m.group())
    except ValueError:
        return None
    if j.get("plan") is not True:
        return None
    segs = []
    for s in j.get("segments", []):
        for k in (parse_kinds(s) or []) if isinstance(s, str) else []:  # same rules as /edit; invalid ones are dropped
            if k not in segs:
                segs.append(k)
    return segs[:3] or ["hangout"]


def detect(text: str):
    if not HINT.search(text):
        return None
    for _ in range(2):  # one retry on bad JSON or a transient error
        try:
            return parse_intent(llm.complete(text, SYSTEM, 100))
        except Exception:
            log.exception("intent call failed")
    return None


def parse_kinds(text: str):
    """'Golf, mini golf , dinner' -> ['golf', 'mini golf', 'dinner']; None if empty or invalid. Max 4."""
    kinds = []
    for k in re.split(r",|\bthen\b|→|->", text.lower()):
        k = " ".join(k.split())
        if not k:
            continue
        if not re.fullmatch(r"[a-z][a-z \-]{0,19}", k):
            return None
        if k not in kinds:
            kinds.append(k)
    return kinds[:4] or None


def _core(o: dict) -> str:
    """when · where · hours, shared by option lines and the lock line."""
    s, e = datetime.fromisoformat(o["start"]), datetime.fromisoformat(o["end"])
    when = f"{s:%a %b %-d}, {s:%-I:%M%p}–{e:%-I:%M%p}".replace("AM", "am").replace("PM", "pm")
    if o["venue_name"]:
        src = f" ([source]({o['source_url']}))" if o["source_url"] else ""
        where = f"**{o['venue_name']}**{src}"
    else:
        where = "venue TBD (no venue data for this activity)"
    if o["hours_status"] == "verified":
        hours = f"hours confirmed {datetime.fromisoformat(o['checked_at']).strftime('%b %-d %-I:%M%p').lower()}"
    else:
        hours = "⚠️ hours unverified"
    return f"{when} · {where} · {hours}"


def option_line(letter: str, o: dict) -> str:
    who = f"can't: {', '.join(o['blocked_by'])}" if o["blocked_by"] else "everyone who replied is free"
    if o["unknown_n"]:
        who += f" · {o['unknown_n']} haven't shared times"
    votes = f" · 👍 {o['approvals']}" if "approvals" in o else ""
    return f"**{letter}.** {_core(o)} · {who}{votes}"


def render_segment(s: dict) -> str:
    value = ", ".join(s["in"]) or "nobody yet"
    if len(value) > 200:  # shorten the names, never the options below
        head = value[:200].rsplit(", ", 1)[0]
        value = f"{head} +{len(s['in']) - head.count(', ') - 1} more"
    if s.get("locked"):
        out = value + f"\n🔒 **Locked:** {_core(s['locked'])}"
        return out + (f"\n✅ Booked by {s['booked_by']}" if s.get("booked_by") else "")
    if s["options"]:
        tb = s.get("tiebreak")
        if tb:
            tied = ", ".join("ABC"[j] for j, o in enumerate(s["options"]) if o["key"] in tb["keys"])
            value += (f"\n*Tie between {tied}: re-vote on those.*" if tb["round"] == 1
                      else f"\n*Still tied between {tied}: the organizer picks.*")
        elif s.get("total"):
            value += f"\n{s['voted']} of {s['total']} voted (approve every option you can do)"
        value += "\n" + "\n".join(option_line("ABC"[j], o) for j, o in enumerate(s["options"]))
        value += "\n*Provisional, not locked.*"
    return value


def _cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


def render(snap: dict, status: dict = None) -> discord.Embed:
    kinds = " → ".join(s["kind"] for s in snap["segments"])
    e = discord.Embed(title=f"Plan: {kinds}", description=f"Proposed by <@{snap['proposer_id']}>")
    has_options = any(s["options"] for s in snap["segments"])
    for i, s in enumerate(snap["segments"], 1):
        e.add_field(name=f"{i}. {s['kind'].title()}", value=render_segment(s)[:1020], inline=False)
    c = snap.get("constraints") or {}
    needs = []
    if c.get("diet"):
        needs.append("Dietary: " + ", ".join(c["diet"]))  # aggregate only, never who
    if c.get("budget"):
        needs.append("Budget: " + "$" * c["budget"] + " or less")
    if c.get("area"):
        needs.append(f"Area: {c['area'][0]} ({'required' if c['area'][1] else 'preferred'})")
    if needs:
        e.add_field(name="Group needs", value=_cap("\n".join(needs), 1024), inline=False)
    if status:
        e.add_field(name="Agents", value="\n".join(f"**{k}:** {v}" for k, v in status.items())[:1020], inline=False)
    if has_options:
        e.set_footer(text=f"Provisional · {snap['replied']} of {snap['n_in']} replied")
    else:
        e.set_footer(text=(f"Planning started · {snap['replied']} of {snap['n_in']} shared times"
                           if snap["planning"] else "Planning starts once one more person is in"))
    return e


# Cheap gate: only messages that look like availability, diet, budget, area or in/out go to the model.
LISTEN = re.compile(
    r"\b(free|busy|available|can'?t|cannot|after|before|tonight|tomorrow|weekend|mon|tue|wed|thu|fri|sat|sun|"
    r"\d\s?(am|pm)|budget|cheap|around|near|downtown|campus|area|in for|there for|skip|i'?m out|count me)"
    r"|allerg|nut|vegan|vegetarian|gluten|halal|kosher|dairy|diet|pescatarian|shellfish|lactose|celiac|\$", re.I)
LISTEN_SYSTEM = """Extract planning info from one chat message. Today is {today}. Times are local, no timezone.
Reply with JSON only, using null or [] when absent:
{"free": [{"start": "YYYY-MM-DDTHH:MM", "end": "YYYY-MM-DDTHH:MM"}], "busy": [same shape],
 "diet": ["vegetarian"], "diet_hard": true|false, "budget": 1-4 or null, "area": "short place name" or null,
 "area_hard": true|false, "segments": [{"kind": "golf", "status": "in"|"out"}]}
free = windows the person can do; busy = windows they cannot. Weekday names always mean the next such day
on or after today, never a past date. "after 6" ends at 23:59 the same day. diet_hard is true for allergies or "has to be",
false for "prefer". budget: 1 cheap to 4 expensive; N dollar signs means N ("under $$" is 2). area_hard is true only for "has to be" style wording;
"preferably Tempe" is false. segments only when the person says they are in or out of an activity; kind is a
one-word lowercase activity. Never infer anything that was not stated."""


def _windows(items):
    out = []
    for w in items if isinstance(items, list) else []:
        try:
            s, e = datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if s < e:
            out.append((s.isoformat(timespec="minutes"), e.isoformat(timespec="minutes")))
    return out


def parse_listen(out: str):
    """LLM output -> validated dict, or None when nothing usable was said."""
    m = re.search(r"\{.*\}", out, re.S)
    try:
        j = json.loads(m.group()) if m else None
    except ValueError:
        j = None
    if not isinstance(j, dict):
        return None
    diet = [d.strip().lower()[:40] for d in j.get("diet") or [] if isinstance(d, str) and d.strip()][:8]
    budget = j.get("budget")
    area = j.get("area")
    p = {
        "free": _windows(j.get("free")), "busy": _windows(j.get("busy")),
        "diet": diet, "diet_hard": j.get("diet_hard") is True,
        "budget": budget if isinstance(budget, int) and 1 <= budget <= 4 else None,
        "area": area.strip()[:60] if isinstance(area, str) and area.strip() else None,
        "area_hard": j.get("area_hard") is True,
        "segments": [(s["kind"].strip().lower(), s["status"]) for s in j.get("segments") or []
                     if isinstance(s, dict) and isinstance(s.get("kind"), str) and s.get("status") in ("in", "out")],
    }
    return p if any(p[k] for k in ("free", "busy", "diet", "budget", "area", "segments")) else None


def listen(text: str, gated=True):
    if gated and not LISTEN.search(text):  # DMs skip the gate: they are always meant for the bot
        return None
    today = datetime.now(ZoneInfo(os.environ.get("TZ_NAME", "America/Phoenix"))).strftime("%A %Y-%m-%d")
    system = LISTEN_SYSTEM.replace("{today}", today)
    for _ in range(2):
        try:
            return parse_listen(llm.complete(text, system, 400))
        except Exception:
            log.exception("listen call failed")
    return None


def _span(s, e):
    s, e = datetime.fromisoformat(s), datetime.fromisoformat(e)
    end = e.strftime("%-I:%M%p") if s.date() == e.date() else e.strftime("%a %-I:%M%p")
    return f"{s.strftime('%a %-I:%M%p')}–{end}".lower()


def echo(p: dict):
    """(public, private). Diet only ever appears in the private part."""
    pub = []
    for label, key in (("free", "free"), ("busy", "busy")):
        pub += [f"{label} {_span(s, e)}" for s, e in p[key]]
    for kind, status in p["segments"]:
        pub.append(f"{status} for {kind}")
    if p["budget"]:
        pub.append("budget " + "$" * p["budget"])
    if p["area"]:
        pub.append(f"area {p['area']} ({'must' if p['area_hard'] else 'preferably'})")
    priv = ""
    if p["diet"]:
        priv = f"diet {', '.join(p['diet'])} ({'must' if p['diet_hard'] else 'preference'}), shared with the group only as an anonymous need"
    return ("Understood: " + " · ".join(pub) + ". Reply to correct me.") if pub else "", priv
