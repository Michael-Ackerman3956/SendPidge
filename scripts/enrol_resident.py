"""Enrols or unenrols one resident on the deployed stack (founder only). enrol writes RECEIVER#<address>/PROFILE and the
resident fields on USER#<id>/PROFILE, marks the circle's contacts, then lists the address on the ResidentAddressList;
a resident without a PWA token gets one in .env.tokens.<userId> (git-ignored, owner-only), never printed. unenrol delists
the address first, then deletes the resident's rows and token.
Usage: AWS_PROFILE=sendpidge python scripts/enrol_resident.py enrol --address grandma@sendpidge.com --user-id grandma
           --name Grandma --org-id <orgId> --timezone <IANA name>
       AWS_PROFILE=sendpidge python scripts/enrol_resident.py unenrol --address grandma@sendpidge.com
"""

import argparse
import os
import sys
from pathlib import Path

import boto3

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT / "lambda"))

from shared import circle, graph, residents  # noqa: E402
from shared.residents import ResidentEnrolment  # noqa: E402

STACK_NAME = "SendPidge"
REGION_NAME = "us-east-1"
RESIDENT_ADDRESS_LIST_OUTPUT_NAME = "ResidentAddressListId"
TABLE_NAME_OUTPUT_NAME = "TableName"


def read_stack_outputs(session: boto3.Session) -> dict[str, str]:
    [stack] = session.client("cloudformation").describe_stacks(StackName=STACK_NAME)["Stacks"]
    return {output["OutputKey"]: output["OutputValue"] for output in stack["Outputs"]}


def save_access_token(user_id: str, access_token: str) -> Path:
    """Writes the token to .env.tokens.<userId>, created owner-only (0600) and re-chmodded if it already existed."""
    if not residents.USER_ID_PATTERN.fullmatch(user_id):
        raise ValueError("user id")
    tokens_file = REPOSITORY_ROOT / f".env.tokens.{user_id}"
    file_descriptor = os.open(tokens_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.chmod(tokens_file, 0o600)
    with os.fdopen(file_descriptor, "w") as tokens_stream:
        tokens_stream.write(f"PWA_TOKEN={access_token}\n")
    return tokens_file


def issue_token_when_missing(user_id: str) -> None:
    profile_response = graph.get_table().get_item(Key={"PK": f"USER#{user_id}", "SK": circle.PROFILE_SORT_KEY})
    if "tokenHash" in profile_response["Item"]:
        print(f"Kept the existing PWA token of {user_id}")
        return
    tokens_file = save_access_token(user_id, circle.issue_access_token(user_id))
    print(f"PWA token written to {tokens_file.name}")


def run_enrol_command(mailmanager_client, address_list_id: str, arguments: argparse.Namespace) -> None:
    resident = ResidentEnrolment(arguments.address, arguments.user_id, arguments.name, arguments.org_id, arguments.timezone)
    skipped_contact_user_ids = residents.enrol_resident(mailmanager_client, address_list_id, resident)
    print(f"Enrolled {arguments.user_id} and listed the address on {address_list_id}")
    if skipped_contact_user_ids:
        print(f"Contacts not marked (no PROFILE row, or another role): {', '.join(skipped_contact_user_ids)}")
    issue_token_when_missing(arguments.user_id)


def run_unenrol_command(mailmanager_client, address_list_id: str, arguments: argparse.Namespace) -> None:
    residents.unenrol_resident(mailmanager_client, address_list_id, arguments.address)
    print(f"Delisted the address from {address_list_id} and deleted the resident's rows and token")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enrol or unenrol one resident.")
    subcommands = parser.add_subparsers(required=True)
    enrol_parser = subcommands.add_parser("enrol")
    for option in ("--address", "--user-id", "--name", "--org-id", "--timezone"):
        enrol_parser.add_argument(option, required=True)
    enrol_parser.set_defaults(run_command=run_enrol_command)
    unenrol_parser = subcommands.add_parser("unenrol")
    unenrol_parser.add_argument("--address", required=True)
    unenrol_parser.set_defaults(run_command=run_unenrol_command)
    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()
    session = boto3.Session(region_name=REGION_NAME)
    stack_outputs = read_stack_outputs(session)
    # shared.graph builds its table from the environment: the deployed table, in the stack's region.
    os.environ["TABLE_NAME"] = stack_outputs[TABLE_NAME_OUTPUT_NAME]
    os.environ["AWS_DEFAULT_REGION"] = REGION_NAME
    arguments.run_command(session.client("mailmanager"), stack_outputs[RESIDENT_ADDRESS_LIST_OUTPUT_NAME], arguments)


if __name__ == "__main__":
    main()
