# PlanBot

Discord bot that turns "wanna golf this weekend?" into a locked group plan. Cubbie Hackathon, Track 3. Work in progress (M0).

## Run locally
    python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
    cp .env.example .env   # fill in values, never commit .env
    AWS_PROFILE=<profile> .venv/bin/python bot.py

Try `!ping` and `!llm` in the configured channel.

## Env vars
See `.env.example`. Defaults: `LLM_PROVIDER=bedrock`, `TZ_NAME=America/Phoenix`, `DEMO_AREA=Tempe`.
Locally Bedrock uses your AWS profile; on EC2 it uses the instance role (no keys on the box).

Known bugs and things to recheck: see [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

## How planning works (M3)
- **Preferences** (`db.constraints`): aggregates diet, budget and area across people in the plan. Only anonymous needs leave it.
- **Schedule** (`schedule.py`): deterministic. Slots for the next 7 days, ranked by fewest people blocked. Dinner starts after golf ends plus 30 min travel. Shows who is blocked, never why.
- **Venue** (`venue.py`): filters `data/venues.json` (hard diet, budget cap, soft diet ranking), then fetches the venue's page and asks the model for that weekday's hours. Every option shows its source and "checked at", or `unverified`. Fetched pages are treated as untrusted data; the model output is reduced to two `HH:MM` values.
- **Facilitator** (`bot.py`, `engine.py`): the only voice. Once two people have shared availability it shows 2 or 3 provisional options, with live status lines per role.
- `vote.py`: approval-voting tally (buttons come in M4).

Limits: times are naive local times in one time zone (`America/Phoenix`). The venue seed list has 10 venues, each with a `source` and `checked` date; diet tags are only set where the venue's own site states them. Edit `data/venues.json` to add more.

## Voting, conflicts, booking (M4, M5)
- Vote buttons under the card (approval voting). An activity locks when everyone in it has voted; ties re-vote once, then the organizer picks.
- A person blocked by the leading option goes tentatively out and the group votes: go without them, or find a time for everyone. `/reschedule` opens a yes/no poll (majority passes, organizer breaks ties) and reopens the activity.
- After a lock, a "Who'll book?" button appears. The volunteer is DMed the details (aggregated dietary note, no names) and confirms; the group sees "Booked by X". The bot never books or pays.
- Slash commands: `/help`, `/status`, `/edit`, `/cancel` (organizer), `/reschedule`.

## Deploy (AWS, one small EC2 box)
1. Stop any local copy of the bot (two copies with one token would both answer).
2. In AWS CloudShell (region us-east-1), run `deploy/ec2-setup.sh`. It prompts for the Discord ids and token (hidden), stores them as one encrypted SSM parameter, creates a role that can only invoke Bedrock and read that parameter, a security group with no inbound rules, and one `t3.small` that builds and runs the Docker image from this repo.
3. Watch it with Session Manager: `aws ssm start-session --target <instance id>` then `sudo docker logs -f planbot`.
4. Remove everything with `deploy/ec2-teardown.sh`.

No AWS keys live on the box (instance role), no SSH keys exist, and the token is never in the repo or the instance user-data. State is SQLite in `/opt/planbot/data` on the instance disk.
