"""Cognito ID-token verification against one local RSA key and its JWKS. The JWKS-client factory is the only seam: the
production PyJWKClient runs with its network fetch replaced by the local key set, and the production decode runs as is.
A valid ID token passes; every forged, misdirected, expired or wrong-kind token is refused with its exact reason."""

import base64
import hashlib
import hmac
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from jwt import PyJWKClient
from jwt.algorithms import RSAAlgorithm

from shared import circle, cognito_auth
from shared.cognito_auth import CognitoRejectionReason, CognitoTokenRejected
from tests.test_verdict import aws  # noqa: F401

TEST_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool"
TEST_APP_CLIENT_ID = "test-app-client-id"
TEST_KEY_ID = "test-key-1"
TEST_SUB = "5f3c2a1e-0000-4000-8000-00000000abcd"
OTHER_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_OtherPool"
TOKEN_LIFETIME_SECONDS = 3600
SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
TEST_JWKS = {"keys": [{**json.loads(RSAAlgorithm.to_jwk(SIGNING_KEY.public_key())), "kid": TEST_KEY_ID, "use": "sig", "alg": "RS256"}]}


class LocalJwksClient(PyJWKClient):
    """The production client class (key cache, unknown-kid refresh) with its HTTP fetch answered by TEST_JWKS."""

    def fetch_data(self) -> dict:
        return TEST_JWKS


@pytest.fixture
def cognito_environment(monkeypatch) -> list[str]:
    """Sign-in configured for the test pool; returns the JWKS URLs the module asked its factory for."""
    monkeypatch.setenv(cognito_auth.ISSUER_ENVIRONMENT_NAME, TEST_ISSUER)
    monkeypatch.setenv(cognito_auth.APP_CLIENT_ID_ENVIRONMENT_NAME, TEST_APP_CLIENT_ID)
    requested_jwks_urls: list[str] = []

    def build_local_jwks_client(jwks_url: str) -> PyJWKClient:
        requested_jwks_urls.append(jwks_url)
        return LocalJwksClient(jwks_url, cache_keys=True)

    monkeypatch.setattr(cognito_auth, "build_jwks_client", build_local_jwks_client)
    monkeypatch.setattr(cognito_auth, "_jwks_client", None)  # the module keeps one client per container
    return requested_jwks_urls


def build_claims(**overrides) -> dict:
    issued_at = int(time.time())
    claims = {"sub": TEST_SUB, "aud": TEST_APP_CLIENT_ID, "iss": TEST_ISSUER, "token_use": "id", "iat": issued_at,
              "exp": issued_at + TOKEN_LIFETIME_SECONDS, "email": "grandma@example.com"}
    return {**claims, **overrides}


def sign_token(claims: dict, key_id: str = TEST_KEY_ID) -> str:
    return jwt.encode(claims, SIGNING_KEY, algorithm="RS256", headers={"kid": key_id})


def encode_segment(raw_bytes: bytes) -> str:
    return base64.urlsafe_b64encode(raw_bytes).rstrip(b"=").decode()


def join_header_and_claims(header: dict, claims: dict) -> str:
    return f"{encode_segment(json.dumps(header).encode())}.{encode_segment(json.dumps(claims).encode())}"


def forge_token_signed_with_public_key() -> str:
    """HS256 with the public key's PEM bytes as the HMAC secret (PyJWT refuses to mint it, so it is built by hand)."""
    signing_input = join_header_and_claims({"alg": "HS256", "typ": "JWT", "kid": TEST_KEY_ID}, build_claims())
    public_key_bytes = SIGNING_KEY.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    signature = hmac.new(public_key_bytes, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{encode_segment(signature)}"


def forge_unsigned_token() -> str:
    return join_header_and_claims({"alg": "none", "typ": "JWT", "kid": TEST_KEY_ID}, build_claims()) + "."


def tamper_signature(token: str) -> str:
    header_and_claims, signature_segment = token.rsplit(".", 1)
    signature = bytearray(base64.urlsafe_b64decode(signature_segment + "=" * (-len(signature_segment) % 4)))
    signature[0] ^= 0xFF
    return f"{header_and_claims}.{encode_segment(bytes(signature))}"


def build_claims_without_sub() -> dict:
    return {claim_name: value for claim_name, value in build_claims().items() if claim_name != "sub"}


def test_valid_id_token_returns_its_claims(cognito_environment):
    claims = cognito_auth.verify_cognito_id_token(sign_token(build_claims()))
    assert (claims["sub"], claims["token_use"], claims["aud"]) == (TEST_SUB, "id", TEST_APP_CLIENT_ID)
    assert cognito_environment == [f"{TEST_ISSUER}/.well-known/jwks.json"]


# The access token carries aud here so that token_use is what refuses it (a real Cognito access token has client_id, not
# aud, and is refused earlier as INVALID).
REJECTED_TOKEN_CASES = {
    "access_token": (lambda: sign_token(build_claims(token_use="access")), CognitoRejectionReason.WRONG_TOKEN_USE),
    "wrong_audience": (lambda: sign_token(build_claims(aud="another-app-client")), CognitoRejectionReason.INVALID),
    "wrong_issuer": (lambda: sign_token(build_claims(iss=OTHER_ISSUER)), CognitoRejectionReason.INVALID),
    "expired": (lambda: sign_token(build_claims(iat=int(time.time()) - 7200, exp=int(time.time()) - 60)), CognitoRejectionReason.INVALID),
    "tampered_signature": (lambda: tamper_signature(sign_token(build_claims())), CognitoRejectionReason.INVALID),
    "hs256_with_public_key": (forge_token_signed_with_public_key, CognitoRejectionReason.INVALID),
    "alg_none": (forge_unsigned_token, CognitoRejectionReason.INVALID),
    "unknown_kid": (lambda: sign_token(build_claims(), key_id="unknown-key"), CognitoRejectionReason.SIGNING_KEY_UNAVAILABLE),
    "missing_sub": (lambda: sign_token(build_claims_without_sub()), CognitoRejectionReason.INVALID),
    "empty_sub": (lambda: sign_token(build_claims(sub="")), CognitoRejectionReason.NO_SUBJECT),
}


@pytest.mark.parametrize("case_name", list(REJECTED_TOKEN_CASES))
def test_rejected_token_names_its_reason(cognito_environment, case_name):
    build_token, expected_reason = REJECTED_TOKEN_CASES[case_name]
    with pytest.raises(CognitoTokenRejected) as rejection:
        cognito_auth.verify_cognito_id_token(build_token())
    assert rejection.value.reason is expected_reason


@pytest.mark.parametrize("unset_variable", [cognito_auth.ISSUER_ENVIRONMENT_NAME, cognito_auth.APP_CLIENT_ID_ENVIRONMENT_NAME])
def test_missing_or_empty_configuration_is_not_configured(cognito_environment, monkeypatch, unset_variable):
    valid_token = sign_token(build_claims())
    monkeypatch.delenv(unset_variable)
    with pytest.raises(CognitoTokenRejected) as rejection:
        cognito_auth.verify_cognito_id_token(valid_token)
    assert rejection.value.reason is CognitoRejectionReason.NOT_CONFIGURED
    monkeypatch.setenv(unset_variable, "")
    with pytest.raises(CognitoTokenRejected) as rejection:
        cognito_auth.verify_cognito_id_token(valid_token)
    assert rejection.value.reason is CognitoRejectionReason.NOT_CONFIGURED


def test_looks_like_jwt_tells_a_jwt_from_an_opaque_token(aws):  # noqa: F811
    opaque_token = circle.issue_access_token("grandma")
    assert cognito_auth.looks_like_jwt(sign_token(build_claims())) is True
    assert cognito_auth.looks_like_jwt(opaque_token) is False
