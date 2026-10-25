# PlanBot demo flow

A scene-by-scene script of the PlanBot demo, written so a person or a video tool can reproduce it exactly. Everything below describes what the bot actually does today. Where wording is approximate it says so.

## The pitch (30 seconds)

Group plans die in "idk, whenever" threads. PlanBot sits in a friend group's Discord channel and turns a day of chatter into one plan nobody had to organize: it spots a plan, collects availability, diet, budget and area from normal chat, checks real venues and their opening hours, shows two or three options, lets the group vote, locks the plan, and gets one person to book. It never books or pays for anyone.

Hackathon fit (Cubbie Track 3, group chat assistant). Point at these three moments in the video:
1. **Conflicting constraints.** Someone's availability clashes with the leading time, so the bot marks them tentatively out and the group decides (scene 6). A hard diet narrows dinner to the only venue whose own site supports it (scene 4).
2. **What agents share.** The card shows aggregate needs only ("Dietary: vegetarian"), never who has them. Conflict notices say someone cannot make it, never why (scenes 4 and 6).
3. **When the bot asks people to confirm.** It echoes every parse, asks the group before going without someone, and asks a human to confirm a booking (scenes 3, 6, 8).

## Cast and setup

- A private Discord server with one text channel (the bot only listens in the configured channel).
- Four dummy accounts. **A** is the organizer (starts the plan). **B**, **C**, **D** are friends.
- Bot display name: PlanBot (the account shows as `nbot`). Slash commands: `/help`, `/status`, `/edit`, `/cancel`, `/reschedule`.
- Demo area is Tempe, Arizona, time zone America/Phoenix. Dates in the bot are relative to the day you film.
- Start from a clean channel: if an old plan card exists, A runs `/cancel` first.
- Do not show the terminal, `.env`, or the AWS console while a token or ID could be visible.

## How the bot is built (for the narrator)

One Discord bot, one visible voice (the facilitator). Behind it, separate parts that never talk to each other and only read and write one shared plan state:

| Part | Job | Uses an LLM? |
| --- | --- | --- |
| Listener | Reads chat and DMs, extracts availability, diet, budget, area, in/out, and echoes back what it understood | Yes (Amazon Bedrock, Claude Haiku) |
| Preferences | Combines everyone's needs into anonymous group needs | No |
| Schedule | Ranks time slots, finds who is blocked, shifts dinner after golf | No (plain, unit-tested code) |
| Venue | Filters a researched Tempe venue list and checks opening hours live from the venue's own page | Only to read hours from page text |
| Facilitator | Edits the one plan card, opens votes and polls, locks, hands out booking | Wording only |

Message flow: chat message → Listener → plan state → Preferences, then Schedule and Venue → card edits itself.

## Scenes

Each scene lists what to do, what appears on screen, and a narration line.

### Scene 1. Kickoff
- **A types:** `free this weekend, wanna golf? then dinner after`
- **Screen:** the bot posts a card and pins it. Title "Plan: golf → dinner", "Proposed by @A". Two fields, "1. Golf" and "2. Dinner", each listing A. Two buttons: "I'm in: golf" and "I'm in: dinner". Footer: "Planning starts once one more person is in".
- **Narration:** "One casual message and PlanBot turns it into a plan with two activities. The organizer is in automatically."

### Scene 2. Friends join, per activity
- **B taps** both "I'm in" buttons.
- **Screen:** B appears under golf and dinner. Footer becomes "Planning started · 0 of 2 shared times".
- **Narration:** "Everyone chooses per activity. Planning starts once the organizer plus one more person is in."

### Scene 3. Availability in plain chat, with echo-back
- **A types:** `I'm free Saturday 8am to 8pm, around Tempe preferably, budget under $$$`
- **B types:** `free Saturday 9am to 6pm`
- **Screen:** after each message the bot replies with what it understood, for example "Understood: free sat 8:00am–8:00pm · budget $$$ · area Tempe (preferably). Reply to correct me." A "Group needs" box appears on the card with the budget and "Area: Tempe (preferred)".
- **Then:** an "Agents" box shows live status for Preferences, Schedule and Venue, such as "Venue: checking opening hours…". After roughly 10 to 20 seconds, two or three options appear under each activity, each like: day and time, venue name with a source link, and either "hours confirmed <time>" or "⚠️ hours unverified", plus "everyone who replied is free". Footer: "Provisional · 2 of 2 replied". Each activity says "Provisional, not locked".
- **Narration:** "No calendar access. People just type. The bot repeats back what it understood so nobody is misread. Then separate agents rank times and check real venues. Hours it can confirm are labelled with when it checked. Hours it cannot confirm say unverified. It never makes hours up."

### Scene 4. Private diet, shared only as an aggregate
- **B sends the bot a DM:** `I'm vegetarian and it has to be vegetarian friendly`
- **Screen:** the reply appears in the DM only ("Noted privately: diet vegetarian (must)…"). Back in the channel, the card's "Group needs" now includes "Dietary: vegetarian" with no name. Dinner options narrow to Picazzo's Healthy Italian Kitchen, the only venue in the list whose own website states vegetarian options.
- **Narration:** "Diet can be sent privately. The group only ever sees an anonymous need. And because it is a hard requirement, dinner narrows to the one venue that confirms it, instead of guessing."

### Scene 5. One person skips an activity
- **C types:** `can't do golf but I'm there for dinner, free Saturday 5pm to 10pm`
- **Screen:** C appears under Dinner only. Golf stays without C.
- **Narration:** "People can join some activities and skip others. Golf is planned without C. Dinner includes C."

### Scene 6. Conflict: tentatively out, then the group decides
- **D taps** "I'm in: golf", then **types:** `busy Saturday morning until 1pm`
- **Screen:** a notice from the bot: D "may not make golf at <time> and is tentatively out for now. Go without them, or find a time for everyone?" with buttons "Go without them" and "Find a time for everyone". D also gets an "I can make it" button that only D can press. The notice shows only "X of N voted" while open, never who voted what.
- **A and B tap** "Find a time for everyone".
- **Screen:** a follow-up with "Best times for golf:" and bullet lines such as "Sat 1:00pm: 3 of 4 free", and the knock-on effect on later activities, for example "dinner would move to …". It names who is still blocked and asks them to share more times. Exact wording of the follow-up is approximate.
- **Narration:** "When someone's time clashes, PlanBot does not guess and does not nag. It marks them tentatively out, asks the group whether to go without them or find a time that works for everyone, and shows what moving golf does to dinner. It never says why someone is busy."

### Scene 7. Vote and lock
- **Under the card** each open activity has up to three vote buttons, such as "Golf A", "Golf B", "Golf C". People tap every option they could live with (approval voting). Tapping again removes the approval.
- **Screen:** each activity shows "N of M voted (approve every option you can do)" and a count per option, but never who voted for what.
- **Everyone in golf votes, then everyone in dinner.** When the last person in an activity has voted, that activity shows "🔒 Locked:" with the winning time, venue, source link and hours status, and its vote buttons disappear. Dinner is then scheduled after the locked golf ends plus travel time.
- **If there is a tie:** "Tie between A, C: re-vote on those", and if it ties again, "Still tied: the organizer picks" with organizer-only buttons.
- **Narration:** "The plan locks per activity once everyone in it has voted. Ties get one re-vote, then the organizer decides."

### Scene 8. Booking, without the bot booking anything
- **Screen:** after a lock, the bot posts "Who'll book **golf**? I won't book or pay for anything, I just need a volunteer." with an "I'll book it" button.
- **B taps** "I'll book it". A second person tapping gets a private "B already volunteered".
- **B gets a DM** with the venue, link, address, time, party size and an anonymous dietary note, plus "I will not book or pay for anything. Please book it yourself, then tap 'I booked it'." Buttons: "I booked it" and "Hand back".
- **B taps** "I booked it". The channel posts "Golf booked by B" and the card shows "✅ Booked by B".
- **Narration:** "PlanBot never books or pays. It finds one volunteer, hands them everything they need, and tells the group once it is done."

### Scene 9. Reschedule
- **A runs** `/reschedule golf`.
- **Screen:** a poll, "A asked to reschedule **golf**. Move it?" with buttons "Yes, reschedule" and "No, keep it", and a note that dinner reopens too. A majority of the activity's participants passes it. On a pass: "Reschedule passed: golf is open again, and dinner reopens with it."
- **Narration:** "Plans change. Anyone in an activity can ask to move it. A majority decides, and later activities reopen because they depended on it."

### Scene 10. Wrap up
- **A runs** `/cancel` to cancel the plan (organizer only; the card turns grey and unpins), or leave it locked for the closing shot.
- **Closing line:** "From a one-line message to a locked, booked plan, without anyone leaving Discord."

## Quick reference: slash commands
- `/help`: what the bot does (only you see it).
- `/status`: link to the current plan card.
- `/edit`: organizer changes the activities (opens a small form).
- `/cancel`: organizer cancels the plan.
- `/reschedule [activity]`: ask the group to move an activity.

## Honest limits to keep in the video
- Opening hours are read live from each venue's page. Many pages do not state hours, so expect several options to say "⚠️ hours unverified". That is the bot refusing to guess.
- The venue list has 10 real places in and around Tempe, each with a source link and the date it was checked. Only one has diet tags, because only its own site states them.
- There is no distance filter. Every venue counts as "near Tempe".
- One open plan per channel. Times are in one time zone.

## Do not show
- The `.env` file, the Discord token, AWS credentials, the AWS console account id, or the server logs while recording.
- Real people's data. Use the dummy accounts only.
