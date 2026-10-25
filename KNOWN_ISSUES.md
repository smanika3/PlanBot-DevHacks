# Known issues

## Bot sometimes doesn't pick up changes to plan details (reported 2026-10-25, open)

**Symptom:** after the first plan card, an owner message changing plan details did not update the card.

**Not confirmed, possible causes to check after M3 is built:**
- Keyword gate false negatives. Fixed once for diet and money words (`39a9f4a`); other phrasings may still slip through.
- Chat messages can only change a person's own availability, diet, budget, area and in/out. There is no way to change the plan's activities from chat; `/edit` (organizer only) was added for that.
- Only one open plan per channel, so a new proposal while a plan is open is treated as a reply to the old plan, not a new plan.
- Unpinning the card does not break the bot (verified); the card is now re-pinned automatically.

**To recheck:** run the demo script end to end, then try changing availability, area, budget, diet and activities, in channel and in DM, and confirm the card updates each time. Check `bot.log` for `intent call failed` or `listen call failed` (Bedrock throttling).
