"""A home (org) in the kernel: the office's contact identity org-<orgId> is answered and reached through the org's admins only,
an admin's org comes from their own PROFILE and ADMIN# rows (never the body), a name variant is held by gate 1, gate 1 reads
the org's live verified addresses, a former home's office is neither asked nor answers, the ask cap counts each resident of
the office apart, and an org action is refused to anyone but an admin of the member's own org."""

import importlib
import json
import logging
from dataclasses import replace

import boto3

from pidge_org import handler as org_actions_handler
from shared import channels, graph, hourly_counters, inbox_rows, orgs, verdict
from shared.enums import ActionOutcome, Channel, Friction, Intent, PolicyDecision, ReasonCode, RequestState, VerdictAnswer
from shared.org_ids import org_contact_sort_key_for, org_user_id_for
from shared.reader_agent import EmailVerdict
from tests.helpers.orgs import seed_org_contact, seed_test_org
from tests.helpers.profiles import seed_contact_roles, seed_resident
from tests.helpers.scripted_model import ScriptedModel, verdict_step
from tests.test_cross_resident import read_profile_rejection_reasons
from tests.test_reader_reading_state import inbound_event
from tests.test_reader_receiver import BUCKET_NAME, reader_with_bucket  # noqa: F401
from tests.test_retry import FakeContext
from tests.test_roles import put_profile
from tests.test_seed_org import read_all_rows
from tests.test_verdict import BUCKET_NAME as VERDICT_BUCKET_NAME, aws  # noqa: F401
from tests.test_verify_contact import seed_circle_contact, send_action, verdict_handler_without_profiles  # noqa: F401

SUNRISE_ORG_ID = "sunrise"
MAPLE_ORG_ID = "maple"
GRANDMA_ADDRESS = "grandma@example.com"
BOB_ADDRESS = "bob@example.com"
SUNRISE_NAME_VARIANT = "Sunrise Commons Billing"
BILLING_LOOKALIKE_FROM = f"{SUNRISE_NAME_VARIANT} <billing@sunrise-help.example>"
STATEMENT_SUBJECT = "Your October statement"
OFFICE_VERDICT_FIELDS = {"claimed_person": "Sunrise Commons", "intent": "money", "confidence": 0.95, "summary": "Asks to pay an invoice."}
STATEMENT_NO_ASK = {"claimed_person": "Sunrise Commons", "intent": Intent.NONE, "confidence": 0.9, "summary": "A monthly statement."}
SUNRISE_OFFICE_ADDRESS = "office@sunrise.example"
SUNRISE_BILLING_ADDRESS = "billing@sunrise.example"
INVOICE_SUBJECT = "Your October invoice"
OFFICE_SORT_KEY = "CONTACT#org-sunrise"


def seed_sunrise_and_maple() -> None:
    """Grandma lives at Sunrise Commons (admins ann and amy; also writes as "Sunrise Commons Billing"), Bob at Maple Commons
    (admin max)."""
    seed_resident(GRANDMA_ADDRESS, "grandma", "Grandma", org_id=SUNRISE_ORG_ID)
    seed_resident(BOB_ADDRESS, "bob", "Bob", org_id=MAPLE_ORG_ID)
    seed_test_org(SUNRISE_ORG_ID, ["ann", "amy"], [GRANDMA_ADDRESS], name_variants=(SUNRISE_NAME_VARIANT,))
    seed_test_org(MAPLE_ORG_ID, ["max"], [BOB_ADDRESS])


def add_office_to_circle(receiver_address: str, org_id: str) -> graph.Contact:
    """The office's row (seed_org_contact) in the resident's circle, returned as the Reader lists it."""
    office_key = seed_org_contact(receiver_address, org_id)["SK"]
    return next(contact for contact in graph.list_contacts(receiver_address) if contact.contact_key == office_key)


def new_office_request(email_id: str, office: graph.Contact) -> str:
    """A pending request to Grandma held for her home's office; returns its id."""
    inbox_rows.claim_row(Channel.EMAIL, email_id, {"s3Key": email_id, "senderAddress": "billing@sunrise-help.example", "subject": "Invoice",
                                                   "quarantined": False})
    return graph.request_id_of(graph.create_request(email_id, GRANDMA_ADDRESS, office, OFFICE_VERDICT_FIELDS, Friction.SINGLE_TAP, None,
                                                    Channel.EMAIL))


def store_email_to_grandma(message_id: str, from_header: str, subject: str) -> None:
    """A raw email to Grandma, stored where Mail Manager stores it (inbox/<messageId>)."""
    raw_email = (f"From: {from_header}\r\nTo: Grandma <{GRANDMA_ADDRESS}>\r\nSubject: {subject}\r\n"
                 f"Message-ID: <{message_id}@sender.example>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
                 "Please see this month's account.\r\n").encode()
    boto3.client("s3").put_object(Bucket=BUCKET_NAME, Key=f"inbox/{message_id}", Body=raw_email)


def read_email_to_grandma(handler, message_id: str, from_header: str, subject: str) -> dict:
    """Runs pidge-reader on one email to Grandma and returns its EMAIL# row."""
    store_email_to_grandma(message_id, from_header, subject)
    handler.handle(inbound_event(message_id, [GRANDMA_ADDRESS]), None)
    return inbox_rows.get_row(Channel.EMAIL, f"inbox/{message_id}")


def record_user_pushes(handler, monkeypatch) -> list[tuple[str, dict]]:
    """Every push_to_user from any handler (they share shared.channels), recorded with its user as wire JSON."""
    user_pushes: list[tuple[str, dict]] = []
    monkeypatch.setattr(handler.channels, "push_to_user",
                        lambda user_id, message: user_pushes.append((user_id, json.loads(handler.channels.encode_push_message(message)))) or 1)
    return user_pushes


def summarize_request_pushes(user_pushes: list[tuple[str, dict]]) -> list[tuple]:
    return [(user_id, message["type"], message["request"]["requestId"], message["request"]["status"], message["request"].get("contactName"))
            for user_id, message in user_pushes]


def hold_three_office_requests(handler) -> tuple[str, str, str]:
    """Three of Grandma's requests held for Sunrise's office: one ann answered Yes (to release), one unanswered (to time
    out) and one ann answered No (to confirm; its raw email stored)."""
    office = add_office_to_circle(GRANDMA_ADDRESS, SUNRISE_ORG_ID)
    released_id, timed_out_id, confirmed_id = (new_office_request(f"inbox/office-{number}.eml", office) for number in (1, 2, 3))
    boto3.client("s3").create_bucket(Bucket=VERDICT_BUCKET_NAME)
    boto3.client("s3").put_object(Bucket=VERDICT_BUCKET_NAME, Key="inbox/office-3.eml", Body=b"raw")
    send_action(handler, "ann", {"action": "answer", "answer": "yes", "requestId": released_id})
    send_action(handler, "ann", {"action": "answer", "answer": "no", "requestId": confirmed_id})
    return released_id, timed_out_id, confirmed_id


def run_release_retries_and_confirm(handler, monkeypatch, caplog, request_ids: tuple[str, str, str]) -> tuple[list, list]:
    """The production pidge-release on the first request, pidge-retry attempts 1 and 2 on the second and Grandma's Confirm on
    the third. Returns the retry timers booked and, per shared.orgs log line, the request ids it names."""
    released_id, timed_out_id, confirmed_id = request_ids
    monkeypatch.setenv("RE_ASK_DELAY_SECONDS", "600")
    monkeypatch.setenv("VOICE_ENABLED", "false")
    release_handler = importlib.reload(importlib.import_module("pidge_release.handler"))
    retry_handler = importlib.reload(importlib.import_module("pidge_retry.handler"))
    scheduled: list[tuple[str, dict]] = []
    monkeypatch.setattr(retry_handler.schedules, "schedule_one_time_invocation",
                        lambda name, _fire_at, _function_arn, payload: scheduled.append((name, payload)))
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=orgs.logger.name):
        release_handler.handle({"requestId": released_id}, None)
        retry_handler.handle({"requestId": timed_out_id, "attempt": 1}, FakeContext())
        retry_handler.handle({"requestId": timed_out_id, "attempt": 2}, FakeContext())
        send_action(handler, "grandma", {"action": "confirm", "requestId": confirmed_id})
    office_logs = [record.getMessage() for record in caplog.records if record.name == orgs.logger.name]
    return scheduled, [[request_id for request_id in request_ids if request_id in log] for log in office_logs]


def rejection(request_id: str | None, reason: str) -> dict:
    return {"type": "action_rejected", "requestId": request_id, "reason": reason}


class RecordingManagementClient:
    """The API Gateway management client's post_to_connection, recording each post; its exceptions are the real client's."""

    def __init__(self):
        self.exceptions = boto3.client("apigatewaymanagementapi", endpoint_url="https://example.com").exceptions
        self.posts: list[tuple[str, dict]] = []

    def post_to_connection(self, ConnectionId, Data):
        self.posts.append((ConnectionId, json.loads(Data)))


def test_org_id_comes_from_profile_not_body(verdict_handler_without_profiles):  # noqa: F811
    handler, pushed_messages = verdict_handler_without_profiles
    seed_sunrise_and_maple()
    send_action(handler, "ann", {"action": "sync", "orgId": MAPLE_ORG_ID})
    [sync_state] = pushed_messages
    assert (sync_state["type"], sync_state["role"], sync_state["org"]["orgId"]) == ("sync_state", "org_admin", SUNRISE_ORG_ID)
    assert [member["memberRef"] for member in sync_state["org"]["members"]] == [GRANDMA_ADDRESS]
    pushed_text = json.dumps(sync_state)
    assert MAPLE_ORG_ID not in pushed_text.casefold() and BOB_ADDRESS not in pushed_text  # neither Maple's id, name nor member


def test_org_name_variant_held_by_gate_1(reader_with_bucket):  # noqa: F811
    handler, pushes = reader_with_bucket
    seed_sunrise_and_maple()
    add_office_to_circle(GRANDMA_ADDRESS, SUNRISE_ORG_ID)
    raw_email = (f"From: {BILLING_LOOKALIKE_FROM}\r\nTo: Grandma <{GRANDMA_ADDRESS}>\r\nSubject: {STATEMENT_SUBJECT}\r\n"
                 "Message-ID: <msg-billing-0001@sunrise-help.example>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
                 "Your statement is ready.\r\n").encode()
    boto3.client("s3").put_object(Bucket=BUCKET_NAME, Key="inbox/msg-billing-0001", Body=raw_email)
    handler.pipeline.reader.model = ScriptedModel(EmailVerdict, {STATEMENT_SUBJECT: [verdict_step(STATEMENT_NO_ASK)]})
    handler.handle(inbound_event("msg-billing-0001", [GRANDMA_ADDRESS]), None)
    email_row = inbox_rows.get_row(Channel.EMAIL, "inbox/msg-billing-0001")
    assert email_row["state"] == str(PolicyDecision.HOLD)
    assert str(ReasonCode.UNKNOWN_ADDRESS) in email_row["reasons"]  # only gate 1 adds it: the variant matched the office
    assert str(ReasonCode.LOOKALIKE_SENDER_NAME) not in email_row["reasons"]
    assert graph.get_request(email_row["requestId"])["contactSK"] == "CONTACT#org-sunrise"
    assert ("org-sunrise", "verification_card") in [(user_id, message["type"]) for user_id, message in pushes]


def test_ask_push_reaches_every_admin(aws, monkeypatch):  # noqa: F811
    seed_sunrise_and_maple()
    office = add_office_to_circle(GRANDMA_ADDRESS, SUNRISE_ORG_ID)
    for user_id in ("ann", "amy", "max", "grandma"):
        for connection_number in (1, 2):
            graph.register_connection(f"conn-{user_id}-{connection_number}", user_id)
    recorder = RecordingManagementClient()
    monkeypatch.setattr(channels, "_management_client", recorder)
    request_id = new_office_request("inbox/office-1.eml", office)
    assert channels.ask_in_app(graph.get_request(request_id), office, "Grandma", office.contact_pwa_user_id) is Channel.INAPP
    assert sorted(connection_id for connection_id, _message in recorder.posts) == ["conn-amy-1", "conn-amy-2", "conn-ann-1", "conn-ann-2"]
    assert {(message["type"], message["request"]["requestId"]) for _connection_id, message in recorder.posts} == {
        ("verification_card", request_id)}


def test_ask_cap_for_org_contact_is_per_resident(aws):  # noqa: F811
    resident_addresses = [f"resident{number}@example.com" for number in range(20)]
    office_key = org_contact_sort_key_for(SUNRISE_ORG_ID)
    assert [hourly_counters.take_ask_slot(office_key, address, 3) for address in resident_addresses] == [True] * 20
    assert [hourly_counters.take_ask_slot("CONTACT#jimin", address, 3) for address in resident_addresses] == [True] * 3 + [False] * 17


def test_admin_answer_accepted_for_org_contact(verdict_handler_without_profiles):  # noqa: F811
    handler, pushed_messages = verdict_handler_without_profiles
    seed_sunrise_and_maple()
    office = add_office_to_circle(GRANDMA_ADDRESS, SUNRISE_ORG_ID)
    assert office.contact_pwa_user_id == org_user_id_for(SUNRISE_ORG_ID)
    first_id, second_id, third_id = (new_office_request(f"inbox/office-{number}.eml", office) for number in (1, 2, 3))
    send_action(handler, "ann", {"action": "answer", "answer": "no", "requestId": first_id})
    answered = graph.get_request(first_id)
    assert (answered["status"], answered["answeredByUserId"], answered["repliedVia"]) == (RequestState.DENIED, "ann", Channel.INAPP)
    send_action(handler, "amy", {"action": "answer", "answer": "yes", "requestId": second_id})
    answered = graph.get_request(second_id)
    assert (answered["status"], answered["answeredByUserId"]) == (RequestState.CONFIRMED_PENDING, "amy")
    assert pushed_messages == []
    unanswered = graph.get_request(third_id)
    for user_id in ("max", "grandma"):  # Maple's admin, and the resident herself
        send_action(handler, user_id, {"action": "answer", "answer": "no", "requestId": third_id})
    assert graph.get_request(third_id) == unanswered
    assert pushed_messages == [rejection(third_id, "wrong_sender")] * 2


def test_unknown_role_refused_on_sync(verdict_handler_without_profiles):  # noqa: F811
    handler, pushed_messages = verdict_handler_without_profiles
    seed_sunrise_and_maple()
    put_profile("no-org-id", {"role": "org_admin"})
    put_profile("no-admin-row", {"role": "org_admin", "orgId": SUNRISE_ORG_ID})
    put_profile("maple-admin-row", {"role": "org_admin", "orgId": SUNRISE_ORG_ID})
    graph.get_table().put_item(Item={"PK": f"ORG#{MAPLE_ORG_ID}", "SK": "ADMIN#maple-admin-row", "role": "sender"})
    put_profile("owner", {"role": "org_admin", "orgId": SUNRISE_ORG_ID})
    graph.get_table().put_item(Item={"PK": f"ORG#{SUNRISE_ORG_ID}", "SK": "ADMIN#owner", "role": "owner"})
    for user_id in ("no-org-id", "no-admin-row", "maple-admin-row", "owner"):
        send_action(handler, user_id, {"action": "sync"})
    assert pushed_messages == [rejection(None, "not_allowed")] * 4
    send_action(handler, "ann", {"action": "sync"})
    assert (pushed_messages[-1]["type"], pushed_messages[-1]["role"]) == ("sync_state", "org_admin")


def read_rows_except_connections() -> list[dict]:
    """Every row but the CONN# pair send_action registers for each caller."""
    return [row for row in read_all_rows() if not row["PK"].startswith("CONN#") and not row["SK"].startswith("CONN#")]


def test_non_admin_org_action_not_allowed(verdict_handler_without_profiles):  # noqa: F811
    _handler, pushed_messages = verdict_handler_without_profiles
    seed_sunrise_and_maple()
    office = add_office_to_circle(GRANDMA_ADDRESS, SUNRISE_ORG_ID)
    seed_circle_contact(GRANDMA_ADDRESS, "jimin")
    put_profile("jimin", {})
    assert seed_contact_roles(GRANDMA_ADDRESS) == []  # jimin is now a contact
    resolve_body = {"action": "org_resolve", "memberRef": GRANDMA_ADDRESS,
                    "sourceKey": f"REQUEST#{new_office_request('inbox/office-1.eml', office)}"}
    rows_before = read_rows_except_connections()
    for user_id in ("grandma", "jimin", "nobody", "max"):  # resident, contact, no PROFILE, Maple's admin on a Sunrise member
        send_action(org_actions_handler, user_id, resolve_body)
    assert pushed_messages == [rejection(None, "not_allowed")] * 4
    send_action(org_actions_handler, "ann", resolve_body)  # Sunrise's admin passes the member check; nothing is open yet
    assert pushed_messages[-1] == rejection(None, "not_applied")
    assert read_rows_except_connections() == rows_before


def test_address_removed_from_profile_is_no_longer_known(reader_with_bucket):  # noqa: F811
    handler, _pushes = reader_with_bucket
    seed_resident(GRANDMA_ADDRESS, "grandma", "Grandma", org_id=SUNRISE_ORG_ID)
    office_only_seed = seed_test_org(SUNRISE_ORG_ID, ["ann"], [GRANDMA_ADDRESS])
    orgs.write_org_seed(orgs.validate_org_seed(replace(office_only_seed, verified_addresses=(SUNRISE_OFFICE_ADDRESS, SUNRISE_BILLING_ADDRESS))))
    add_office_to_circle(GRANDMA_ADDRESS, SUNRISE_ORG_ID)
    handler.pipeline.reader.model = ScriptedModel(EmailVerdict, {STATEMENT_SUBJECT: [verdict_step(STATEMENT_NO_ASK)] * 2})
    from_billing = f"Sunrise Commons <{SUNRISE_BILLING_ADDRESS}>"
    known_row = read_email_to_grandma(handler, "msg-billing-0002", from_billing, STATEMENT_SUBJECT)
    assert known_row["state"] != str(PolicyDecision.HOLD) and str(ReasonCode.UNKNOWN_ADDRESS) not in known_row["reasons"]
    orgs.write_org_seed(office_only_seed)  # the re-seed drops billing@
    assert SUNRISE_BILLING_ADDRESS in graph.get_contact_row(GRANDMA_ADDRESS, OFFICE_SORT_KEY)["knownAddresses"]  # the stale copy
    email_row = read_email_to_grandma(handler, "msg-billing-0003", from_billing, STATEMENT_SUBJECT)
    assert email_row["state"] == str(PolicyDecision.HOLD) and str(ReasonCode.UNKNOWN_ADDRESS) in email_row["reasons"]
    assert graph.get_request(email_row["requestId"])["contactSK"] == OFFICE_SORT_KEY


def office_ask_count(pushes: list[tuple[str, dict]]) -> int:
    return [(user_id, message["type"]) for user_id, message in pushes].count(("org-sunrise", "verification_card"))


def test_former_home_office_neither_asked_nor_answers(reader_with_bucket):  # noqa: F811
    handler, pushes = reader_with_bucket
    seed_sunrise_and_maple()
    office = add_office_to_circle(GRANDMA_ADDRESS, SUNRISE_ORG_ID)
    answered_id, open_id = (new_office_request(f"inbox/office-{number}.eml", office) for number in (1, 2))
    ann = verdict.SenderIdentity(Channel.INAPP, "ann")
    assert verdict.record_answer(answered_id, VerdictAnswer.NO, ann, "inapp:m1", graph.now_timestamp()).outcome is ActionOutcome.RECORDED
    handler.pipeline.reader.model = ScriptedModel(EmailVerdict, {INVOICE_SUBJECT: [verdict_step(OFFICE_VERDICT_FIELDS)] * 2})
    from_office = f"Sunrise Commons <{SUNRISE_OFFICE_ADDRESS}>"
    held_row = read_email_to_grandma(handler, "msg-office-0001", from_office, INVOICE_SUBJECT)
    assert graph.get_request(held_row["requestId"])["contactSK"] == OFFICE_SORT_KEY and office_ask_count(pushes) == 1

    seed_resident(GRANDMA_ADDRESS, "grandma", "Grandma", org_id=MAPLE_ORG_ID)  # re-enrolled at Maple: Grandma's Sunrise office row stays
    assert graph.get_contact_row(GRANDMA_ADDRESS, OFFICE_SORT_KEY) is not None
    email_row = read_email_to_grandma(handler, "msg-office-0002", from_office, INVOICE_SUBJECT)
    assert (email_row["state"], email_row["warnReason"], email_row["requestId"]) == ("warn", "unknown_name", None)
    assert office_ask_count(pushes) == 1
    unanswered = graph.get_request(open_id)
    assert verdict.record_answer(open_id, VerdictAnswer.NO, ann, "inapp:m2", graph.now_timestamp()).outcome is ActionOutcome.WRONG_SENDER
    assert graph.get_request(open_id) == unanswered


def test_former_home_office_left_out_of_open_requests(verdict_handler_without_profiles, monkeypatch, caplog):  # noqa: F811
    handler, pushed_messages = verdict_handler_without_profiles
    user_pushes = record_user_pushes(handler, monkeypatch)
    seed_sunrise_and_maple()
    request_ids = released_id, timed_out_id, confirmed_id = hold_three_office_requests(handler)
    send_action(handler, "ann", {"action": "sync"})
    assert sorted(card["requestId"] for card in pushed_messages[-1]["requests"]) == sorted(request_ids)

    seed_resident(GRANDMA_ADDRESS, "grandma", "Grandma", org_id=MAPLE_ORG_ID)  # re-enrolled at Maple: Grandma's Sunrise office row stays
    assert graph.get_contact_row(GRANDMA_ADDRESS, OFFICE_SORT_KEY) is not None
    user_pushes.clear()
    scheduled, office_log_request_ids = run_release_retries_and_confirm(handler, monkeypatch, caplog, request_ids)
    assert scheduled == [(f"retry-{timed_out_id}-2", {"requestId": timed_out_id, "attempt": 2})]
    assert summarize_request_pushes(user_pushes) == [("grandma", "request_state", released_id, "confirmed", "Maple Commons"),
                                                     ("grandma", "request_state", timed_out_id, "timeout", "Maple Commons"),
                                                     ("grandma", "request_state", confirmed_id, "denied", "Maple Commons")]
    assert office_log_request_ids == [[released_id], [timed_out_id], [timed_out_id], [confirmed_id]]
    send_action(handler, "ann", {"action": "sync"})
    assert (pushed_messages[-1]["type"], pushed_messages[-1]["requests"]) == ("sync_state", [])


def test_office_answer_refusal_counted_once(verdict_handler_without_profiles, capsys):  # noqa: F811
    handler, pushed_messages = verdict_handler_without_profiles
    seed_sunrise_and_maple()
    request_id = new_office_request("inbox/office-1.eml", add_office_to_circle(GRANDMA_ADDRESS, SUNRISE_ORG_ID))
    put_profile("no-admin-row", {"role": "org_admin", "orgId": SUNRISE_ORG_ID})  # an org admin's PROFILE with no ADMIN# row
    capsys.readouterr()
    send_action(handler, "no-admin-row", {"action": "answer", "answer": "no", "requestId": request_id})
    assert pushed_messages == [rejection(request_id, "wrong_sender")]
    assert read_profile_rejection_reasons(capsys) == ["not_in_org"]
