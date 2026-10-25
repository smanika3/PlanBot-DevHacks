"""Planning-intent detection and the kickoff card."""
import json
import logging
import re

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
    segs = [s.strip().lower() for s in j.get("segments", []) if isinstance(s, str) and s.strip()][:3]
    return segs or ["hangout"]


def detect(text: str):
    if not HINT.search(text):
        return None
    for _ in range(2):  # one retry on bad JSON or a transient error
        try:
            return parse_intent(llm.complete(text, SYSTEM, 100))
        except Exception:
            log.exception("intent call failed")
    return None


def render(snap: dict) -> discord.Embed:
    kinds = " → ".join(s["kind"] for s in snap["segments"])
    e = discord.Embed(title=f"Plan: {kinds}", description=f"Proposed by <@{snap['proposer_id']}>")
    for i, s in enumerate(snap["segments"], 1):
        e.add_field(name=f"{i}. {s['kind'].title()}", value=", ".join(s["in"]) or "nobody yet", inline=False)
    e.set_footer(text="Planning started" if snap["planning"] else "Planning starts once one more person is in")
    return e
