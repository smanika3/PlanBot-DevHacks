"""Discord side of the conflict and reschedule polls. Thin: all decisions live in polls.py."""
import logging
import time

import discord

import db
import engine
import polls

LABELS = {"without": "Go without them", "everyone": "Find a time for everyone", "yes": "Yes, reschedule",
          "no": "No, keep it", "back": "I can make it"}


class PollButton(discord.ui.DynamicItem[discord.ui.Button], template=r"poll:(?P<pid>\d+):(?P<choice>[a-z]+)"):
    """Custom id poll:{id}:{choice}; dynamic so buttons on old polls survive a restart."""

    def __init__(self, pid: int, choice: str):
        style = discord.ButtonStyle.success if choice == "back" else discord.ButtonStyle.primary
        super().__init__(discord.ui.Button(label=LABELS.get(choice, choice), style=style, custom_id=f"poll:{pid}:{choice}"))
        self.pid, self.choice = pid, choice

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["pid"]), match["choice"])

    async def callback(self, i: discord.Interaction):
        await handle_press(i, self.pid, self.choice)


def poll_view(poll_id: int) -> discord.ui.View:
    p = polls.get(poll_id)
    v = discord.ui.View(timeout=None)
    for c in p["choices"] + (["back"] if p["kind"] == "conflict" else []):
        v.add_item(PollButton(poll_id, c))
    return v


async def handle_press(i: discord.Interaction, pid: int, choice: str):
    """Answers within 3 seconds: every step is a tiny local query, Discord follow-ups come after the response."""
    client, uid = i.client, i.user.id
    p = polls.get(pid)
    if not p or p["status"] != "open":
        await i.response.send_message("This vote is closed.", ephemeral=True)
    elif choice == "back":
        if polls.restore(pid, uid, client.schedule_recompute):
            await i.response.edit_message(content=f"{polls.name_of(uid)} can make it, so the vote is cancelled.", view=None)
        else:
            await i.response.send_message("Only the person this is about can press that.", ephemeral=True)
    elif polls.cast(pid, uid, choice):
        state, winner = polls.resolve(pid)
        if state == "decided":
            await i.response.defer()
            await finalize(client, pid, winner)
        else:
            await i.response.edit_message(content=polls.render(pid), view=poll_view(pid))
    elif choice in p["choices"] and uid == polls.proposer_of(p) and polls.proposer_break(pid, uid, choice):
        state, winner = polls.resolve(pid)  # the organizer is not a voter here, so this settles a tie
        await i.response.defer()
        if state == "decided":
            await finalize(client, pid, winner)
    else:
        await i.response.send_message("Only people in this activity can vote.", ephemeral=True)


def _channel(client, poll):
    return client.get_channel(db.con.execute("SELECT channel_id FROM events WHERE id=?", (poll["event_id"],)).fetchone()[0])


async def finalize(client, pid: int, winner: str):
    """Apply the (already closed) poll, edit its message with the outcome, post any follow-up."""
    p = polls.get(pid)
    line, more = polls.finish(pid, winner, client.schedule_recompute, engine.local_now())
    ch = _channel(client, p)
    if ch is None:
        return
    try:
        if p["message_id"]:
            await (await ch.fetch_message(p["message_id"])).edit(content=line, view=None)
        else:
            await ch.send(line)
        if more:
            await ch.send(more)
    except discord.HTTPException:
        logging.warning("could not post poll outcome")


async def on_recompute_done(client, event_id: int):
    """Call after a recompute finished (and `running` was cleared): open conflict polls for the leading option."""
    ids = polls.detect_conflicts(event_id)
    for pid in ids:
        ch = _channel(client, polls.get(pid))
        try:
            m = await ch.send(polls.render(pid), view=poll_view(pid), allowed_mentions=discord.AllowedMentions.none())
            polls.set_message(pid, m.id)
        except (discord.HTTPException, AttributeError):
            logging.warning("could not post conflict notice")
    if ids:
        client.schedule_recompute(event_id)  # the tentative-out person is no longer counted


async def reschedule_command(client, i: discord.Interaction, activity: str = None):
    ev = db.open_event(i.channel_id)
    if not ev:
        await i.response.send_message("No open plan in this channel.", ephemeral=True)
        return
    seg, msg = polls.pick_segment(ev["id"], i.user.id, activity)
    if seg is None:
        await i.response.send_message(msg, ephemeral=True)
        return
    pid = polls.request_reschedule(seg, i.user.id)
    if pid is None:
        await i.response.send_message("A vote is already open for that activity.", ephemeral=True)
        return
    await i.response.send_message(polls.render(pid), view=poll_view(pid), allowed_mentions=discord.AllowedMentions.none())
    polls.set_message(pid, (await i.original_response()).id)


async def sweep(client):
    """Once a minute: close polls past their deadline and apply the result."""
    for pid in polls.due(time.time()):
        state, winner = polls.resolve(pid, time.time())
        if state == "decided":
            await finalize(client, pid, winner)


def register(client):
    client.add_dynamic_items(PollButton)
