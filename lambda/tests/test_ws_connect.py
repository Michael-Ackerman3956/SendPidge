"""$connect through handler.handle: a Cognito ID token connects as the userId on its SUB# row (never a claim), a JWT that
fails its check is never retried as an opaque token, and a user linked to Cognito can no longer connect with their old link.
Users are linked by the founder script's own transaction (scripts/seed_auth_users.py write_sub_link)."""

import json
import sys
from pathlib import Path

from shared import circle, graph
from tests.test_cognito_auth import TEST_SUB, build_claims, cognito_environment, encode_segment, sign_token  # noqa: F401
from tests.test_verdict import aws  # noqa: F401
from ws_connect import handler

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from seed_auth_users import write_sub_link  # noqa: E402

CONNECTION_ID = "connection-1"
LINKED_USER_ID = "grandma"
UNLINKED_USER_ID = "jimin"


def connect_with_token(token: str) -> dict:
    event = {"requestContext": {"routeKey": "$connect", "connectionId": CONNECTION_ID}, "queryStringParameters": {"token": token}}
    return handler.handle(event, None)


def read_connection_row() -> dict | None:
    response = graph.get_table().get_item(Key={"PK": f"CONN#{CONNECTION_ID}", "SK": graph.METADATA_SORT_KEY})
    return response["Item"] if "Item" in response else None


def link_user_to_test_sub(user_id: str) -> str:
    """Issues the user's opaque token (which writes their PROFILE row), then links them to TEST_SUB; returns the token."""
    opaque_token = circle.issue_access_token(user_id)
    write_sub_link(user_id, TEST_SUB)
    return opaque_token


def test_mapped_id_token_connects_as_the_sub_row_user(aws, cognito_environment):  # noqa: F811
    link_user_to_test_sub(LINKED_USER_ID)
    token = sign_token(build_claims(userId=UNLINKED_USER_ID, **{"custom:userId": UNLINKED_USER_ID}))
    assert connect_with_token(token) == handler.ACCEPTED
    assert read_connection_row()["userId"] == LINKED_USER_ID
    assert graph.list_connection_ids(LINKED_USER_ID) == [CONNECTION_ID]
    assert graph.list_connection_ids(UNLINKED_USER_ID) == []


def test_unlinked_sub_is_refused(aws, cognito_environment):  # noqa: F811
    assert connect_with_token(sign_token(build_claims())) == handler.REJECTED
    assert read_connection_row() is None


def test_failed_jwt_check_never_falls_through_to_the_opaque_lookup(aws, cognito_environment):  # noqa: F811
    """The tampered token string is also a TOKEN# row of an unlinked user who has a valid link, so a fall-through would
    connect them."""
    circle.issue_access_token(UNLINKED_USER_ID)
    header_segment, _claims_segment, signature_segment = sign_token(build_claims()).split(".")
    edited_claims_segment = encode_segment(json.dumps(build_claims(sub="someone-else")).encode())
    tampered_token = f"{header_segment}.{edited_claims_segment}.{signature_segment}"
    graph.get_table().put_item(Item={
        "PK": f"TOKEN#{graph.hash_access_token(tampered_token)}", "SK": graph.METADATA_SORT_KEY, "userId": UNLINKED_USER_ID})
    assert graph.find_user_id_for_access_token(tampered_token) == UNLINKED_USER_ID  # the opaque path alone would accept it
    assert connect_with_token(tampered_token) == handler.REJECTED
    assert read_connection_row() is None


def test_opaque_token_of_a_linked_user_is_refused(aws):  # noqa: F811
    opaque_token = link_user_to_test_sub(LINKED_USER_ID)
    assert connect_with_token(opaque_token) == handler.REJECTED
    assert read_connection_row() is None


def test_opaque_token_of_an_unlinked_user_connects(aws):  # noqa: F811
    opaque_token = circle.issue_access_token(UNLINKED_USER_ID)
    assert connect_with_token(opaque_token) == handler.ACCEPTED
    assert read_connection_row()["userId"] == UNLINKED_USER_ID
