# Known issues

## Bot sometimes doesn't pick up changes to plan details (reported 2026-10-25, open)

**Symptom:** after the first plan card, an owner message changing plan details did not update the card.

**Not confirmed, possible causes to check after M3 is built:**
- Keyword gate false negatives. Fixed once for diet and money words (`39a9f4a`); other phrasings may still slip through.
- Chat messages can only change a person's own availability, diet, budget, area and in/out. There is no way to change the plan's activities from chat; `/edit` (organizer only) was added for that.
- Only one open plan per channel, so a new proposal while a plan is open is treated as a reply to the old plan, not a new plan.
- Unpinning the card does not break the bot (verified); the card is now re-pinned automatically.

**To recheck:** run the demo script end to end, then try changing availability, area, budget, diet and activities, in channel and in DM, and confirm the card updates each time. Check `bot.log` for `intent call failed` or `listen call failed` (Bedrock throttling).

## M4 (voting, locking, conflicts, reschedule): issues reported while building (2026-10-25)

None of M4 has been exercised against live Discord yet. Unit and scripted tests only, using fake interactions.

**Voting and locking**
- [medium] `db.vote_toggle` does not validate the option key; anything that calls it with an arbitrary key writes a row. `save_options` drops votes whose key is not a current option.
- [medium] A locked segment's rows in `options` go stale. The lock copy is what the card shows, so anything that reads options for a locked segment must read the lock instead.
- [medium] `db.unlock_segment` leaves options in place and does not recompute. Callers must call `schedule_recompute` afterwards.
- [medium] The event status `done` is never set, and the UI has no way to unlock. Only the reschedule poll can unlock a segment.
- [low] A tie can only be broken by the organizer if they are still "in" that activity. (fixed 2026-10-25)
- [low] If participants change while a tie re-vote is pending, round 1 does not restart; it waits for the remaining voters on the tied options.
- [low] Vote buttons cover only the first 3 options. The engine caps options at 3 today, so a change there would hide the rest.
- [low] A four-activity card with every option line filled in can exceed the 1020-character field limit. The text is truncated, which can cut option lines.
- [low] Locked segments are not rechecked, so a venue closing after a lock goes unnoticed.
- [low] `PickButton` is a subclass of `VoteButton` with its own template. Registration was checked offline only.

**Conflicts and reschedule**
- [medium] Conflict detection maps names to people by display name, so two people with the same display name in one plan can be confused. (fixed 2026-10-25)
- [medium] A person is never polled twice for the same activity. If they press "I can make it" and are still blocked by the leading option, or the group picks "find a time for everyone" and nothing changes, no new vote opens. That follows the spec but can hide a real conflict.
- [low] A blocked organizer can break the tie on their own conflict poll.
- [low] An unbroken tie or an empty poll at the deadline falls back to a default: "find a time for everyone" for conflicts, "no" for reschedule.
- [low] `/reschedule` is accepted on an activity that is not locked; unlocking it does nothing.
- [low] Two different activities can each have an open conflict poll at once (the limit is one per activity).
- [low] If posting a conflict notice fails, the person stays tentatively out with a poll nobody can vote on until the deadline outcome is posted.
- [low] If the blocked person is the only one in an activity, no poll opens and they stay "in".
- [low] Conflict detection reads saved options even when the last recompute did not run (plan not ready). Harmless but wasteful.

## M4 review findings (independent reviewer, offline tests, 2026-10-25)

Each confirmed bug below has a strict-xfail test in `tests/test_e2e.py` (named `test_bug_...`). Fixing a bug makes its test flip to an unexpected pass, which fails the suite until the `xfail` marker is removed. That is deliberate.

**Confirmed bugs**
- [medium] A segment never locks after someone goes tentatively out. If everyone else has voted and the remaining person is marked tentatively out by a conflict poll, nothing re-checks the lock, so it stays unlocked until someone re-toggles a vote. (`test_bug_conflict_tentative_out_completes_vote`) (fixed 2026-10-25)
- [medium] Same stuck vote after a "go without them" result: `ui_polls.finalize` and `polls.finish` set the person to out but never re-check the lock. (`test_bug_without_outcome_completes_vote`) (fixed 2026-10-25)
- [medium] An organizer who toggled out of an activity cannot break a round-2 tie, because the tie-break button inherits the "must be a participant" check. The tie deadlocks. (`test_bug_organizer_out_cannot_break_tie`) (fixed 2026-10-25)
- [medium] `/edit` removing an activity that has an open reschedule poll crashes the poll rendering and finish code and leaves live buttons on its message. (`test_bug_edit_removing_segment_orphans_poll`) (fixed 2026-10-25)
- [medium] People are keyed by display name in `db.people_for_segment`, `blocked_by` and conflict detection. Two members named the same collapse into one and one person's availability is lost. (`test_bug_duplicate_display_names_collapse`) (fixed 2026-10-25)
- [medium] `ui_polls.finalize` and the "back" edit send names with no `allowed_mentions`, so a member named `@everyone` or `<@id>` can ping. (`test_bug_finalize_allowed_mentions`) (fixed 2026-10-25)
- [medium] The "Group needs" embed field is not capped at 1024 characters. A long area or diet value from the model makes Discord reject the card edit and the card stops updating. (`test_bug_group_needs_field_over_1024`) (fixed 2026-10-25)
- [low] Polls ignore a cancelled plan: voting and `request_reschedule` still work after `/cancel`. (`test_bug_poll_on_cancelled_event_still_open`) (fixed 2026-10-25)
- [low] With about 30 or more participants in an activity, the names list pushes the options and the "Provisional" label past the 1020-character field cut. (`test_bug_many_participants_truncate_options`) (fixed 2026-10-25)
- [low] `plan.parse_intent` does not validate activity names the way `parse_kinds` does. A name over about 72 characters makes the "I'm in: ..." label exceed Discord's 80-character limit and the kickoff fails. (`test_bug_intent_kind_unvalidated_breaks_card`) (fixed 2026-10-25)
- [low] Locking has no ordering check. If dinner is locked first, the recomputed golf option can overlap it. (`test_bug_golf_option_overlaps_locked_dinner`)

**Suspected, not reproduced**
- The recompute runs in a worker thread on the same SQLite connection as the event loop. A vote can see options half saved, and a commit on one thread can commit the other's partial work. Likely harmless; no isolation.
- `polls.resolve` closes a poll before `finalize` applies the outcome. A crash in between loses the outcome and nothing retries it.
- The electorate is a snapshot. Someone who leaves can still vote, a late joiner cannot, and a leaver still counts toward the majority threshold, so a poll can hang until its deadline.
- Votes from a person who toggles out and back in come back, since they are not deleted.
- A poll past its deadline but not yet swept tells people who are in the activity "Only people in this activity can vote".
- A conflict poll is never reopened for the same person and activity, even after "I can make it" cancelled the first one.
- The "I'm in" card button flips a tentatively out person back to "in" while their conflict poll is still open.
- `INTEGER PRIMARY KEY` without `AUTOINCREMENT` can reuse a deleted activity's id, so an orphan poll could attach to a new activity.
- Names and markdown such as `**x**` or `[a](url)` are not escaped in embeds (embeds do not ping).

**Not tested (needs live Discord)**
- The real `PlanBot._recompute` loop and its dirty-flag race.
- The 3-second response deadline with a live gateway (by reading, every callback does only small local queries first).
- Real `DynamicItem` dispatch after a restart.
- Real Discord rejection of oversized fields or labels (only checked against documented limits).
- `venue.check_hours` with a live LLM and network, and the real `plan.listen`, inside the M4 flows.
