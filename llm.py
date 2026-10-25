"""Thin LLM interface. All model calls go through complete(); provider swaps via env."""
import logging
import os

import boto3

log = logging.getLogger("llm")
_client = None


def complete(prompt: str, system: str = "", max_tokens: int = 512) -> str:
    global _client
    if os.environ.get("LLM_PROVIDER", "bedrock") != "bedrock":
        raise NotImplementedError("only bedrock supported")
    if _client is None:
        # AWS_PROFILE is picked up by boto3 locally; on EC2 the instance role is used.
        _client = boto3.client("bedrock-runtime", region_name=os.environ["AWS_REGION"])
    kwargs = {"system": [{"text": system}]} if system else {}
    r = _client.converse(
        modelId=os.environ["LLM_MODEL"],
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": max_tokens},
        **kwargs,
    )
    log.info("tokens %s", r["usage"])
    return r["output"]["message"]["content"][0]["text"]
