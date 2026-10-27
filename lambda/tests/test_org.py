"""A home (org) in the kernel: the office's contact identity org-<orgId> is answered and reached through the org's admins only,
an admin's org comes from their own PROFILE and ADMIN# rows (never the body), a name variant is held by gate 1, and the ask
cap counts each resident of the office apart."""

import json

import boto3

from shared import channels, graph, hourly_counters
from shared.enums import Channel, Friction, Intent, PolicyDecision, ReasonCode, RequestState
from shared.org_ids import org_contact_sort_key_for, org_user_id_for
from shared.reader_agent import EmailVerdict
from tests.helpers.orgs import seed_org_contact, seed_test_org
from tests.helpers.profiles import seed_resident
from tests.helpers.scripted_model import ScriptedModel, verdict_step
from tests.test_reader_reading_state import inbound_event
from tests.test_reader_receiver import BUCKET_NAME, reader_with_bucket  # noqa: F401
from tests.test_roles import put_profile
from tests.test_verdict import aws  # noqa: F401
from tests.test_verify_contact import send_action, verdict_handler_without_profiles  # noqa: F401

SUNRISE_ORG_ID = "sunrise"
MAPLE_ORG_ID = "maple"
GRANDMA_ADDRESS = "grandma@example.com"
BOB_ADDRESS = "bob@example.com"
SUNRISE_NAME_VARIANT = "Sunrise Commons Billing"
BILLING_LOOKALIKE_FROM = f"{SUNRISE_NAME_VARIANT} <billing@sunrise-help.example>"
STATEMENT_SUBJECT = "Your October statement"
OFFICE_VERDICT_FIELDS = {"claimed_person": "Sunrise Commons", "intent": "money", "confidence": 0.95, "summary": "Asks to pay an invoice."}
STATEMENT_NO_ASK = {"claimed_person": "Sunrise Commons", "intent": Intent.NONE, "confidence": 0.9, "summary": "A monthly statement."}


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
    graph.claim_email(email_id, {"s3Key": email_id, "senderAddress": "billing@sunrise-help.example", "subject": "Invoice",
                                 "quarantined": False})
    return graph.request_id_of(graph.create_request(email_id, GRANDMA_ADDRESS, office, OFFICE_VERDICT_FIELDS, Friction.SINGLE_TAP, None))


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
    handler.reader.model = ScriptedModel(EmailVerdict, {STATEMENT_SUBJECT: [verdict_step(STATEMENT_NO_ASK)]})
    handler.handle(inbound_event("msg-billing-0001", [GRANDMA_ADDRESS]), None)
    email_row = graph.get_email("inbox/msg-billing-0001")
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
