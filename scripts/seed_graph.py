"""Writes the demo trust graph (Grandma → Jimin) and fresh PWA access tokens, then marks the circle's contacts.

The resident (cdk.json mailboxResidentAddress) must be enrolled first with scripts/enrol_resident.py: her user id is read
from her RECEIVER#<address>/PROFILE row, and the script stops when that row is missing.
Tokens go to .env.tokens (git-ignored, owner-only) and are never printed. Re-running keeps them unless
--rotate-tokens is passed. JIMIN_CONTACT_EMAIL (optional) is where Jimin's verdict receipts go.
JIMIN_KNOWN_ADDRESSES (comma-separated) is the list gate 1 trusts: a "Jimin" email from any other address is held.
Usage: AWS_PROFILE=sendpidge [JIMIN_CONTACT_EMAIL=...] [JIMIN_KNOWN_ADDRESSES=a@x.com,b@y.com] python scripts/seed_graph.py [--rotate-tokens]
"""

import json
import os
import sys
from pathlib import Path

import boto3

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT / "lambda"))

from shared import circle, profiles, residents  # noqa: E402
from shared.graph import METADATA_SORT_KEY  # noqa: E402

TABLE_NAME = "sendpidge"
TOKENS_FILE = REPOSITORY_ROOT / ".env.tokens"
CDK_CONTEXT = json.loads((REPOSITORY_ROOT / "infra" / "cdk.json").read_text())["context"]
RECEIVER_ADDRESS = CDK_CONTEXT["mailboxResidentAddress"]
CONTACT_USER_ID = "jimin"
CONTACT_NAME = "Jimin"
DEFAULT_KNOWN_ADDRESSES = ["jimin@example.com"]  # the address samples/family-hello.eml is sent from


def known_addresses_from_environment() -> list[str]:
    configured = os.environ.get("JIMIN_KNOWN_ADDRESSES", "")
    addresses = [address.strip().casefold() for address in configured.split(",") if address.strip()]
    return addresses or DEFAULT_KNOWN_ADDRESSES


def rotate_access_token(table, user_id: str) -> str:
    """Deletes the user's previous token row, then writes a new one and sets its hash on USER#/PROFILE in place."""
    previous_profile = table.get_item(Key={"PK": f"USER#{user_id}", "SK": circle.PROFILE_SORT_KEY}).get("Item")
    if previous_profile is not None and "tokenHash" in previous_profile:  # an enrolled resident may have no token yet
        table.delete_item(Key={"PK": f"TOKEN#{previous_profile['tokenHash']}", "SK": METADATA_SORT_KEY})
    return circle.issue_access_token(user_id)


def write_contact(table) -> None:
    contact_row = {
        "PK": f"RECEIVER#{RECEIVER_ADDRESS}",
        "SK": f"CONTACT#{CONTACT_USER_ID}",
        "contactName": CONTACT_NAME,
        "contactPwaUserId": CONTACT_USER_ID,
        "knownAddresses": known_addresses_from_environment(),
        "verifiedVia": "seed",
    }
    contact_email = os.environ.get("JIMIN_CONTACT_EMAIL")
    if contact_email:
        contact_row["contactEmail"] = contact_email
    contact_rcs = os.environ.get("JIMIN_RCS_NUMBER")
    if contact_rcs:
        contact_row["contactRcs"] = contact_rcs  # E.164 (+8210…), exactly as RCS reports originationNumber
    contact_whatsapp = os.environ.get("JIMIN_WHATSAPP_NUMBER")
    if contact_whatsapp:
        contact_row["contactWhatsApp"] = contact_whatsapp  # E.164 (+8210…); the parser adds the "+" Meta omits
    table.put_item(Item=contact_row)


def save_tokens(tokens_by_variable: dict[str, str]) -> None:
    TOKENS_FILE.write_text("".join(f"{name}={value}\n" for name, value in tokens_by_variable.items()))
    os.chmod(TOKENS_FILE, 0o600)


def main() -> None:
    os.environ.setdefault("TABLE_NAME", TABLE_NAME)  # shared.graph reads the table name from the environment
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    receiver_user_id = profiles.require_receiver_profile(RECEIVER_ADDRESS).receiver_user_id
    table = boto3.Session(region_name="us-east-1").resource("dynamodb").Table(TABLE_NAME)
    write_contact(table)
    if TOKENS_FILE.exists() and "--rotate-tokens" not in sys.argv:
        tokens_outcome = f"kept the tokens in {TOKENS_FILE.name}"
    else:
        save_tokens({
            "GRANDMA_PWA_TOKEN": rotate_access_token(table, receiver_user_id),
            "JIMIN_PWA_TOKEN": rotate_access_token(table, CONTACT_USER_ID),
        })
        tokens_outcome = f"tokens written to {TOKENS_FILE.name}"
    skipped_contact_user_ids = residents.mark_circle_contacts(RECEIVER_ADDRESS)
    print(f"Seeded {RECEIVER_ADDRESS} → {CONTACT_NAME}; {tokens_outcome}")
    if skipped_contact_user_ids:
        print(f"Contacts not marked (no PROFILE row, or another role): {', '.join(skipped_contact_user_ids)}")


if __name__ == "__main__":
    main()
