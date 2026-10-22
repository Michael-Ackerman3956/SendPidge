"""Writes the demo trust graph (Grandma → Jimin) and fresh PWA access tokens.

Tokens go to .env.tokens (git-ignored, owner-only) and are never printed. Re-running keeps them unless
--rotate-tokens is passed. JIMIN_CONTACT_EMAIL (optional) is where Jimin's verdict receipts go.
Usage: AWS_PROFILE=sendpidge [JIMIN_CONTACT_EMAIL=...] python scripts/seed_graph.py [--rotate-tokens]
"""

import json
import os
import secrets
import sys
from pathlib import Path

import boto3

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT / "lambda"))

from shared.graph import METADATA_SORT_KEY, hash_access_token  # noqa: E402

TABLE_NAME = "sendpidge"
TOKENS_FILE = REPOSITORY_ROOT / ".env.tokens"
CDK_CONTEXT = json.loads((REPOSITORY_ROOT / "infra" / "cdk.json").read_text())["context"]
RECEIVER_ADDRESS = CDK_CONTEXT["receiverAddress"]
RECEIVER_USER_ID = CDK_CONTEXT["receiverUserId"]
CONTACT_USER_ID = "jimin"
CONTACT_NAME = "Jimin"
PROFILE_SORT_KEY = "PROFILE"


def rotate_access_token(table, user_id: str) -> str:
    """Deletes the user's previous token row, writes a new one, returns the new token."""
    profile_key = {"PK": f"USER#{user_id}", "SK": PROFILE_SORT_KEY}
    previous_profile = table.get_item(Key=profile_key).get("Item")
    if previous_profile:
        table.delete_item(Key={"PK": f"TOKEN#{previous_profile['tokenHash']}", "SK": METADATA_SORT_KEY})
    access_token = secrets.token_urlsafe(32)
    token_hash = hash_access_token(access_token)
    table.put_item(Item={"PK": f"TOKEN#{token_hash}", "SK": METADATA_SORT_KEY, "userId": user_id})
    table.put_item(Item={**profile_key, "tokenHash": token_hash})
    return access_token


def write_contact(table) -> None:
    contact_row = {
        "PK": f"RECEIVER#{RECEIVER_ADDRESS}",
        "SK": f"CONTACT#{CONTACT_USER_ID}",
        "contactName": CONTACT_NAME,
        "contactPwaUserId": CONTACT_USER_ID,
        "verifiedVia": "seed",
    }
    contact_email = os.environ.get("JIMIN_CONTACT_EMAIL")
    if contact_email:
        contact_row["contactEmail"] = contact_email
    table.put_item(Item=contact_row)


def save_tokens(tokens_by_variable: dict[str, str]) -> None:
    TOKENS_FILE.write_text("".join(f"{name}={value}\n" for name, value in tokens_by_variable.items()))
    os.chmod(TOKENS_FILE, 0o600)


def main() -> None:
    table = boto3.Session(region_name="us-east-1").resource("dynamodb").Table(TABLE_NAME)
    write_contact(table)
    if TOKENS_FILE.exists() and "--rotate-tokens" not in sys.argv:
        print(f"Seeded {RECEIVER_ADDRESS} → {CONTACT_NAME}; kept the tokens in {TOKENS_FILE.name}")
        return
    save_tokens({
        "GRANDMA_PWA_TOKEN": rotate_access_token(table, RECEIVER_USER_ID),
        "JIMIN_PWA_TOKEN": rotate_access_token(table, CONTACT_USER_ID),
    })
    print(f"Seeded {RECEIVER_ADDRESS} → {CONTACT_NAME}; tokens written to {TOKENS_FILE.name}")


if __name__ == "__main__":
    main()
