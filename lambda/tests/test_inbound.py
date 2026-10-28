"""Texts and media sent to Pidge's WhatsApp, RCS and SMS numbers, through the production pidge_inbound handler and the real
pipeline on moto with a scripted Reader: MESSAGE# rows of the messenger resident, gate 1 on numbers only Pidge checked, the
Reader skipped for media, a waiting contact's code left to verification, and a broken or foreign record raising unwritten."""

import importlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from shared import circle, graph, inbox_rows
from shared.enums import Channel, GateFired, Intent, Relationship
from shared.profiles import UnknownReceiverError
from shared.reader_agent import EmailVerdict
from tests.helpers.profiles import seed_resident
from tests.helpers.scripted_model import ScriptedModel, ScriptedStep, verdict_step
from tests.test_inbound_texts import (
    IMAGE, JIMIN_DIGITS, JIMIN_NUMBER, NEW_PHONE_SMS_TEXT, PIDGE_SMS_NUMBER, REACTION, REVERSE_BUTTON_TAP, SCAMMER_SMS_NUMBER,
    SENT_AT, SENT_AT_EPOCH_SECONDS, SMS_RECORD, STATUSES_ONLY, TEMPLATE_TAP, change_value_with, profile_contact, typed_text,
)
from tests.test_rcs import sns_message as rcs_sns_message
from tests.test_reader_pipeline import JIMIN_MONEY_ASK
from tests.test_reader_receiver import rows_with_partition_prefix
from tests.test_verdict import aws  # noqa: F401
from tests.test_whatsapp import sns_message as whatsapp_sns_message

GRANDMA_ADDRESS = "grandma@sendpidge.example"  # MESSENGER_RESIDENT_ADDRESS
WHATSAPP_TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:whatsapp"
RCS_TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:rcs"
SMS_TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:sms"
STRANGER_DIGITS = "447700900123"
JIMINS_PENDING_DIGITS = "821033334444"  # on Jimin's row, still waiting for its code
WAITING_NUMBER = "+821099998888"  # Minho's, added from the app: a live WACODE# row waits on it
HELLO_TEXT = "Hi Grandma, see you on Sunday"
NO_ASK_FROM_JIMIN = {"claimed_person": "Jimin", "intent": Intent.NONE, "confidence": 0.9, "summary": "Says hello."}


@dataclass
class LoadedInboundHandler:
    handler: object
    pushes: list[tuple[str, dict]] = field(default_factory=list)  # (user id, message) for every push, the asks included
    decided_gates: list[GateFired | None] = field(default_factory=list)  # one per decided item, from the production decide()
    reader_model: ScriptedModel = field(default_factory=lambda: ScriptedModel(EmailVerdict, {}))

    def use_reader_scripts(self, scripts: dict[str, list[ScriptedStep]]) -> None:
        self.reader_model = ScriptedModel(EmailVerdict, scripts)
        self.handler.pipeline.reader.model = self.reader_model

    def handle(self, topic_arn: str, sns_message: dict) -> None:
        self.handler.handle({"Records": [{"EventSource": "aws:sns", "Sns": {"TopicArn": topic_arn, "Message": json.dumps(sns_message)}}]},
                            None)

    def pushes_of_type(self, user_id: str, push_type: str) -> list[dict]:
        return [message for pushed_user_id, message in self.pushes if pushed_user_id == user_id and message["type"] == push_type]


def load_inbound_handler(monkeypatch, sms_topic_arn: str = SMS_TOPIC_ARN) -> LoadedInboundHandler:
    """pidge_inbound.handler and its shared.pipeline re-imported under the test environment; every push recorded, asks go out
    in the app only (the real ask_in_app), the Reader answers only from a script."""
    environment = {"READER_MODEL": "haiku-4.5", "ESCALATION_MODEL": "opus-4.5", "ESCALATION_ENABLED": "false",
                   "RETRY_SCHEDULER_ENABLED": "false", "ASKS_PER_CONTACT_PER_HOUR": "3", "WHATSAPP_TOPIC_ARN": WHATSAPP_TOPIC_ARN,
                   "RCS_TOPIC_ARN": RCS_TOPIC_ARN, "SMS_TOPIC_ARN": sms_topic_arn, "SMS_ORIGINATION_NUMBER": PIDGE_SMS_NUMBER,
                   "MESSENGER_RESIDENT_ADDRESS": GRANDMA_ADDRESS}
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    handler = importlib.import_module("pidge_inbound.handler")
    importlib.reload(handler.pipeline)
    importlib.reload(handler)
    loaded = LoadedInboundHandler(handler)
    handler.pipeline.reader.model = loaded.reader_model  # no script: any Reader call is recorded and refused
    for flag_name in ("RCS_ENABLED", "WHATSAPP_ENABLED", "EMAIL_ASK_ENABLED"):
        monkeypatch.setattr(handler.pipeline.channels, flag_name, False)
    monkeypatch.setattr(handler.pipeline.channels, "push_to_user", lambda user_id, message: loaded.pushes.append((user_id, message)) or 1)
    production_decide = handler.pipeline.decide
    def decide_and_record_gate(*arguments):
        outcome = production_decide(*arguments)
        loaded.decided_gates.append(outcome.gate)
        return outcome
    monkeypatch.setattr(handler.pipeline, "decide", decide_and_record_gate)
    return loaded


@pytest.fixture
def inbound_handler(aws, monkeypatch):  # noqa: F811
    return load_inbound_handler(monkeypatch)


def seed_grandma_and_jimin() -> None:
    """Grandma, the messenger resident, with Jimin: one number checked for WhatsApp and RCS, one still pending, and an email."""
    seed_resident(GRANDMA_ADDRESS, "grandma", "Grandma")
    graph.get_table().put_item(Item={
        "PK": f"RECEIVER#{GRANDMA_ADDRESS}", "SK": "CONTACT#jimin", "contactName": "Jimin", "contactPwaUserId": "jimin",
        "verifiedVia": "seed", "contactReachPoints": [
            {"channel": "whatsapp", "address": JIMIN_NUMBER, "verifiedVia": "seed"},
            {"channel": "rcs", "address": JIMIN_NUMBER, "verifiedVia": "seed"},
            {"channel": "whatsapp", "address": f"+{JIMINS_PENDING_DIGITS}", "verifiedVia": "pending"},
            {"channel": "email", "address": "jimin@example.com"},
        ]})


def whatsapp_text_record(message_id: str, body: str, sender_digits: str, profile_name: str | None,
                         timestamp: str = SENT_AT_EPOCH_SECONDS) -> dict:
    contacts = [profile_contact(profile_name, sender_digits)] if profile_name is not None else None
    return whatsapp_sns_message(change_value_with(typed_text(message_id, body, sender_digits, timestamp), contacts=contacts))


def message_row(channel: Channel, provider_message_id: str) -> dict | None:
    return inbox_rows.get_row(channel, inbox_rows.message_item_id(channel, provider_message_id))


def thread_row(thread_key: str) -> dict | None:
    return graph.get_table().get_item(Key={"PK": f"RECEIVER#{GRANDMA_ADDRESS}", "SK": f"THREAD#{thread_key}"}).get("Item")


def was_reader_called(loaded: LoadedInboundHandler) -> bool:
    """A refused call (no script matched) raises before it is recorded, so both lists count."""
    return loaded.reader_model.recorded_calls != [] or loaded.reader_model.raised_errors != []


def assert_nothing_written(loaded: LoadedInboundHandler) -> None:
    assert rows_with_partition_prefix("MESSAGE#") == [] and rows_with_partition_prefix("REQUEST#") == []
    assert loaded.pushes == [] and not was_reader_called(loaded)


def test_new_phone_sms_from_unknown_number_holds_and_asks_jimin(inbound_handler):
    seed_grandma_and_jimin()
    inbound_handler.use_reader_scripts({NEW_PHONE_SMS_TEXT: [verdict_step(JIMIN_MONEY_ASK)]})
    inbound_handler.handle(SMS_TOPIC_ARN, SMS_RECORD)
    row = message_row(Channel.SMS, "sms-1")
    assert (row["state"], row["reasons"], row["senderName"], row["senderAddress"], row["textBody"]) == (
        "hold", [], "", SCAMMER_SMS_NUMBER, NEW_PHONE_SMS_TEXT)
    assert (row["channel"], row["direction"], row["threadKey"], row["providerMessageId"], row["userIndexPK"]) == (
        "sms", "in", f"sms#{SCAMMER_SMS_NUMBER}", "sms-1", "grandma#MESSAGE")
    request_row = graph.get_request(row["requestId"])
    assert (request_row["inboundChannel"], request_row["emailId"], request_row["contactSK"], request_row["friction"]) == (
        "sms", "sms#sms-1", "CONTACT#jimin", "single_tap")
    grandmas_pushes = inbound_handler.pushes_of_type("grandma", "message_state")
    assert [(push["message"]["messageId"], push["message"]["state"]) for push in grandmas_pushes] == [
        ("sms#sms-1", None), ("sms#sms-1", "hold")]  # reading, then the verdict
    assert grandmas_pushes[-1]["message"]["request"]["contactName"] == "Jimin"
    [ask] = inbound_handler.pushes_of_type("jimin", "verification_card")
    assert (ask["request"]["requestId"], ask["request"]["senderAddress"]) == (row["requestId"], SCAMMER_SMS_NUMBER)
    assert inbound_handler.decided_gates == [GateFired.GATE_2] and inbound_handler.reader_model.raised_errors == []
    assert thread_row(f"sms#{SCAMMER_SMS_NUMBER}") is None  # only WhatsApp stamps a reply window


def test_profile_name_from_unknown_whatsapp_number_is_gate_1(inbound_handler):
    seed_grandma_and_jimin()
    inbound_handler.use_reader_scripts({HELLO_TEXT: [verdict_step(NO_ASK_FROM_JIMIN)]})
    inbound_handler.handle(WHATSAPP_TOPIC_ARN, whatsapp_text_record("wamid.G1", HELLO_TEXT, STRANGER_DIGITS, "Jimin"))
    row = message_row(Channel.WHATSAPP, "wamid.G1")
    assert (row["state"], row["reasons"], row["senderName"]) == ("hold", ["unknown_address"], "")
    assert inbound_handler.decided_gates == [GateFired.GATE_1]
    assert graph.get_request(row["requestId"])["inboundChannel"] == "whatsapp"


def test_jimins_checked_number_is_not_gate_1(inbound_handler):
    seed_grandma_and_jimin()
    inbound_handler.use_reader_scripts({HELLO_TEXT: [verdict_step(NO_ASK_FROM_JIMIN), verdict_step(NO_ASK_FROM_JIMIN)]})
    inbound_handler.handle(WHATSAPP_TOPIC_ARN, whatsapp_text_record("wamid.J1", HELLO_TEXT, JIMIN_DIGITS, "Jimin"))
    inbound_handler.handle(WHATSAPP_TOPIC_ARN, whatsapp_text_record("wamid.J2", HELLO_TEXT, JIMINS_PENDING_DIGITS, "Jimin"))
    checked_row, pending_row = message_row(Channel.WHATSAPP, "wamid.J1"), message_row(Channel.WHATSAPP, "wamid.J2")
    assert (checked_row["state"], checked_row["reasons"], checked_row["senderName"], checked_row["requestId"]) == ("pass", [], "Jimin", None)
    assert (pending_row["state"], pending_row["reasons"], pending_row["senderName"]) == ("hold", ["unknown_address"], "")
    assert inbound_handler.decided_gates == [None, GateFired.GATE_1]


def test_redelivered_record_writes_one_row_and_reads_once(inbound_handler):
    seed_grandma_and_jimin()
    inbound_handler.use_reader_scripts({NEW_PHONE_SMS_TEXT: [verdict_step(JIMIN_MONEY_ASK)]})
    inbound_handler.handle(SMS_TOPIC_ARN, SMS_RECORD)
    inbound_handler.handle(SMS_TOPIC_ARN, SMS_RECORD)  # SNS delivers at least once
    assert len(rows_with_partition_prefix("MESSAGE#")) == 1 and len(rows_with_partition_prefix("REQUEST#")) == 1
    assert len(inbound_handler.reader_model.recorded_calls) == 1 and inbound_handler.reader_model.raised_errors == []
    assert len(inbound_handler.pushes_of_type("jimin", "verification_card")) == 1


def test_taps_and_statuses_write_nothing_and_do_not_raise(inbound_handler):
    seed_grandma_and_jimin()
    for change_value in (change_value_with(TEMPLATE_TAP), change_value_with(REVERSE_BUTTON_TAP), STATUSES_ONLY, change_value_with(REACTION)):
        inbound_handler.handle(WHATSAPP_TOPIC_ARN, whatsapp_sns_message(change_value))
    inbound_handler.handle(RCS_TOPIC_ARN, rcs_sns_message({"type": "SUGGESTION", "text": "No, not me", "postbackData": "req-1:no"}))
    assert_nothing_written(inbound_handler)
    assert thread_row(f"whatsapp#{JIMIN_NUMBER}") is None
    inbound_handler.use_reader_scripts({HELLO_TEXT: [verdict_step(NO_ASK_FROM_JIMIN)]})
    tap_and_text = change_value_with(TEMPLATE_TAP, typed_text("wamid.T9", HELLO_TEXT))  # one record: the tap skipped, the text read
    inbound_handler.handle(WHATSAPP_TOPIC_ARN, whatsapp_sns_message(tap_and_text))
    assert [row["PK"] for row in rows_with_partition_prefix("MESSAGE#")] == ["MESSAGE#whatsapp#wamid.T9"]


def test_code_from_a_waiting_number_is_left_to_verification(inbound_handler):
    seed_grandma_and_jimin()
    minho = circle.ContactForm("Minho", Relationship.SON, (WAITING_NUMBER,), (), ("minho@example.com",))
    assert circle.enrol_contact(GRANDMA_ADDRESS, minho, datetime.now(timezone.utc)).code is not None  # writes WACODE#<number>
    inbound_handler.use_reader_scripts({"Hello Grandma": [verdict_step(NO_ASK_FROM_JIMIN)], "123456": [verdict_step(NO_ASK_FROM_JIMIN)]})
    waiting_digits = WAITING_NUMBER.removeprefix("+")
    inbound_handler.handle(WHATSAPP_TOPIC_ARN, whatsapp_text_record("wamid.C1", " 123456 ", waiting_digits, None))
    assert message_row(Channel.WHATSAPP, "wamid.C1") is None and not was_reader_called(inbound_handler)
    inbound_handler.handle(WHATSAPP_TOPIC_ARN, whatsapp_text_record("wamid.C2", "Hello Grandma", waiting_digits, None))
    inbound_handler.handle(WHATSAPP_TOPIC_ARN, whatsapp_text_record("wamid.C3", "123456", JIMIN_DIGITS, None))  # no code waits here
    assert message_row(Channel.WHATSAPP, "wamid.C2")["state"] == "pass"
    assert message_row(Channel.WHATSAPP, "wamid.C3")["state"] == "pass"


def test_media_is_warn_unreadable_without_a_reader_call(inbound_handler):
    seed_grandma_and_jimin()
    stranger_image = {**IMAGE, "id": "wamid.M1", "from": STRANGER_DIGITS}
    inbound_handler.handle(WHATSAPP_TOPIC_ARN, whatsapp_sns_message(change_value_with(stranger_image)))
    inbound_handler.handle(WHATSAPP_TOPIC_ARN, whatsapp_sns_message(change_value_with({**IMAGE, "id": "wamid.M2"})))  # Jimin's number
    named_image = {**IMAGE, "id": "wamid.M3", "from": STRANGER_DIGITS}
    inbound_handler.handle(WHATSAPP_TOPIC_ARN, whatsapp_sns_message(change_value_with(
        named_image, contacts=[profile_contact("Jimin", STRANGER_DIGITS)])))
    for provider_message_id in ("wamid.M1", "wamid.M2"):
        row = message_row(Channel.WHATSAPP, provider_message_id)
        assert (row["state"], row["warnReason"], row["textBody"], row["requestId"]) == ("warn", "unreadable", "", None)
    named_row = message_row(Channel.WHATSAPP, "wamid.M3")
    assert (named_row["state"], named_row["reasons"]) == ("hold", ["unknown_address"])  # gate 1 holds what nobody could read
    assert not was_reader_called(inbound_handler)
    assert inbound_handler.decided_gates == [None, None, GateFired.GATE_1]


def test_unknown_topic_raises_and_writes_nothing(aws, monkeypatch):  # noqa: F811
    loaded = load_inbound_handler(monkeypatch)
    seed_grandma_and_jimin()
    with pytest.raises(ValueError):
        loaded.handle("arn:aws:sns:us-east-1:123456789012:another-topic", whatsapp_text_record("wamid.X1", HELLO_TEXT, JIMIN_DIGITS, None))
    with pytest.raises(ValueError):
        loaded.handle(SMS_TOPIC_ARN, {**SMS_RECORD, "destinationNumber": "+18885550000"})  # an SMS to a number that is not Pidge's
    sms_off = load_inbound_handler(monkeypatch, sms_topic_arn="")
    with pytest.raises(ValueError):
        sms_off.handle("", SMS_RECORD)  # with SMS off no topic, not even an empty one, reads as SMS
    assert_nothing_written(loaded)
    assert_nothing_written(sms_off)


def test_unknown_messenger_resident_raises(inbound_handler):
    with pytest.raises(UnknownReceiverError):
        inbound_handler.handle(SMS_TOPIC_ARN, SMS_RECORD)
    assert_nothing_written(inbound_handler)


def test_whatsapp_text_opens_the_thread_window(inbound_handler):
    seed_grandma_and_jimin()
    inbound_handler.use_reader_scripts({HELLO_TEXT: [verdict_step(NO_ASK_FROM_JIMIN)] * 3})
    inbound_handler.handle(WHATSAPP_TOPIC_ARN, whatsapp_text_record("wamid.T1", HELLO_TEXT, JIMIN_DIGITS, "Jimin"))
    assert thread_row(f"whatsapp#{JIMIN_NUMBER}")["lastInboundAt"] == SENT_AT
    inbound_handler.handle(WHATSAPP_TOPIC_ARN, whatsapp_text_record("wamid.T0", HELLO_TEXT, JIMIN_DIGITS, "Jimin", "1793055600"))
    assert thread_row(f"whatsapp#{JIMIN_NUMBER}")["lastInboundAt"] == SENT_AT  # sent an hour earlier, delivered late: kept
    inbound_handler.handle(WHATSAPP_TOPIC_ARN, whatsapp_text_record("wamid.T2", HELLO_TEXT, JIMIN_DIGITS, "Jimin", "1793062800"))
    assert thread_row(f"whatsapp#{JIMIN_NUMBER}")["lastInboundAt"] == "2026-10-27T01:00:00Z"
    assert len(rows_with_partition_prefix("MESSAGE#")) == 3
