"""Links or unlinks one PWA user to a Cognito user for passkey sign-in on the deployed stack (founder only). link creates the
Cognito user (email verified; Cognito emails the temporary password) and, in one transaction, writes SUB#<sub>/META {userId}
and cognitoSub on USER#<id>/PROFILE; if that transaction fails, the new Cognito user is deleted again. After link, that
user's old ?token= link stops working: they sign in, set a password, then add a passkey. unlink removes cognitoSub and the
SUB# row in one transaction, then deletes the Cognito user. Prints only the user id, never the email, sub or a password.
Usage: AWS_PROFILE=sendpidge python scripts/seed_auth_users.py link --user-id grandma --email <address>
       AWS_PROFILE=sendpidge python scripts/seed_auth_users.py unlink --user-id grandma
"""

import argparse
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT / "lambda"))

from enrol_resident import REGION_NAME, TABLE_NAME_OUTPUT_NAME, read_stack_outputs  # noqa: E402
from shared import circle, graph, profiles, residents  # noqa: E402
from shared.cognito_auth import COGNITO_SUB_FIELD, SUB_PARTITION_PREFIX  # noqa: E402

USER_POOL_ID_OUTPUT_NAME = "UserPoolId"


def require_user_id(user_id: str) -> str:
    if not residents.USER_ID_PATTERN.fullmatch(user_id):
        raise ValueError("user id")
    return user_id


def create_cognito_user(cognito_client, user_pool_id: str, email_address: str) -> str:
    """Creates the user with a verified email (the pool signs in by email); Cognito emails the temporary password. Returns
    the new user's sub."""
    response = cognito_client.admin_create_user(
        UserPoolId=user_pool_id, Username=email_address, DesiredDeliveryMediums=["EMAIL"],
        UserAttributes=[{"Name": "email", "Value": email_address}, {"Name": "email_verified", "Value": "true"}],
    )
    attributes = {attribute["Name"]: attribute["Value"] for attribute in response["User"]["Attributes"]}
    return attributes["sub"]


def write_sub_link(user_id: str, sub: str) -> None:
    """In one transaction: SUB#<sub>/META {userId} (new) and cognitoSub on USER#<id>/PROFILE (the row exists and is not linked
    yet). Raises ValueError when either condition fails."""
    table = graph.get_table()
    transaction_items = [
        {"Put": {"TableName": table.name,
                 "Item": {"PK": f"{SUB_PARTITION_PREFIX}{sub}", "SK": graph.METADATA_SORT_KEY, "userId": user_id},
                 "ConditionExpression": "attribute_not_exists(PK)"}},
        {"Update": {"TableName": table.name, "Key": {"PK": f"USER#{user_id}", "SK": circle.PROFILE_SORT_KEY},
                    "UpdateExpression": "SET #cognitoSub = :sub",
                    "ConditionExpression": "attribute_exists(PK) AND attribute_not_exists(#cognitoSub)",
                    "ExpressionAttributeNames": {"#cognitoSub": COGNITO_SUB_FIELD},
                    "ExpressionAttributeValues": {":sub": sub}}},
    ]
    try:
        table.meta.client.transact_write_items(TransactItems=transaction_items)
    except ClientError as error:
        if error.response["Error"]["Code"] == circle.TRANSACTION_CANCELLED:
            raise ValueError("the user has no PROFILE row, is already linked, or the sub is already mapped") from None
        raise


def remove_sub_link(user_id: str) -> str:
    """In one transaction: removes cognitoSub from USER#<id>/PROFILE and deletes its SUB#<sub>/META row. Returns the sub.
    Raises ValueError when the user is not linked or the rows changed underneath."""
    profile_row = profiles.read_profile_row(f"USER#{user_id}")
    if profile_row is None or COGNITO_SUB_FIELD not in profile_row:
        raise ValueError("the user is not linked to a Cognito user")
    sub = profile_row[COGNITO_SUB_FIELD]
    table = graph.get_table()
    transaction_items = [
        {"Update": {"TableName": table.name, "Key": {"PK": f"USER#{user_id}", "SK": circle.PROFILE_SORT_KEY},
                    "UpdateExpression": "REMOVE #cognitoSub", "ConditionExpression": "#cognitoSub = :sub",
                    "ExpressionAttributeNames": {"#cognitoSub": COGNITO_SUB_FIELD}, "ExpressionAttributeValues": {":sub": sub}}},
        {"Delete": {"TableName": table.name, "Key": {"PK": f"{SUB_PARTITION_PREFIX}{sub}", "SK": graph.METADATA_SORT_KEY},
                    "ConditionExpression": "userId = :userId", "ExpressionAttributeValues": {":userId": user_id}}},
    ]
    try:
        table.meta.client.transact_write_items(TransactItems=transaction_items)
    except ClientError as error:
        if error.response["Error"]["Code"] == circle.TRANSACTION_CANCELLED:
            raise ValueError("the PROFILE and SUB# rows no longer match; nothing was changed") from None
        raise
    return sub


def run_link_command(cognito_client, user_pool_id: str, arguments: argparse.Namespace) -> None:
    user_id = require_user_id(arguments.user_id)
    email_address = circle.parse_email_address(arguments.email)
    sub = create_cognito_user(cognito_client, user_pool_id, email_address)
    try:
        write_sub_link(user_id, sub)
    except Exception:
        cognito_client.admin_delete_user(UserPoolId=user_pool_id, Username=sub)  # no Cognito user without its SUB# row
        raise
    print(f"Linked {user_id}; Cognito emailed the temporary password")


def run_unlink_command(cognito_client, user_pool_id: str, arguments: argparse.Namespace) -> None:
    user_id = require_user_id(arguments.user_id)
    sub = remove_sub_link(user_id)
    cognito_client.admin_delete_user(UserPoolId=user_pool_id, Username=sub)
    print(f"Unlinked {user_id} and deleted the Cognito user")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Link or unlink one PWA user to a Cognito user.")
    subcommands = parser.add_subparsers(required=True)
    link_parser = subcommands.add_parser("link")
    for option in ("--user-id", "--email"):
        link_parser.add_argument(option, required=True)
    link_parser.set_defaults(run_command=run_link_command)
    unlink_parser = subcommands.add_parser("unlink")
    unlink_parser.add_argument("--user-id", required=True)
    unlink_parser.set_defaults(run_command=run_unlink_command)
    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()
    session = boto3.Session(region_name=REGION_NAME)
    stack_outputs = read_stack_outputs(session)
    if USER_POOL_ID_OUTPUT_NAME not in stack_outputs:
        raise SystemExit("The stack has no user pool: set cognitoDomainPrefix in infra/cdk.json and deploy first")
    # shared.graph builds its table from the environment: the deployed table, in the stack's region.
    os.environ["TABLE_NAME"] = stack_outputs[TABLE_NAME_OUTPUT_NAME]
    os.environ["AWS_DEFAULT_REGION"] = REGION_NAME
    arguments.run_command(session.client("cognito-idp"), stack_outputs[USER_POOL_ID_OUTPUT_NAME], arguments)


if __name__ == "__main__":
    main()
