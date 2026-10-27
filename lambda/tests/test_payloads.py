"""The wire shapes the gaps added: the reading row withholds the body, the answer carries its channel, sync lists a row the
Reader is still on, and the circle's reach chips follow the stack's flags."""

import json

from shared import graph, payloads
from shared.enums import Channel, RequestState, VerdictAnswer
from tests.test_verdict import JIMIN_WHATSAPP_NUMBER, RECEIVER_USER_ID, aws, at, new_request, verdict  # noqa: F401
from tests.test_verify_contact import send_action, verdict_handler  # noqa: F401

EMAIL_ROW = {"s3Key": "inbox/new.eml", "senderName": "Jimin", "senderAddress": "jimin@lookalike.example", "subject": "Help",
             "receivedAt": "2026-10-27T06:00:00Z", "textBody": "Send money to this account now.", "links": [{"href": "x"}]}


def test_the_reading_row_has_no_state_no_body_and_no_links():
    reading_row = payloads.reading_email_for_receiver(EMAIL_ROW)
    assert (reading_row["emailId"], reading_row["senderName"], reading_row["subject"]) == ("inbox/new.eml", "Jimin", "Help")
    assert (reading_row["state"], reading_row["textBody"], reading_row["links"], reading_row["request"]) == (None, "", [], None)
    assert payloads.email_reading_message(EMAIL_ROW)["type"] == "email_state"


def test_the_answer_carries_the_channel_it_came_on(aws):
    request_id = new_request()
    whatsapp_sender = verdict.SenderIdentity(Channel.WHATSAPP, JIMIN_WHATSAPP_NUMBER)
    result = verdict.record_answer(request_id, VerdictAnswer.NO, whatsapp_sender, "whatsapp:wamid.1", at(0))
    message = payloads.request_state_message(result.request_row, RequestState.DENIED, "Jimin", False)
    assert message["request"]["repliedVia"] == "whatsapp"
    assert payloads.request_for_receiver(result.request_row, "Jimin", RequestState.DENIED)["repliedVia"] == "whatsapp"
    unanswered = payloads.request_for_receiver(graph.get_request(new_request()), "Jimin", RequestState.PENDING)
    assert unanswered["repliedVia"] is None


def test_sync_lists_a_row_the_reader_is_still_on_without_its_body(verdict_handler):
    handler, pushed_messages = verdict_handler
    graph.claim_email("inbox/new.eml", {**EMAIL_ROW, "receiverEmail": "grandma@example.com", "quarantined": False,
                                        graph.USER_INDEX_PARTITION_KEY: graph.user_index_partition(RECEIVER_USER_ID, graph.EMAIL_ROW_KIND),
                                        graph.USER_INDEX_SORT_KEY: EMAIL_ROW["receivedAt"]})
    send_action(handler, RECEIVER_USER_ID, {"action": "sync"})
    [sync_message] = pushed_messages
    [reading_row] = [email for email in sync_message["emails"] if email["emailId"] == "inbox/new.eml"]
    assert (reading_row["state"], reading_row["textBody"], reading_row["links"]) == (None, "", [])
    assert "Send money" not in json.dumps(sync_message)


def test_a_reach_chip_lights_only_when_the_row_has_the_channel_and_the_stack_sends_on_it():
    contact_row = {"SK": "CONTACT#jimin", "contactPwaUserId": "jimin", "contactName": "Jimin", "verifiedVia": "seed",
                   "contactRcs": "+821012345678", "contactWhatsApp": "+821012345678", "contactEmail": "jimin@example.com"}
    reach_with = lambda flags: payloads.circle_entry_for_receiver(contact_row, flags)["reach"]  # noqa: E731
    assert reach_with(payloads.ReachFlags(False, True, True)) == {"inapp": True, "rcs": False, "whatsapp": True, "email": True}
    assert reach_with(payloads.ReachFlags(True, True, True))["rcs"] is True
    assert reach_with(payloads.ReachFlags(True, False, False)) == {"inapp": True, "rcs": True, "whatsapp": False, "email": False}
    no_number_row = {key: value for key, value in contact_row.items() if key not in ("contactRcs", "contactWhatsApp", "contactEmail")}
    assert reach_with(payloads.ReachFlags(True, True, True)) != payloads.circle_entry_for_receiver(no_number_row, payloads.ReachFlags(True, True, True))["reach"]
    assert payloads.circle_entry_for_receiver(no_number_row, payloads.ReachFlags(True, True, True))["reach"] == {"inapp": True, "rcs": False, "whatsapp": False, "email": False}
