"""The keypad answer on Pidge's own call: production pidge_verdict.handler.handle on moto with the documented Amazon Connect
ContactFlowEvent shape. Only our instance, only the ContactId Pidge placed, only a checked RCS number and only keys 1 and 2
count; a Connect retry says recorded once; a table failure is counted and raised."""

import json

import pytest
from botocore.exceptions import ClientError

from pidge_verdict.voice_answers import VOICE_ANSWER_FAILED_METRIC_NAME, VoiceAnswerError
from shared import graph, verdict
from shared.enums import Channel, VerdictAnswer
from tests.test_message_verdict import JIMIN_EMAIL, JIMIN_NUMBER, SENDER_ON, new_hold, seed_jimin
from tests.test_verdict import RECEIVER_USER_ID, at, aws  # noqa: F401
from tests.test_verify_contact import verdict_handler  # noqa: F401

INSTANCE_ARN = "arn:aws:connect:ap-northeast-2:358148451054:instance/f6343b6c-43fc-4d43-b255-c54d8b0fb66a"
FOREIGN_INSTANCE_ARN = "arn:aws:connect:ap-northeast-2:111122223333:instance/00000000-0000-0000-0000-000000000000"
PIDGES_CONTACT_ID = "contact-1"
UNCHECKED_NUMBER = "+821033334444"
ASK_PROMPT = "Hello, this is Pidge, calling for Grandma. Grandma just got an email in your name."


class RecordingSesClient:
    """Stands in for pidge-verdict's SES client; each receipt is one send_email call."""

    def __init__(self):
        self.sent_emails: list[dict] = []

    def send_email(self, **arguments):
        self.sent_emails.append(arguments)
        return {"MessageId": f"receipt-{len(self.sent_emails)}"}


@pytest.fixture
def voice_handler(verdict_handler, monkeypatch):  # noqa: F811
    """pidge-verdict with voice on, Jimin's checked numbers seeded, pushes, receipts and release schedules recorded."""
    handler, _pushed_messages = verdict_handler
    monkeypatch.setenv("VOICE_ENABLED", "true")
    monkeypatch.setenv("VOICE_INSTANCE_ARN", INSTANCE_ARN)
    seed_jimin()
    recorded = {"pushes": [], "schedules": []}
    monkeypatch.setattr(handler.channels, "push_to_user", lambda user_id, message: recorded["pushes"].append((user_id, message)) or 1)
    ses_client = RecordingSesClient()
    monkeypatch.setattr(handler, "ses_client", ses_client)
    recorded["receipts"] = ses_client.sent_emails
    monkeypatch.setattr(handler, "RELEASE_SCHEDULER_ENABLED", True)
    monkeypatch.setattr(handler.schedules, "schedule_one_time_invocation",
                        lambda name, _fire_at, _function_arn, payload: recorded["schedules"].append((name, payload)))
    return handler, recorded


def hold_called_on(channel: Channel, provider_message_id: str = "m1", contact_id: str | None = PIDGES_CONTACT_ID) -> str:
    """A hold whose REQUEST# row names the call Pidge placed (None: no call was placed)."""
    request_id = new_hold(channel, provider_message_id)
    if contact_id is not None:
        graph.get_table().update_item(
            Key={"PK": f"REQUEST#{request_id}", "SK": graph.METADATA_SORT_KEY},
            UpdateExpression="SET voiceContactId = :contactId", ExpressionAttributeValues={":contactId": contact_id},
        )
    return request_id


def flow_event(request_id: str, keypad_digit: str | None = "2", contact_id: str = PIDGES_CONTACT_ID,
               customer_number: str = JIMIN_NUMBER, instance_arn: str = INSTANCE_ARN, voice_step: str = "record_answer") -> dict:
    """The Invoke-Lambda event Connect sends after the keypad block (None: the flow set no keypadDigit)."""
    attributes = {"requestId": request_id, "askPrompt": ASK_PROMPT}
    if keypad_digit is not None:
        attributes["keypadDigit"] = keypad_digit
    return {"Name": "ContactFlowEvent", "Details": {
        "ContactData": {"ContactId": contact_id, "InitialContactId": contact_id, "Channel": "VOICE", "InstanceARN": instance_arn,
                        "CustomerEndpoint": {"Address": customer_number, "Type": "TELEPHONE_NUMBER"}, "Attributes": attributes},
        "Parameters": {"voiceStep": voice_step}}}


def outcome(reason: str) -> dict:
    return {"voiceOutcome": reason}


def test_keypad_2_records_no_and_tells_both_sides(voice_handler):
    handler, recorded = voice_handler
    request_id = hold_called_on(Channel.EMAIL)

    assert handler.handle(flow_event(request_id, "2"), None) == outcome("recorded")

    request_row = graph.get_request(request_id)
    assert (request_row["status"], request_row["repliedVia"], request_row["inboundMessageIds"]) == (
        "denied", "voice", ["connect:contact-1"])
    assert [user_id for user_id, _ in recorded["pushes"]] == [RECEIVER_USER_ID, "jimin"]
    assert [message["request"]["status"] for _, message in recorded["pushes"]] == ["denied", "denied"]
    [receipt] = recorded["receipts"]
    assert receipt["Destination"] == {"ToAddresses": [JIMIN_EMAIL]}


def test_keypad_1_on_email_hold_books_the_release(voice_handler):
    handler, recorded = voice_handler
    request_id = hold_called_on(Channel.EMAIL)

    assert handler.handle(flow_event(request_id, "1"), None) == outcome("recorded")

    assert graph.get_request(request_id)["status"] == "confirmed_pending"
    assert recorded["schedules"] == [(f"release-{request_id}", {"requestId": request_id})]
    assert recorded["receipts"] == []  # the receipt follows the release


def test_keypad_1_on_sms_hold_is_wrong_channel(voice_handler):
    handler, recorded = voice_handler
    request_id = hold_called_on(Channel.SMS, "sms-1")

    assert handler.handle(flow_event(request_id, "1"), None) == outcome("wrong_channel")

    assert graph.get_request(request_id)["status"] == "pending"
    assert (recorded["pushes"], recorded["schedules"]) == ([], [])


def test_foreign_instance_writes_nothing(voice_handler):
    handler, recorded = voice_handler
    request_id = hold_called_on(Channel.EMAIL)
    row_before = graph.get_request(request_id)

    assert handler.handle(flow_event(request_id, instance_arn=FOREIGN_INSTANCE_ARN), None) == outcome("bad_message")

    assert graph.get_request(request_id) == row_before
    assert recorded["pushes"] == []


def test_contact_id_not_pidges_call_is_refused(voice_handler):
    handler, recorded = voice_handler
    called_hold_id = hold_called_on(Channel.EMAIL)
    uncalled_hold_id = hold_called_on(Channel.WHATSAPP, "wamid.A1", contact_id=None)
    rows_before = [graph.get_request(called_hold_id), graph.get_request(uncalled_hold_id)]

    assert handler.handle(flow_event(called_hold_id, contact_id="contact-2"), None) == outcome("wrong_sender")
    assert handler.handle(flow_event(uncalled_hold_id), None) == outcome("wrong_sender")

    assert [graph.get_request(called_hold_id), graph.get_request(uncalled_hold_id)] == rows_before
    assert recorded["pushes"] == []


def test_number_not_a_checked_point_is_refused(voice_handler):
    handler, recorded = voice_handler
    request_id = hold_called_on(Channel.EMAIL)
    row_before = graph.get_request(request_id)

    assert handler.handle(flow_event(request_id, customer_number=UNCHECKED_NUMBER), None) == outcome("wrong_sender")

    assert graph.get_request(request_id) == row_before
    assert recorded["pushes"] == []


@pytest.mark.parametrize("event_fields", [{"keypad_digit": "3"}, {"keypad_digit": None}, {"voice_step": "load_facts"}],
                         ids=["key-3", "no-key", "load-facts-step"])
def test_other_digits_or_unknown_step_record_nothing(voice_handler, event_fields):
    handler, recorded = voice_handler
    request_id = hold_called_on(Channel.EMAIL)
    row_before = graph.get_request(request_id)

    assert handler.handle(flow_event(request_id, **event_fields), None) == outcome("bad_message")

    assert graph.get_request(request_id) == row_before
    assert recorded["pushes"] == []


def voice_answer_failure_count(capsys) -> int:
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    return sum(1 for record in records if VOICE_ANSWER_FAILED_METRIC_NAME in record)


def test_contract_refusals_are_counted_but_a_late_key_is_not(voice_handler, capsys):
    handler, recorded = voice_handler
    request_id = hold_called_on(Channel.EMAIL)
    capsys.readouterr()

    assert handler.handle(flow_event(request_id, instance_arn=FOREIGN_INSTANCE_ARN), None) == outcome("bad_message")
    assert handler.handle(flow_event(request_id, contact_id="contact-2"), None) == outcome("wrong_sender")
    assert voice_answer_failure_count(capsys) == 2  # only a broken flow contract gets here: alarmed

    verdict.record_answer(request_id, VerdictAnswer.NO, SENDER_ON[Channel.INAPP], "inapp:no-1", at(0))  # Jimin's No came first
    assert handler.handle(flow_event(request_id, "1"), None) == outcome("not_applied")  # never "recorded" for a key that missed
    assert voice_answer_failure_count(capsys) == 0
    assert recorded["pushes"] == []


def test_connect_retry_says_recorded_once(voice_handler):
    handler, recorded = voice_handler
    request_id = hold_called_on(Channel.EMAIL)
    event = flow_event(request_id, "2")

    assert handler.handle(event, None) == outcome("recorded")
    assert handler.handle(event, None) == outcome("recorded")  # Connect invoked again after the write landed

    assert graph.get_request(request_id)["inboundMessageIds"] == ["connect:contact-1"]
    assert len(recorded["pushes"]) == 2  # one announce: Grandma's PWA and Jimin's
    assert len(recorded["receipts"]) == 1


def test_table_failure_raises_and_counts(voice_handler, monkeypatch, capsys, caplog):
    handler, recorded = voice_handler
    request_id = hold_called_on(Channel.EMAIL)
    capsys.readouterr()

    def fail_to_read_request(_request_id: str):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": f"throttled for {JIMIN_NUMBER}"}},
                          "GetItem")

    monkeypatch.setattr(graph, "get_request", fail_to_read_request)
    with pytest.raises(VoiceAnswerError) as raised:
        handler.handle(flow_event(request_id), None)

    assert JIMIN_NUMBER not in str(raised.value)  # the runtime logs a raised error's message

    [metric_line] = [line for line in capsys.readouterr().out.splitlines() if VOICE_ANSWER_FAILED_METRIC_NAME in line]
    assert json.loads(metric_line)[VOICE_ANSWER_FAILED_METRIC_NAME] == 1
    assert JIMIN_NUMBER not in caplog.text  # the exception class is logged, never the boto message
    assert recorded["pushes"] == []


def test_voice_off_refuses(voice_handler, monkeypatch):
    handler, recorded = voice_handler
    monkeypatch.setenv("VOICE_ENABLED", "false")
    request_id = hold_called_on(Channel.EMAIL)
    row_before = graph.get_request(request_id)

    assert handler.handle(flow_event(request_id), None) == outcome("not_allowed")

    assert graph.get_request(request_id) == row_before
    assert recorded["pushes"] == []
