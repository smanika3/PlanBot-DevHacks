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
