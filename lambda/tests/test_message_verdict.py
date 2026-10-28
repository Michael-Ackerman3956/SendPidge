"""Answers, Confirm and sync on a hold that came in as a text (MESSAGE#): the cross-channel Yes rule over every inbound and
answer channel pair, Confirm listing the number with no S3 tag, Reverse dropping only its own listed number, and sync
carrying the MESSAGE# rows. Production record_answer, confirm_quarantine and pidge-verdict on moto."""

import json

import pytest

from shared import graph, inbox_rows, lists, verdict
from shared.enums import ActionOutcome, Channel, Friction, ListSource, PolicyDecision, ReasonCode, RequestState, VerdictAnswer
from shared.messenger_enums import MessageDirection
from tests.helpers.profiles import seed_contact_roles
from tests.test_verdict import RECEIVER_ADDRESS, RECEIVER_USER_ID, VERDICT_FIELDS, at, aws, status_of  # noqa: F401
from tests.test_verify_contact import send_action, verdict_handler  # noqa: F401

SCAM_NUMBER = "+12065550100"
OTHER_SCAM_NUMBER = "+447700900123"
JIMIN_NUMBER = "+821011112222"
JIMIN_EMAIL = "jimin@example.com"
RECEIVED_AT = "2026-10-27T00:00:00Z"
NEW_PHONE_TEXT = "Mom it's Jimin, new phone"
JIMIN = graph.Contact("CONTACT#jimin", "Jimin", "jimin")
SENDER_ON = {
    Channel.INAPP: verdict.SenderIdentity(Channel.INAPP, "jimin"),
    Channel.WHATSAPP: verdict.SenderIdentity(Channel.WHATSAPP, JIMIN_NUMBER),
    Channel.RCS: verdict.SenderIdentity(Channel.RCS, JIMIN_NUMBER),
    Channel.EMAIL: verdict.SenderIdentity(Channel.EMAIL, JIMIN_EMAIL),
}
MESSAGE_PAYLOAD_FIELDS = {"messageId", "channel", "direction", "threadKey", "senderName", "senderAddress", "textBody", "receivedAt",
                          "state", "warnReason", "reasons", "quarantined", "request"}
RECORDED, REFUSED = ActionOutcome.RECORDED, ActionOutcome.WRONG_CHANNEL
# R3: a Yes is refused by email, and on the inbound channel's reach channel (SMS and RCS share one number); in-app always counts.
YES_GOLDENS = [
    (Channel.EMAIL, Channel.INAPP, RECORDED), (Channel.EMAIL, Channel.WHATSAPP, RECORDED),
    (Channel.EMAIL, Channel.RCS, RECORDED), (Channel.EMAIL, Channel.EMAIL, REFUSED),
    (Channel.SMS, Channel.INAPP, RECORDED), (Channel.SMS, Channel.WHATSAPP, RECORDED),
    (Channel.SMS, Channel.RCS, REFUSED), (Channel.SMS, Channel.EMAIL, REFUSED),
    (Channel.WHATSAPP, Channel.INAPP, RECORDED), (Channel.WHATSAPP, Channel.WHATSAPP, REFUSED),
    (Channel.WHATSAPP, Channel.RCS, RECORDED), (Channel.WHATSAPP, Channel.EMAIL, REFUSED),
    (Channel.RCS, Channel.INAPP, RECORDED), (Channel.RCS, Channel.WHATSAPP, RECORDED),
    (Channel.RCS, Channel.RCS, REFUSED), (Channel.RCS, Channel.EMAIL, REFUSED),
]


def seed_jimin() -> None:
    """Jimin with one checked number on WhatsApp and RCS and one email, so every answer channel passes is_expected_sender."""
    graph.get_table().put_item(Item={
        "PK": f"RECEIVER#{RECEIVER_ADDRESS}", "SK": "CONTACT#jimin", "contactName": "Jimin", "contactPwaUserId": "jimin",
        "verifiedVia": "seed", "contactReachPoints": [
            {"channel": "whatsapp", "address": JIMIN_NUMBER, "verifiedVia": "seed"},
            {"channel": "rcs", "address": JIMIN_NUMBER, "verifiedVia": "seed"},
            {"channel": "email", "address": JIMIN_EMAIL},
        ],
    })


def inbound_message_fields(channel: Channel, provider_message_id: str, sender_address: str) -> dict:
    """The MESSAGE# in-row fields of the spec's Rows contract (claim_row adds claimedAt and the ttl)."""
    return {
        "channel": str(channel), "direction": str(MessageDirection.IN), "threadKey": inbox_rows.thread_key_for(channel, sender_address),
        "providerMessageId": provider_message_id, "senderAddress": sender_address, "senderName": "", "textBody": NEW_PHONE_TEXT,
        "links": [], "receivedAt": RECEIVED_AT, "receiverEmail": RECEIVER_ADDRESS, "quarantined": False,
        graph.USER_INDEX_PARTITION_KEY: graph.user_index_partition(RECEIVER_USER_ID, inbox_rows.MESSAGE_ROW_KIND),
        graph.USER_INDEX_SORT_KEY: RECEIVED_AT,
    }


def new_hold(channel: Channel, provider_message_id: str = "m1", sender_address: str = SCAM_NUMBER) -> str:
    """A hold as the pipeline leaves it: the inbound row claimed and decided HOLD, its REQUEST# naming it on its channel."""
    if channel is Channel.EMAIL:
        item_id = f"inbox/{provider_message_id}.eml"
        row_fields = {"s3Key": item_id, "senderAddress": "scammer@lookalike.example", "quarantined": False}
    else:
        item_id = inbox_rows.message_item_id(channel, provider_message_id)
        row_fields = inbound_message_fields(channel, provider_message_id, sender_address)
    assert inbox_rows.claim_row(channel, item_id, row_fields)
    request_row = graph.create_request(item_id, RECEIVER_ADDRESS, JIMIN, VERDICT_FIELDS, Friction.SINGLE_TAP, None, channel)
    request_id = graph.request_id_of(request_row)
    inbox_rows.record_outcome(channel, item_id, PolicyDecision.HOLD, None, (), request_id)
    return request_id


class RecordingS3Client:
    """Stands in for pidge-verdict's S3 client; any call other than put_object_tagging raises AttributeError."""

    def __init__(self):
        self.tagging_calls: list[dict] = []

    def put_object_tagging(self, **arguments):
        self.tagging_calls.append(arguments)


def read_listed_number(number: str) -> dict | None:
    response = graph.get_table().get_item(Key={"PK": "LIST#address", "SK": number})
    return response["Item"] if "Item" in response else None


def deny_and_confirm(request_id: str, s3_client: RecordingS3Client) -> None:
    denied = verdict.record_answer(request_id, VerdictAnswer.NO, SENDER_ON[Channel.INAPP], f"inapp:no-{request_id}", at(0))
    assert denied.outcome is RECORDED
    confirmed = verdict.confirm_quarantine(request_id, RECEIVER_USER_ID, RECEIVER_USER_ID, RECEIVER_ADDRESS, s3_client, "test-emails")
    assert confirmed.outcome is RECORDED


@pytest.mark.parametrize(("inbound_channel", "answer_channel", "expected_outcome"), YES_GOLDENS,
                         ids=[f"{inbound}-hold-{answer}-yes" for inbound, answer, _ in YES_GOLDENS])
def test_yes_on_inbound_channel_or_its_number_sibling_is_refused(aws, inbound_channel, answer_channel, expected_outcome):  # noqa: F811
    seed_jimin()
    request_id = new_hold(inbound_channel)
    result = verdict.record_answer(request_id, VerdictAnswer.YES, SENDER_ON[answer_channel], f"{answer_channel}:yes-1", at(0))
    expected_status = RequestState.CONFIRMED_PENDING if expected_outcome is RECORDED else RequestState.PENDING
    assert (result.outcome, status_of(request_id)) == (expected_outcome, str(expected_status))
    if expected_outcome is REFUSED:  # the refusal leaves the hold answerable: the in-app Yes still lands
        in_app = verdict.record_answer(request_id, VerdictAnswer.YES, SENDER_ON[Channel.INAPP], "inapp:yes-2", at(1))
        assert (in_app.outcome, in_app.status, status_of(request_id)) == (
            RECORDED, RequestState.CONFIRMED_PENDING, str(RequestState.CONFIRMED_PENDING))
        no_hold_id = new_hold(inbound_channel, "m2")  # a No on the refused channel still keeps a hold
        refused_channel_no = verdict.record_answer(no_hold_id, VerdictAnswer.NO, SENDER_ON[answer_channel], f"{answer_channel}:no-1", at(0))
        assert (refused_channel_no.outcome, status_of(no_hold_id)) == (RECORDED, str(RequestState.DENIED))


def test_confirm_on_message_hold_quarantines_and_lists_the_number(verdict_handler, monkeypatch):  # noqa: F811
    handler, _ = verdict_handler
    seed_jimin()
    request_id = new_hold(Channel.SMS, "sms-1")
    assert verdict.record_answer(request_id, VerdictAnswer.NO, SENDER_ON[Channel.INAPP], "inapp:no-1", at(0)).outcome is RECORDED
    s3_client = RecordingS3Client()
    monkeypatch.setattr(handler, "s3_client", s3_client)
    pushed_to_users: list[tuple[str, dict]] = []

    def record_push_to_user(user_id: str, message: dict) -> int:
        pushed_to_users.append((user_id, json.loads(handler.channels.encode_push_message(message))))
        return 1

    monkeypatch.setattr(handler.channels, "push_to_user", record_push_to_user)
    send_action(handler, RECEIVER_USER_ID, {"action": "confirm", "requestId": request_id})

    assert inbox_rows.get_row(Channel.SMS, "sms#sms-1")["quarantined"] is True
    listed_row = read_listed_number(SCAM_NUMBER)
    assert (listed_row["source"], listed_row["requestId"]) == (ListSource.PIDGE_CONFIRMED, request_id)
    assert s3_client.tagging_calls == []
    assert [user_id for user_id, _ in pushed_to_users] == [RECEIVER_USER_ID, "jimin"]
    request_state = pushed_to_users[0][1]
    assert request_state["type"] == "request_state"
    assert {key: request_state["request"][key] for key in ("requestId", "emailId", "status", "contactName", "quarantined")} == {
        "requestId": request_id, "emailId": "sms#sms-1", "status": "denied", "contactName": "Jimin", "quarantined": True}
    next_text_reasons = lists.lookup_sender_reasons(lists.DynamoListStore(), SCAM_NUMBER, "", "", [])
    assert next_text_reasons == [ReasonCode.REPORTED_SENDER]  # the next text from that number reads as reported


def test_reverse_deletes_only_that_holds_listed_number(aws):  # noqa: F811
    seed_jimin()
    s3_client = RecordingS3Client()
    sms_hold_id = new_hold(Channel.SMS, "sms-1", SCAM_NUMBER)
    whatsapp_hold_id = new_hold(Channel.WHATSAPP, "wamid.B1", OTHER_SCAM_NUMBER)
    deny_and_confirm(sms_hold_id, s3_client)
    deny_and_confirm(whatsapp_hold_id, s3_client)

    reversed_answer = verdict.record_answer(sms_hold_id, VerdictAnswer.REVERSE, SENDER_ON[Channel.INAPP], "inapp:reverse-1", at(1))

    assert (reversed_answer.outcome, reversed_answer.status) == (RECORDED, RequestState.REVERSED_NO)
    assert read_listed_number(SCAM_NUMBER) is None
    assert read_listed_number(OTHER_SCAM_NUMBER)["requestId"] == whatsapp_hold_id
    assert s3_client.tagging_calls == []


def test_sync_carries_message_rows_and_the_card_shows_the_number(verdict_handler):  # noqa: F811
    handler, pushed_messages = verdict_handler
    seed_jimin()
    request_id = new_hold(Channel.SMS, "sms-1")

    send_action(handler, RECEIVER_USER_ID, {"action": "sync"})

    [sync_message] = pushed_messages
    assert sync_message["emails"] == []  # a text is never listed as an email
    [message] = sync_message["messages"]
    assert set(message) == MESSAGE_PAYLOAD_FIELDS
    assert {key: message[key] for key in MESSAGE_PAYLOAD_FIELDS - {"request"}} == {
        "messageId": "sms#sms-1", "channel": "sms", "direction": "in", "threadKey": f"sms#{SCAM_NUMBER}", "senderName": "",
        "senderAddress": SCAM_NUMBER, "textBody": NEW_PHONE_TEXT, "receivedAt": RECEIVED_AT, "state": "hold", "warnReason": None,
        "reasons": [], "quarantined": False}
    assert {key: message["request"][key] for key in ("requestId", "emailId", "contactName", "status")} == {
        "requestId": request_id, "emailId": "sms#sms-1", "contactName": "Jimin", "status": "pending"}
    graph.get_table().put_item(Item={"PK": "USER#jimin", "SK": "PROFILE", "tokenHash": "jimin-token-hash"})
    assert seed_contact_roles(RECEIVER_ADDRESS) == []
    send_action(handler, "jimin", {"action": "sync"})
    [card] = pushed_messages[-1]["requests"]
    assert (card["requestId"], card["senderAddress"], card["subject"]) == (request_id, SCAM_NUMBER, "")
