"""The wire shapes the gaps added: the reading row withholds the body, the answer carries its channel, sync lists a row the
Reader is still on, the circle's reach chips follow the stack's flags, and the scam-list coverage reaches the email state."""

import json

from shared import graph, inbox_rows, lists, payloads, sources
from shared.enums import Channel, RequestState, VerdictAnswer
from shared.reader_agent import EmailVerdict
from tests.helpers.scripted_model import ScriptedModel
from tests.test_reader_pipeline import FAMILY_HELLO, NEWSLETTER_LINK, load_reader_handler, reader_scripts_for, run_sample
from tests.test_reader_reading_state import seed_grandma_and_jimin_for_the_reader
from tests.test_sources import AMF, seed_source
from tests.test_verdict import JIMIN_WHATSAPP_NUMBER, RECEIVER_USER_ID, aws, at, new_request, verdict  # noqa: F401
from tests.test_verify_contact import send_action, verdict_handler  # noqa: F401

EMAIL_ROW = {"s3Key": "inbox/new.eml", "senderName": "Jimin", "senderAddress": "jimin@lookalike.example", "subject": "Help",
             "receivedAt": "2026-10-27T06:00:00Z", "textBody": "Send money to this account now.", "links": [{"href": "x"}]}
AMF_ROW_LIFETIME_SECONDS = 180 * 86400


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
    inbox_rows.claim_row(Channel.EMAIL, "inbox/new.eml", {
        **EMAIL_ROW, "receiverEmail": "grandma@example.com", "quarantined": False,
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


def put_amf_source(last_sync_at: str | None) -> None:
    """AMF's SOURCE# row from infra/seed/sources.json with the test's own sync stamp (None: never synced)."""
    seed = {**seed_source(AMF), "lastSyncAt": last_sync_at, "lastGeneration": None if last_sync_at is None else 1}
    graph.get_table().put_item(Item=sources.source_row_from_seed(seed))


def put_amf_listed_domain(domain: str, now: str) -> str:
    """One AMF public-feed domain row through the production row builder; returns its attribution."""
    attribution = seed_source(AMF)["attribution"]["text"]
    seed = {"kind": "domain", "value": domain, "source": "public-feed", "sourceId": AMF, "tier": 1, "category": "scam",
            "strength": "listed", "reason": "On the AMF blacklist (Usurpation) since 2026-10-08", "attribution": attribution,
            "addedAt": now}
    expires_at = lists.add_seconds_to_timestamp(now, AMF_ROW_LIFETIME_SECONDS)
    graph.get_table().put_item(Item=lists.public_list_row_from_seed(seed, 1, expires_at))
    return attribution


def test_scam_list_coverage_reaches_the_email_state(aws, monkeypatch):  # noqa: F811
    seed_grandma_and_jimin_for_the_reader()
    loaded = load_reader_handler(monkeypatch, is_escalation_enabled=False)
    loaded.handler.pipeline.reader.model = ScriptedModel(EmailVerdict, reader_scripts_for((FAMILY_HELLO, NEWSLETTER_LINK)))

    put_amf_source(None)  # never synced: stale, so even a linkless email reads unavailable through its sender domain
    hello_row = inbox_rows.get_row(Channel.EMAIL, run_sample(loaded, monkeypatch, FAMILY_HELLO.sample_file_name))
    assert (hello_row["scamListCoverage"], hello_row["state"], hello_row["reasons"]) == ("unavailable", "pass", [])
    assert [email["scamListCoverage"] for email in loaded.pushed_emails] == [None, "unavailable"]  # reading, then the verdict
    assert loaded.pushed_emails[-1]["state"] == "pass"

    now = graph.now_timestamp()
    put_amf_source(now)
    attribution = put_amf_listed_domain("zelle-pay-help.example", now)
    newsletter_row = inbox_rows.get_row(Channel.EMAIL, run_sample(loaded, monkeypatch, NEWSLETTER_LINK.sample_file_name))
    assert (newsletter_row["scamListCoverage"], newsletter_row["state"], newsletter_row["warnReason"]) == ("complete", "warn", "link_reasons")
    assert newsletter_row["reasons"] == ["link_text_href_mismatch", "link_listed"]
    assert (newsletter_row["links"][0]["reasons"], newsletter_row["links"][0]["listedBy"]) == (
        ["link_text_href_mismatch", "link_listed"], attribution)
    pushed_state = loaded.pushed_emails[-1]
    assert (pushed_state["scamListCoverage"], pushed_state["reasons"], pushed_state["links"]) == (
        "complete", ["link_text_href_mismatch", "link_listed"], newsletter_row["links"])

    pre_sx3_row = {**EMAIL_ROW, "state": "pass"}
    assert payloads.email_for_receiver(pre_sx3_row, None, None, None)["scamListCoverage"] is None
