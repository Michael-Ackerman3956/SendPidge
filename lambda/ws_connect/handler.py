"""$connect accepts a Cognito ID token (passkey sign-in) or the per-user opaque access token, chosen by the token's shape;
$disconnect forgets the connection. A JWT-shaped token is only ever checked as a Cognito token, and an opaque token is
refused for a user linked to Cognito (cognitoSub on their PROFILE): that user signs in with a passkey only."""

import logging
from enum import StrEnum

from shared import cognito_auth, graph, profiles
from shared.cognito_auth import CognitoTokenRejected

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class WebSocketRoute(StrEnum):
    CONNECT = "$connect"
    DISCONNECT = "$disconnect"


class ConnectionRejectionReason(StrEnum):
    MISSING_TOKEN = "missing_token"
    UNLINKED_SUB = "unlinked_sub"  # a valid Cognito ID token whose sub has no SUB# row
    UNKNOWN_ACCESS_TOKEN = "unknown_access_token"  # no TOKEN# row for this opaque token
    SIGN_IN_REQUIRED = "sign_in_required"  # an opaque token of a user linked to Cognito


ACCEPTED = {"statusCode": 200}
REJECTED = {"statusCode": 401}


def log_rejection(connection_id: str, reason: StrEnum) -> None:
    """The reason and the connection id only; never the token or its claims."""
    logger.warning("rejected connection %s: %s", connection_id, reason)


def resolve_user_id_from_cognito_token(token: str, connection_id: str) -> str | None:
    try:
        claims = cognito_auth.verify_cognito_id_token(token)
    except CognitoTokenRejected as rejection:
        log_rejection(connection_id, rejection.reason)
        return None
    user_id = cognito_auth.find_user_id_for_sub(claims["sub"])  # the SUB# row decides; no other claim is read
    if user_id is None:
        log_rejection(connection_id, ConnectionRejectionReason.UNLINKED_SUB)
    return user_id


def resolve_user_id_from_access_token(token: str, connection_id: str) -> str | None:
    user_id = graph.find_user_id_for_access_token(token)
    if user_id is None:
        log_rejection(connection_id, ConnectionRejectionReason.UNKNOWN_ACCESS_TOKEN)
        return None
    profile_row = profiles.read_profile_row(f"USER#{user_id}")
    if profile_row is not None and cognito_auth.COGNITO_SUB_FIELD in profile_row:
        log_rejection(connection_id, ConnectionRejectionReason.SIGN_IN_REQUIRED)
        return None
    return user_id


def resolve_user_id(token: str, connection_id: str) -> str | None:
    """The connecting user, or None (rejection logged). A JWT-shaped token that fails its check is rejected, never retried
    as an opaque token."""
    if cognito_auth.looks_like_jwt(token):
        return resolve_user_id_from_cognito_token(token, connection_id)
    return resolve_user_id_from_access_token(token, connection_id)


def handle(event, context):
    request_context = event["requestContext"]
    route = WebSocketRoute(request_context["routeKey"])
    connection_id = request_context["connectionId"]
    if route is WebSocketRoute.DISCONNECT:
        graph.remove_connection(connection_id)
        return ACCEPTED
    # ?token= carries either a Cognito ID token or the opaque access token.
    access_token = (event.get("queryStringParameters") or {}).get("token")
    if not access_token:
        log_rejection(connection_id, ConnectionRejectionReason.MISSING_TOKEN)
        return REJECTED
    user_id = resolve_user_id(access_token, connection_id)
    if user_id is None:
        return REJECTED
    graph.register_connection(connection_id, user_id)  # userId comes from the TOKEN# or SUB# row, never the client
    logger.info("connected %s as %s", connection_id, user_id)
    return ACCEPTED
