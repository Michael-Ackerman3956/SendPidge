"""Web Push: only browser push-service endpoints are stored, under the caller's own row; the notice is fixed text and a
failed send is reported, never raised."""

import base64
import json

import boto3
import pytest
import pywebpush
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException

from shared import push_subscriptions, web_push
from shared.enums import ActionOutcome, PushNotice
from tests.test_verdict import aws  # noqa: F401

APPLE_ENDPOINT = "https://web.push.apple.com/QGuQyavXutnMH5VNV3bAQZ"
VAPID_SECRET_NAME = "sendpidge/vapid-private-key-test"


def base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def browser_subscription(endpoint: str = APPLE_ENDPOINT) -> dict:
    """A real P-256 key pair and auth secret, the way a browser's PushSubscription.toJSON() returns them."""
    browser_key = ec.generate_private_key(ec.SECP256R1())
    public_point = browser_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"endpoint": endpoint, "keys": {"p256dh": base64url(public_point), "auth": base64url(b"0123456789abcdef")}}


@pytest.fixture
def vapid_secret(aws, monkeypatch):  # noqa: F811
    monkeypatch.setenv("VAPID_SECRET_NAME", VAPID_SECRET_NAME)
    private_value = ec.generate_private_key(ec.SECP256R1()).private_numbers().private_value.to_bytes(32, "big")
    boto3.client("secretsmanager").create_secret(Name=VAPID_SECRET_NAME, SecretString=base64url(private_value))
    web_push._vapid_signer = None
    yield
    web_push._vapid_signer = None


@pytest.mark.parametrize("endpoint", [
    "https://web.push.apple.com/abc", "https://fcm.googleapis.com/fcm/send/abc",
    "https://updates.push.services.mozilla.com/wpush/v2/abc", "https://wns2-par02p.notify.windows.com/w/?token=abc",
])
def test_browser_push_services_are_allowed(endpoint):
    assert push_subscriptions.is_allowed_push_endpoint(endpoint)


@pytest.mark.parametrize("endpoint", [
    "http://web.push.apple.com/abc", "https://evil.example/abc", "https://web.push.apple.com.evil.example/abc",
    "https://web.push.apple.com:8443/abc", "https://user:pass@fcm.googleapis.com/abc", "https://notify.windows.com.evil.example/",
    "https://fcm.googleapis.com:bad/abc", "https://169.254.169.254/latest/meta-data", 42, None,
    "https://fcm.googleapis.com/" + "a" * push_subscriptions.ENDPOINT_MAX_CHARACTERS,
])
def test_any_other_endpoint_is_refused(endpoint):
    assert not push_subscriptions.is_allowed_push_endpoint(endpoint)


def test_a_subscription_is_stored_under_the_callers_row_never_one_from_the_body(aws):  # noqa: F811
    body = {"subscription": browser_subscription(), "userId": "someone-else"}
    assert push_subscriptions.save_subscription("grandma", body) is ActionOutcome.RECORDED
    assert push_subscriptions.save_subscription("grandma", body) is ActionOutcome.RECORDED  # same browser overwrites
    rows = push_subscriptions.list_subscription_rows("grandma")
    assert [row["endpoint"] for row in rows] == [APPLE_ENDPOINT]
    assert push_subscriptions.list_subscription_rows("someone-else") == []


def test_a_bad_subscription_and_the_sixth_browser_are_refused(aws):  # noqa: F811
    assert push_subscriptions.save_subscription("grandma", {"subscription": browser_subscription("https://evil.example/x")}) is ActionOutcome.BAD_MESSAGE
    assert push_subscriptions.save_subscription("grandma", {"subscription": {"endpoint": APPLE_ENDPOINT, "keys": {}}}) is ActionOutcome.BAD_MESSAGE
    for index in range(push_subscriptions.MAXIMUM_SUBSCRIPTIONS_PER_USER):
        assert push_subscriptions.save_subscription("grandma", {"subscription": browser_subscription(f"{APPLE_ENDPOINT}{index}")}) is ActionOutcome.RECORDED
    assert push_subscriptions.save_subscription("grandma", {"subscription": browser_subscription(f"{APPLE_ENDPOINT}new")}) is ActionOutcome.NOT_ALLOWED


class RecordedPost:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.calls: list[dict] = []

    def __call__(self, url, data=None, headers=None, timeout=None, **_):
        self.calls.append({"url": url, "data": data, "headers": headers})
        response = type("Response", (), {"status_code": self.status_code, "reason": "", "text": "", "headers": {}})()
        return response


def test_the_notice_is_encrypted_signed_and_fixed_text(vapid_secret, monkeypatch):
    push_subscriptions.save_subscription("grandma", {"subscription": browser_subscription()})
    recorded_post = RecordedPost(201)
    monkeypatch.setattr(pywebpush.requests, "post", recorded_post)
    assert web_push.send_notice("grandma", PushNotice.BANK_REPLIED) == 1
    call = recorded_post.calls[0]
    assert call["url"] == APPLE_ENDPOINT
    assert call["headers"]["Authorization"].startswith("vapid t=")
    assert call["headers"]["Content-Encoding"] == "aes128gcm"
    assert b"bank replied" not in call["data"]  # the payload is encrypted for the browser's key


def test_the_payload_is_only_the_fixed_sentence(vapid_secret, monkeypatch):
    push_subscriptions.save_subscription("grandma", {"subscription": browser_subscription()})
    sent_payloads = []
    monkeypatch.setattr(web_push, "webpush", lambda subscription, data, **_: sent_payloads.append(json.loads(data)))
    web_push.send_notice("grandma", PushNotice.BANK_REPLIED)
    assert sent_payloads == [{"title": "SendPidge", "body": "Your bank replied to your Pidge case.", "notice": "bank_replied"}]


def test_family_call_notice_names_the_contact_and_refuses_without_a_name(vapid_secret, monkeypatch):
    push_subscriptions.save_subscription("grandma", {"subscription": browser_subscription()})
    sent_payloads = []
    monkeypatch.setattr(web_push, "webpush", lambda subscription, data, **_: sent_payloads.append(json.loads(data)))
    assert web_push.send_notice("grandma", PushNotice.FAMILY_CALL_TODAY, contact_name="Jimin") == 1
    assert web_push.send_notice("grandma", PushNotice.FAMILY_CALL_TOMORROW, contact_name="Jimin") == 1
    assert [payload["body"] for payload in sent_payloads] == ["Jimin will call you today.", "Jimin will call you tomorrow."]
    for notice in (PushNotice.FAMILY_CALL_TODAY, PushNotice.FAMILY_CALL_TOMORROW):
        with pytest.raises(ValueError):
            web_push.send_notice("grandma", notice)
    assert len(sent_payloads) == 2  # the refused notices sent nothing


def test_an_expired_browser_is_removed(vapid_secret, monkeypatch):
    push_subscriptions.save_subscription("grandma", {"subscription": browser_subscription()})
    monkeypatch.setattr(pywebpush.requests, "post", RecordedPost(410))
    assert web_push.send_notice("grandma", PushNotice.BANK_REPLIED) == 0
    assert push_subscriptions.list_subscription_rows("grandma") == []


def test_a_failed_send_is_reported_and_never_raised(vapid_secret, monkeypatch, capsys):
    push_subscriptions.save_subscription("grandma", {"subscription": browser_subscription()})
    monkeypatch.setattr(pywebpush.requests, "post", RecordedPost(500))
    assert web_push.send_notice("grandma", PushNotice.BANK_REPLIED) == 0
    assert len(push_subscriptions.list_subscription_rows("grandma")) == 1  # kept: a 500 is not an unsubscribe
    assert '"PushFailed": 1' in capsys.readouterr().out


def test_a_missing_vapid_secret_is_reported_and_never_raised(aws, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setenv("VAPID_SECRET_NAME", "not-there")
    web_push._vapid_signer = None
    push_subscriptions.save_subscription("grandma", {"subscription": browser_subscription()})
    assert web_push.send_notice("grandma", PushNotice.BANK_REPLIED) == 0
    assert '"PushFailed": 1' in capsys.readouterr().out


def test_no_saved_browser_sends_nothing(vapid_secret):
    assert web_push.send_notice("grandma", PushNotice.BANK_REPLIED) == 0


def test_webpush_exception_type_is_the_one_caught():
    assert issubclass(WebPushException, Exception)
