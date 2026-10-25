"""M0 smoke test: env vars set, Bedrock Converse call works. Prints no secrets."""
import os
import sys

import boto3

REQUIRED = ["DISCORD_TOKEN", "DISCORD_APP_ID", "DISCORD_GUILD_ID", "DISCORD_CHANNEL_ID", "AWS_REGION", "LLM_MODEL"]


def load_env(path=".env"):
    for line in open(path):
        k, _, v = line.strip().partition("=")
        if k and not k.startswith("#"):
            os.environ.setdefault(k, v)


def main():
    load_env()
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    print("env:", "ok" if not missing else f"EMPTY: {missing}")
    if "LLM_MODEL" in missing:
        sys.exit("set LLM_MODEL first")
    br = boto3.Session(profile_name=os.environ.get("AWS_PROFILE", "planbot"), region_name=os.environ["AWS_REGION"]).client("bedrock-runtime")
    r = br.converse(
        modelId=os.environ["LLM_MODEL"],
        messages=[{"role": "user", "content": [{"text": "Reply with the single word: pong"}]}],
        inferenceConfig={"maxTokens": 20},
    )
    print("bedrock:", r["output"]["message"]["content"][0]["text"].strip(), r["usage"])


if __name__ == "__main__":
    main()
