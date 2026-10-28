"""Seeds one org on the deployed stack (founder only): ORG#<orgId>/PROFILE, an ADMIN# row per admin (role org_admin and the
orgId set on their USER#<id>/PROFILE) and the tags on each member's MEMBER# row, in one transaction
(orgs.write_org_seed); each member must already be a resident enrolled in this org. Re-running it rewrites the profile, the
listed admins and the tags, deletes the ADMIN# row of every admin of this org it does not list (in the same transaction;
their USER#/PROFILE keeps its role and orgId, and require_org_admin refuses them) and keeps everything else. An admin
without a PWA token gets one in .env.tokens.<userId> (git-ignored, owner-only), never printed; the org's own org-<orgId>
user never gets one.
Usage: AWS_PROFILE=sendpidge python scripts/seed_org.py --org-id home-demo --org-name "<home name>"
           --verified-address office@<home domain> [--verified-address ...] [--name-variant "<other name>" ...]
           --timezone <IANA name> --admin <userId>:<viewer|sender> [--admin ...] [--member <address>[:<tag>,<tag>] ...]
"""

import argparse
import os
import sys
from pathlib import Path

import boto3

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT / "lambda"))

from enrol_resident import REGION_NAME, TABLE_NAME_OUTPUT_NAME, issue_token_when_missing, read_stack_outputs  # noqa: E402
from shared import orgs  # noqa: E402
from shared.org_enums import AdminRole  # noqa: E402
from shared.orgs import AdminSeed, MemberSeed, OrgSeed  # noqa: E402

ADMIN_SEPARATOR = ":"
MEMBER_TAGS_SEPARATOR = ":"
TAG_SEPARATOR = ","


def parse_admin_argument(admin_text: str) -> AdminSeed:
    """USER_ID:ROLE with ROLE an AdminRole value; the user id itself is checked by orgs.validate_org_seed."""
    user_id, separator, role_text = admin_text.partition(ADMIN_SEPARATOR)
    if not separator:
        raise argparse.ArgumentTypeError("expected USER_ID:ROLE")
    try:
        return AdminSeed(user_id, AdminRole(role_text))
    except ValueError:
        raise argparse.ArgumentTypeError(f"role must be one of {', '.join(AdminRole)}") from None


def parse_member_argument(member_text: str) -> MemberSeed:
    """ADDRESS or ADDRESS:TAG,TAG; the address and each tag are checked (an empty tag refused) by orgs.validate_org_seed."""
    receiver_address, separator, tags_text = member_text.partition(MEMBER_TAGS_SEPARATOR)
    return MemberSeed(receiver_address, tuple(tags_text.split(TAG_SEPARATOR)) if separator else ())


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed one org: its profile, admins and member tags.")
    for option in ("--org-id", "--org-name", "--timezone"):
        parser.add_argument(option, required=True)
    parser.add_argument("--verified-address", action="append", required=True, dest="verified_addresses")
    parser.add_argument("--name-variant", action="append", default=[], dest="name_variants")
    parser.add_argument("--admin", action="append", required=True, type=parse_admin_argument, dest="admins")
    parser.add_argument("--member", action="append", default=[], type=parse_member_argument, dest="members")
    return parser.parse_args()


def build_org_seed(arguments: argparse.Namespace) -> OrgSeed:
    return OrgSeed(arguments.org_id, arguments.org_name, tuple(arguments.verified_addresses), tuple(arguments.name_variants),
                   arguments.timezone, tuple(arguments.admins), tuple(arguments.members))


def main() -> None:
    arguments = parse_arguments()
    session = boto3.Session(region_name=REGION_NAME)
    stack_outputs = read_stack_outputs(session)
    # shared.graph builds its table from the environment: the deployed table, in the stack's region.
    os.environ["TABLE_NAME"] = stack_outputs[TABLE_NAME_OUTPUT_NAME]
    os.environ["AWS_DEFAULT_REGION"] = REGION_NAME
    org_seed = orgs.validate_org_seed(build_org_seed(arguments))
    orgs.write_org_seed(org_seed)
    print(f"Seeded org {org_seed.org_id}: {len(org_seed.admins)} admin(s), {len(org_seed.members)} member(s)")
    for admin_seed in org_seed.admins:
        issue_token_when_missing(admin_seed.user_id)


if __name__ == "__main__":
    main()
