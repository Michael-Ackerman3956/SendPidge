"""Passkey sign-in on $connect (slice 13): verifies a Cognito ID token and maps its sub to a user through SUB#<sub>/META
{userId}. RS256 only, keys from the pool's JWKS (one client per container, keys cached), audience = the app client, issuer =
the pool, both from COGNITO_ISSUER and COGNITO_APP_CLIENT_ID. Every failure raises CognitoTokenRejected with its reason;
nothing falls back."""

import os
import re
from enum import StrEnum

import jwt
from jwt import PyJWK, PyJWKClient
from jwt.exceptions import PyJWTError

from shared import graph
from shared.profiles import COGNITO_SUB_FIELD  # noqa: F401  (read here by ws_connect and scripts/seed_auth_users.py)

ISSUER_ENVIRONMENT_NAME = "COGNITO_ISSUER"
APP_CLIENT_ID_ENVIRONMENT_NAME = "COGNITO_APP_CLIENT_ID"
JWKS_PATH = "/.well-known/jwks.json"
JWKS_FETCH_TIMEOUT_SECONDS = 4  # an unknown kid refetches at most once per 30 s (PyJWT cooldown); fits ws_connect's 10 s
SIGNING_ALGORITHMS = ["RS256"]
REQUIRED_CLAIMS = ["exp", "iat", "iss", "aud", "sub", "token_use"]
ID_TOKEN_USE = "id"
SUB_PARTITION_PREFIX = "SUB#"  # SUB#<sub>/META {userId}, written by scripts/seed_auth_users.py
# Three base64url segments, the signature possibly empty (an unsigned token is still routed here and refused). An opaque
# TOKEN# value (circle.new_access_token: secrets.token_urlsafe) has no "." and never matches.
JWT_SHAPE_PATTERN = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")

_jwks_client = None


class CognitoRejectionReason(StrEnum):
    NOT_CONFIGURED = "not_configured"  # COGNITO_ISSUER or COGNITO_APP_CLIENT_ID absent or empty
    MALFORMED = "malformed"  # not JWT-shaped, or a header that does not decode or names no kid
    SIGNING_KEY_UNAVAILABLE = "signing_key_unavailable"  # the JWKS could not be read or holds no key with this kid
    INVALID = "invalid"  # signature, algorithm, audience, issuer, expiry or a required claim failed
    WRONG_TOKEN_USE = "wrong_token_use"  # token_use is not "id" (an access token)
    NO_SUBJECT = "no_subject"  # sub is empty


class CognitoTokenRejected(Exception):
    def __init__(self, reason: CognitoRejectionReason):
        super().__init__(str(reason))
        self.reason = reason


def looks_like_jwt(token: str) -> bool:
    return isinstance(token, str) and JWT_SHAPE_PATTERN.fullmatch(token) is not None


def build_jwks_client(jwks_url: str) -> PyJWKClient:
    return PyJWKClient(jwks_url, cache_keys=True, timeout=JWKS_FETCH_TIMEOUT_SECONDS)


def get_jwks_client(issuer: str) -> PyJWKClient:
    """The pool's JWKS client, built on first use and kept for the container's life (its keys stay cached)."""
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = build_jwks_client(issuer + JWKS_PATH)
    return _jwks_client


def read_cognito_configuration() -> tuple[str, str]:
    """(issuer, app client id) from the environment; NOT_CONFIGURED when either is absent or empty (sign-in is off)."""
    issuer = os.environ.get(ISSUER_ENVIRONMENT_NAME)
    app_client_id = os.environ.get(APP_CLIENT_ID_ENVIRONMENT_NAME)
    if not issuer or not app_client_id:
        raise CognitoTokenRejected(CognitoRejectionReason.NOT_CONFIGURED)
    return issuer, app_client_id


def read_signing_key(token: str, issuer: str) -> PyJWK:
    """The JWKS key named by the token's kid. MALFORMED when the header does not decode or names no kid; any fetch or lookup
    failure (network, bad JSON, unknown kid after one refresh) is SIGNING_KEY_UNAVAILABLE."""
    try:
        header = jwt.get_unverified_header(token)
    except PyJWTError as error:
        raise CognitoTokenRejected(CognitoRejectionReason.MALFORMED) from error
    key_id = header.get("kid")
    if not isinstance(key_id, str) or key_id == "":
        raise CognitoTokenRejected(CognitoRejectionReason.MALFORMED)
    try:
        return get_jwks_client(issuer).get_signing_key(key_id)
    except (PyJWTError, OSError, ValueError) as error:
        raise CognitoTokenRejected(CognitoRejectionReason.SIGNING_KEY_UNAVAILABLE) from error


def verify_cognito_id_token(token: str) -> dict:
    """The verified claims of a Cognito ID token from this pool for this app client. Raises CognitoTokenRejected otherwise."""
    issuer, app_client_id = read_cognito_configuration()
    if not looks_like_jwt(token):
        raise CognitoTokenRejected(CognitoRejectionReason.MALFORMED)
    signing_key = read_signing_key(token, issuer)
    try:
        claims = jwt.decode(
            token, signing_key, algorithms=SIGNING_ALGORITHMS, audience=app_client_id, issuer=issuer,
            options={"require": REQUIRED_CLAIMS},
        )
    except PyJWTError as error:
        raise CognitoTokenRejected(CognitoRejectionReason.INVALID) from error
    if claims["token_use"] != ID_TOKEN_USE:
        raise CognitoTokenRejected(CognitoRejectionReason.WRONG_TOKEN_USE)
    if not claims["sub"].strip():  # PyJWT already refused a missing or non-text sub
        raise CognitoTokenRejected(CognitoRejectionReason.NO_SUBJECT)
    return claims


def find_user_id_for_sub(sub: str) -> str | None:
    """The userId on SUB#<sub>/META, or None when no row maps this sub (the Cognito user is not linked to a SendPidge user)."""
    response = graph.get_table().get_item(Key={"PK": f"{SUB_PARTITION_PREFIX}{sub}", "SK": graph.METADATA_SORT_KEY})
    return response["Item"]["userId"] if "Item" in response else None
