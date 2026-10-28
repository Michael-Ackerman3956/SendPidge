"""Settings → personal email (set_personal_email, confirm_personal_email) and where the recovery pack goes: a code is mailed
only to the address she typed, the address is saved only when that code comes back from her own app before it expires, our
own domains and her protected address are refused, the code-email cap fails closed, and the pack goes to the saved address."""

import re
from datetime import datetime, timezone
from unittest.mock import ANY

import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from pidge_verdict import settings_handlers
from shared import channels, graph, personal_email, profiles
from tests.helpers.profiles import seed_resident
from tests.test_recovery_drafts import PIDGE_ADDRESS, RECEIVER_MAILBOX, dispatch, stored_note  # noqa: F401
from tests.test_verdict import aws  # noqa: F401
from tests.test_verify_contact import rejection, send_action, verdict_handler_without_profiles  # noqa: F401

GRANDMA_ADDRESS = "grandma@sendpidge.example"
BOB_ADDRESS = "bob@sendpidge.example"
NEW_PERSONAL_EMAIL = "grandma.kim@gmail.com"
CODE_PATTERN = re.compile(r"Your SendPidge code is (\d{6})\.")


class FakeSesClient:
    """Records each send_email call; raises instead when should_refuse is set."""

    def __init__(self):
        self.sent_emails: list[dict] = []
        self.should_refuse = False

    def send_email(self, **arguments):
        if self.should_refuse:
            raise ClientError({"Error": {"Code": "MessageRejected", "Message": "refused"}}, "SendEmail")
        self.sent_emails.append(arguments)
        return {"MessageId": "m1"}

    def last_code(self) -> str:
        return CODE_PATTERN.search(self.sent_emails[-1]["Content"]["Simple"]["Body"]["Text"]["Data"]).group(1)


@pytest.fixture
def settings(verdict_handler_without_profiles, monkeypatch):  # noqa: F811
    """pidge-verdict with Grandma and Bob enrolled; returns (handler, replies to the acting socket, push_to_user calls, SES)."""
    handler, pushed_messages = verdict_handler_without_profiles
    seed_resident(GRANDMA_ADDRESS, "grandma", "Grandma")
    seed_resident(BOB_ADDRESS, "bob", "Bob")
    user_pushes: list[tuple[str, dict]] = []
    monkeypatch.setattr(channels, "push_to_user", lambda user_id, message, should_remove_gone_connections=True: user_pushes.append(
        (user_id, message)) or 1)
    fake_ses_client = FakeSesClient()
    monkeypatch.setattr(settings_handlers, "_ses_client", fake_ses_client)
    return handler, pushed_messages, user_pushes, fake_ses_client


def set_email(handler, user_id: str, address) -> None:
    send_action(handler, user_id, {"action": "set_personal_email", "address": address})


def confirm(handler, user_id: str, code) -> None:
    send_action(handler, user_id, {"action": "confirm_personal_email", "code": code})


def other_code_than(code: str) -> str:
    return "000000" if code != "000000" else "111111"


def grandma_profile_row() -> dict:
    return profiles.read_profile_row(f"RECEIVER#{GRANDMA_ADDRESS}")


def test_the_mailed_code_saves_the_address_and_sync_shows_it(settings):
    handler, pushed_messages, user_pushes, fake_ses_client = settings
    set_email(handler, "grandma", "  Grandma.Kim@Gmail.com ")
    assert pushed_messages == []  # success has no reply of its own
    [sent] = fake_ses_client.sent_emails
    assert (sent["FromEmailAddress"], sent["Destination"]) == ("pidge@sendpidge.example", {"ToAddresses": [NEW_PERSONAL_EMAIL]})
    assert user_pushes[-1] == ("grandma", {"type": "personal_email_state", "personalEmail": {
        "protectedEmail": GRANDMA_ADDRESS, "personalEmail": None, "pendingPersonalEmail": NEW_PERSONAL_EMAIL}})
    assert personal_email.verified_personal_email(GRANDMA_ADDRESS) is None  # not saved before the code comes back

    confirm(handler, "grandma", fake_ses_client.last_code())
    assert pushed_messages == []
    assert user_pushes[-1][1]["personalEmail"] == {
        "protectedEmail": GRANDMA_ADDRESS, "personalEmail": NEW_PERSONAL_EMAIL, "pendingPersonalEmail": None}
    assert personal_email.verified_personal_email(GRANDMA_ADDRESS) == NEW_PERSONAL_EMAIL
    assert not any(field_name in grandma_profile_row() for field_name in personal_email.PENDING_FIELDS)
    send_action(handler, "grandma", {"action": "sync"})
    assert pushed_messages[-1]["personalEmail"]["personalEmail"] == NEW_PERSONAL_EMAIL


@pytest.mark.parametrize("address", [
    "pidge@sendpidge.example", "anyone@mail.sendpidge.example", GRANDMA_ADDRESS, "not-an-email", "a@b", "two@@gmail.com",
    "spaced name@gmail.com", "x" * 250 + "@gmail.com", 12345, None,
])
def test_an_unusable_address_is_refused_and_nothing_is_mailed(settings, address):
    handler, pushed_messages, user_pushes, fake_ses_client = settings
    set_email(handler, "grandma", address)
    assert pushed_messages == [rejection("bad_message")]
    assert (fake_ses_client.sent_emails, user_pushes) == ([], [])
    assert personal_email.PENDING_EMAIL_FIELD not in grandma_profile_row()


def test_wrong_codes_are_refused_and_the_attempts_run_out(settings):
    handler, pushed_messages, _user_pushes, fake_ses_client = settings
    set_email(handler, "grandma", NEW_PERSONAL_EMAIL)
    right_code = fake_ses_client.last_code()
    for _attempt in range(personal_email.CODE_ATTEMPTS_LIMIT):
        confirm(handler, "grandma", other_code_than(right_code))
        assert pushed_messages[-1] == rejection("wrong_code")
    confirm(handler, "grandma", right_code)
    assert pushed_messages[-1] == rejection("code_expired")  # the attempts are used up: even the right code needs a new email
    assert personal_email.verified_personal_email(GRANDMA_ADDRESS) is None


def test_an_expired_code_is_refused(settings):
    handler, pushed_messages, _user_pushes, fake_ses_client = settings
    set_email(handler, "grandma", NEW_PERSONAL_EMAIL)
    graph.get_table().update_item(Key={"PK": f"RECEIVER#{GRANDMA_ADDRESS}", "SK": "PROFILE"},
                                  UpdateExpression="SET #expires = :past", ExpressionAttributeNames={"#expires": personal_email.PENDING_EXPIRES_AT_FIELD},
                                  ExpressionAttributeValues={":past": "2020-01-01T00:00:00Z"})
    confirm(handler, "grandma", fake_ses_client.last_code())
    assert pushed_messages[-1] == rejection("code_expired")
    assert personal_email.verified_personal_email(GRANDMA_ADDRESS) is None


@pytest.mark.parametrize("code", ["12345", "1234567", "abcdef", 123456, None])
def test_a_code_that_is_not_six_digits_is_a_bad_message(settings, code):
    handler, pushed_messages, _user_pushes, _fake_ses_client = settings
    set_email(handler, "grandma", NEW_PERSONAL_EMAIL)
    confirm(handler, "grandma", code)
    assert pushed_messages[-1] == rejection("bad_message")


def test_a_new_change_replaces_the_old_code_and_a_saved_address_changes(settings):
    handler, pushed_messages, _user_pushes, fake_ses_client = settings
    set_email(handler, "grandma", NEW_PERSONAL_EMAIL)
    confirm(handler, "grandma", fake_ses_client.last_code())
    set_email(handler, "grandma", "first@gmail.com")
    first_code = fake_ses_client.last_code()
    set_email(handler, "grandma", "second@gmail.com")
    second_code = fake_ses_client.last_code()
    if first_code != second_code:
        confirm(handler, "grandma", first_code)
        assert pushed_messages[-1] == rejection("wrong_code")
    assert personal_email.verified_personal_email(GRANDMA_ADDRESS) == NEW_PERSONAL_EMAIL  # the saved one holds until confirmed
    confirm(handler, "grandma", second_code)
    assert personal_email.verified_personal_email(GRANDMA_ADDRESS) == "second@gmail.com"


def test_confirm_with_nothing_waiting_is_not_applied(settings):
    handler, pushed_messages, _user_pushes, _fake_ses_client = settings
    confirm(handler, "grandma", "123456")
    assert pushed_messages == [rejection("not_applied")]


def test_one_resident_cannot_confirm_anothers_change(settings):
    handler, pushed_messages, _user_pushes, fake_ses_client = settings
    set_email(handler, "grandma", NEW_PERSONAL_EMAIL)
    confirm(handler, "bob", fake_ses_client.last_code())
    assert pushed_messages[-1] == rejection("not_applied")  # Bob's own row has no change waiting
    assert personal_email.verified_personal_email(BOB_ADDRESS) is None
    assert grandma_profile_row()[personal_email.PENDING_EMAIL_FIELD] == NEW_PERSONAL_EMAIL


@pytest.mark.parametrize("action", ["set_personal_email", "confirm_personal_email"])
def test_only_a_resident_may_use_settings(settings, action):
    handler, pushed_messages, _user_pushes, fake_ses_client = settings
    send_action(handler, "stranger", {"action": action, "address": NEW_PERSONAL_EMAIL, "code": "123456"})
    assert pushed_messages == [rejection("not_allowed")]
    assert fake_ses_client.sent_emails == []


def test_the_hourly_code_email_cap_and_a_failed_counter_both_refuse(settings, monkeypatch):
    handler, pushed_messages, _user_pushes, fake_ses_client = settings
    for _send in range(personal_email.CODE_EMAILS_PER_HOUR):
        set_email(handler, "grandma", NEW_PERSONAL_EMAIL)
    assert pushed_messages == []
    set_email(handler, "grandma", NEW_PERSONAL_EMAIL)
    assert pushed_messages == [rejection("over_cap")]
    assert len(fake_ses_client.sent_emails) == personal_email.CODE_EMAILS_PER_HOUR

    def refuse_every_update(**_arguments):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}}, "UpdateItem")

    monkeypatch.setattr(graph.get_table(), "update_item", refuse_every_update)
    pushed_messages.clear()
    set_email(handler, "bob", "bob.private@gmail.com")
    assert pushed_messages == [rejection("over_cap")]  # the counter could not be written: refused, never a free pass
    assert len(fake_ses_client.sent_emails) == personal_email.CODE_EMAILS_PER_HOUR


def test_a_refused_code_email_says_so_and_keeps_the_change_waiting(settings, monkeypatch):
    handler, pushed_messages, user_pushes, fake_ses_client = settings
    counted = []
    monkeypatch.setattr(personal_email, "emit_count_metric", lambda metric_name, *_: counted.append(metric_name))
    fake_ses_client.should_refuse = True
    set_email(handler, "grandma", NEW_PERSONAL_EMAIL)
    assert pushed_messages == [rejection("send_failed")]
    assert counted == ["PersonalEmailCodeFailed"]
    assert user_pushes[-1][1]["personalEmail"]["pendingPersonalEmail"] == NEW_PERSONAL_EMAIL


def save_personal_email(receiver_address: str, address: str) -> None:
    """Through the production two-step flow, with a recording SES client."""
    resident = profiles.require_receiver_profile(receiver_address)
    fake_ses_client = FakeSesClient()
    now = datetime.now(timezone.utc)
    assert personal_email.start_change(resident, address, now, fake_ses_client, PIDGE_ADDRESS,
                                       frozenset({"sendpidge.example"})).value == "recorded"
    assert personal_email.confirm_change(resident, fake_ses_client.last_code(), now).value == "recorded"


def test_the_pack_goes_to_her_saved_personal_email_before_the_deployment_mailbox(dispatch):  # noqa: F811
    handler, ses_client = dispatch
    resident = profiles.require_receiver_profile("grandma@example.com")
    save_personal_email(resident.receiver_address, NEW_PERSONAL_EMAIL)
    note_row = stored_note()
    with Stubber(ses_client) as stubber:
        stubber.add_response("send_email", {"MessageId": "m1"}, {
            "FromEmailAddress": PIDGE_ADDRESS, "Destination": {"ToAddresses": [NEW_PERSONAL_EMAIL]}, "Content": ANY})
        mailed_row = handler.mail_recovery_pack(note_row, resident)
    assert mailed_row["packMailedAt"].endswith("Z")
    assert NEW_PERSONAL_EMAIL != RECEIVER_MAILBOX


def test_any_resident_with_a_saved_personal_email_gets_the_pack_mailed(dispatch):  # noqa: F811
    handler, ses_client = dispatch
    seed_resident("bob@example.com", "bob", "Bob")
    save_personal_email("bob@example.com", "bob.private@gmail.com")
    note_row = stored_note("bob")
    with Stubber(ses_client) as stubber:
        stubber.add_response("send_email", {"MessageId": "m1"}, {
            "FromEmailAddress": PIDGE_ADDRESS, "Destination": {"ToAddresses": ["bob.private@gmail.com"]}, "Content": ANY})
        mailed_row = handler.mail_recovery_pack(note_row, profiles.require_receiver_profile("bob@example.com"))
    assert "packMailedAt" in mailed_row


def test_a_pending_unconfirmed_address_never_gets_the_pack(dispatch):  # noqa: F811
    handler, ses_client = dispatch
    seed_resident("bob@example.com", "bob", "Bob")
    resident = profiles.require_receiver_profile("bob@example.com")
    personal_email.start_change(resident, "bob.private@gmail.com", datetime.now(timezone.utc), FakeSesClient(), PIDGE_ADDRESS,
                                frozenset({"sendpidge.example"}))
    note_row = stored_note("bob")
    with Stubber(ses_client):  # any send would raise
        kept_row = handler.mail_recovery_pack(note_row, resident)
    assert "packMailedAt" not in kept_row
