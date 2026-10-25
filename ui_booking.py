"""Discord side of booking. Thin: all decisions live in booking.py."""
import logging
import time

import discord

import booking
import db


class _Btn(discord.ui.DynamicItem[discord.ui.Button], template=r"x:(?P<sid>\d+)"):
    """Base: custom id {prefix}:{sid}; dynamic so buttons survive a restart."""
    prefix, label, style = "x", "", discord.ButtonStyle.primary

    def __init__(self, sid: int):
        super().__init__(discord.ui.Button(label=self.label, style=self.style, custom_id=f"{self.prefix}:{sid}"))
        self.sid = sid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["sid"]))


async def _dead(i: discord.Interaction, sid: int) -> bool:
    """Answer and return True if this button no longer applies (unlocked, cancelled or reset)."""
    if not booking.live(sid) or not booking.state(sid):
        await i.response.send_message("This booking is no longer active.", ephemeral=True)
        return True
    return False


def _channel(client, sid: int):
    r = db.con.execute("SELECT channel_id FROM events WHERE id=?", (db.event_of_segment(sid),)).fetchone()
    return client.get_channel(r["channel_id"])


def poll_text(sid: int) -> str:
    return f"Who'll book **{booking.kind_of(sid)}**? I won't book or pay for anything, I just need a volunteer."


def poll_view(sid: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(BookButton(sid))
    return v


def dm_view(sid: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(DoneButton(sid))
    v.add_item(BackButton(sid))
    return v


class BookButton(_Btn, template=r"book:(?P<sid>\d+)"):
    prefix, label, style = "book", "I'll book it", discord.ButtonStyle.success

    async def callback(self, i: discord.Interaction):
        sid, uid = self.sid, i.user.id
        if await _dead(i, sid):
            return
        if uid not in db.segment_participants(sid):
            await i.response.send_message("Only people in this activity can volunteer.", ephemeral=True)
            return
        ok, name = booking.volunteer(sid, uid)
        if not ok:
            await i.response.send_message(f"{name or 'Someone'} already volunteered.", ephemeral=True)
            return
        await i.response.edit_message(content=f"**{name}** is booking **{booking.kind_of(sid)}**.", view=None,
                                      allowed_mentions=discord.AllowedMentions.none())
        text = booking.dm_text(sid)
        try:
            await i.user.send(text, view=dm_view(sid))
            await i.followup.send("Thanks! I sent you the details by DM.", ephemeral=True)
        except discord.Forbidden:  # DMs closed: same prompt, visible only to them
            await i.followup.send(text, view=dm_view(sid), ephemeral=True)


class DoneButton(_Btn, template=r"bookdone:(?P<sid>\d+)"):
    prefix, label, style = "bookdone", "I booked it", discord.ButtonStyle.success

    async def callback(self, i: discord.Interaction):
        sid = self.sid
        if await _dead(i, sid):
            return
        if not booking.mark_booked(sid, i.user.id):
            await i.response.send_message("Only the volunteer can press that.", ephemeral=True)
            return
        await i.response.edit_message(content="Thanks, marked as booked.", view=None)
        try:
            await _channel(i.client, sid).send(f"{booking.kind_of(sid).title()} booked by {booking.booked_by(sid)}",
                                               allowed_mentions=discord.AllowedMentions.none())
        except (discord.HTTPException, AttributeError):
            logging.warning("could not post booked notice")
        try:
            await i.client.refresh_card(db.event_of_segment(sid))  # lead's card shows 'Booked by X'
        except Exception:
            logging.warning("could not refresh card after booking")


class BackButton(_Btn, template=r"bookback:(?P<sid>\d+)"):
    prefix, label, style = "bookback", "Hand back", discord.ButtonStyle.secondary

    async def callback(self, i: discord.Interaction):
        sid = self.sid
        if await _dead(i, sid):
            return
        if not booking.hand_back(sid, i.user.id):
            await i.response.send_message("Only the volunteer can press that.", ephemeral=True)
            return
        await i.response.edit_message(content="Handed back. I'll ask the group again.", view=None)
        await _post_poll(i.client, sid)


async def _post_poll(client, sid: int):
    try:
        await _channel(client, sid).send(poll_text(sid), view=poll_view(sid),
                                         allowed_mentions=discord.AllowedMentions.none())
    except (discord.HTTPException, AttributeError):
        logging.warning("could not post booking poll")


async def on_locked(client, event_id: int, segment_ids):
    """Call with the ids that just locked. Opens one volunteer poll per segment (idempotent)."""
    for sid in segment_ids:
        if booking.open_poll(sid) is not None:
            await _post_poll(client, sid)


async def sweep(client):
    """Once a minute: one reminder per segment nobody volunteered for, mentioning only its participants."""
    for sid in booking.due(time.time()):
        ids = sorted(db.segment_participants(sid))
        try:
            await _channel(client, sid).send(
                " ".join(f"<@{u}>" for u in ids) + f" nobody has volunteered to book **{booking.kind_of(sid)}** yet. "
                "Tap 'I'll book it' on the message above if you can.",
                allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=u) for u in ids]))
        except (discord.HTTPException, AttributeError):
            logging.warning("could not post booking reminder")


def register(client):
    client.add_dynamic_items(BookButton, DoneButton, BackButton)
