"""A resident's choices about their home: what the office may see (RECEIVER#/ORGPREF#<orgId>, written only by the
resident's own set_org_visibility; no choice reads as none) and the office's row in the circle (added and removed only by the
resident, built only by orgs.org_contact_row, never editable; a request still open when it is removed reaches the resident
alone). The org is the resident's own orgId, never the body; an org admin's connection writes nothing under RECEIVER#."""

import importlib

import pytest

from shared import graph, orgs
from shared.enums import Channel, Friction
from shared.org_enums import AdminRole, OrgVisibility
from shared.org_preferences import read_org_visibility
from shared.orgs import AdminSeed, MemberSeed, OrgSeed
from tests.helpers.orgs import seed_test_org
from tests.helpers.profiles import TEST_TIMEZONE, seed_resident
from tests.test_org import (
    BOB_ADDRESS, GRANDMA_ADDRESS, MAPLE_ORG_ID, OFFICE_VERDICT_FIELDS, SUNRISE_NAME_VARIANT, SUNRISE_ORG_ID, hold_three_office_requests,
    record_user_pushes, run_release_retries_and_confirm, seed_sunrise_and_maple, summarize_request_pushes,
)
from tests.test_retry import FakeContext
from tests.test_seed_org import read_all_rows, read_receiver_partition_rows
from tests.test_verdict import aws  # noqa: F401
from tests.test_verify_contact import MINSOO_FORM, rejection, seed_circle_contact, send_action, verdict_handler_without_profiles  # noqa: F401

OFFICE_SORT_KEY = "CONTACT#org-sunrise"
SUNRISE_PREFERENCE_KEY = {"PK": f"RECEIVER#{GRANDMA_ADDRESS}", "SK": "ORGPREF#sunrise"}
EDIT_OFFICE_FORM = {**MINSOO_FORM, "action": "edit_contact", "contactUserId": "org-sunrise"}


@pytest.fixture
def org_preference_handler(verdict_handler_without_profiles, monkeypatch):  # noqa: F811
    """pidge-verdict with no PROFILE rows yet; pushes to a user's sockets are recorded as wire JSON."""
    handler, pushed_messages = verdict_handler_without_profiles
    return handler, pushed_messages, record_user_pushes(handler, monkeypatch)


def stored_office_row() -> dict | None:
    return graph.get_contact_row(GRANDMA_ADDRESS, OFFICE_SORT_KEY)


def sunrise_preference(visibility: str, is_org_contact: bool) -> dict:
    return {"orgName": "Sunrise Commons", "visibility": visibility, "isOrgContact": is_org_contact}


def test_absent_visibility_reads_none(org_preference_handler):
    handler, _pushed_messages, _user_pushes = org_preference_handler
    seed_sunrise_and_maple()
    assert read_org_visibility(GRANDMA_ADDRESS, SUNRISE_ORG_ID) is OrgVisibility.NONE
    # No production writer leaves a row without the field or with an unknown value: both are put here to test the reader.
    graph.get_table().put_item(Item={**SUNRISE_PREFERENCE_KEY, "orgVisibilitySource": "resident_app"})
    assert read_org_visibility(GRANDMA_ADDRESS, SUNRISE_ORG_ID) is OrgVisibility.NONE
    send_action(handler, "grandma", {"action": "set_org_visibility", "visibility": "alerts_only"})
    assert read_org_visibility(GRANDMA_ADDRESS, SUNRISE_ORG_ID) is OrgVisibility.ALERTS_ONLY
    graph.get_table().update_item(Key=SUNRISE_PREFERENCE_KEY, UpdateExpression="SET orgVisibility = :unknown",
                                  ExpressionAttributeValues={":unknown": "bogus"})
    with pytest.raises(ValueError):
        read_org_visibility(GRANDMA_ADDRESS, SUNRISE_ORG_ID)


def test_admin_connection_cannot_write_receiver_partition(org_preference_handler):
    handler, pushed_messages, user_pushes = org_preference_handler
    seed_sunrise_and_maple()
    send_action(handler, "grandma", {"action": "add_org_contact"})
    send_action(handler, "grandma", {"action": "set_org_visibility", "visibility": "alerts_only"})
    assert orgs.require_org_admin("ann") is not None  # a complete admin of Grandma's own home
    receiver_rows_before, push_count_before = read_receiver_partition_rows(), len(user_pushes)
    pushed_messages.clear()
    for body in ({"action": "set_org_visibility", "visibility": "alerts_and_cases"}, {"action": "add_org_contact"},
                 {"action": "remove_org_contact"}, MINSOO_FORM, EDIT_OFFICE_FORM):
        send_action(handler, "ann", body)
    assert pushed_messages == [rejection("not_allowed")] * 5
    assert read_receiver_partition_rows() == receiver_rows_before and len(user_pushes) == push_count_before


def test_org_contact_written_only_by_resident_action(org_preference_handler):
    handler, pushed_messages, user_pushes = org_preference_handler
    seed_resident(GRANDMA_ADDRESS, "grandma", "Grandma", org_id=SUNRISE_ORG_ID)
    receiver_rows_before = read_receiver_partition_rows()
    seed_test_org(SUNRISE_ORG_ID, ["ann"], [GRANDMA_ADDRESS])
    assert read_receiver_partition_rows() == receiver_rows_before
    send_action(handler, "ann", {"action": "add_org_contact"})
    assert pushed_messages == [rejection("not_allowed")] and stored_office_row() is None
    send_action(handler, "grandma", {"action": "add_org_contact"})
    office_row = stored_office_row()
    assert (office_row["contactPwaUserId"], office_row["verifiedVia"]) == ("org-sunrise", "org")
    [(pushed_user, circle_state)] = user_pushes
    assert (pushed_user, circle_state["type"], circle_state["orgPreference"]) == ("grandma", "circle_state", sunrise_preference("none", True))
    assert [(entry["contactUserId"], entry["verifiedVia"]) for entry in circle_state["circle"]] == [("org-sunrise", "org")]
    send_action(handler, "grandma", {"action": "add_org_contact"})
    assert pushed_messages[-1] == rejection("not_applied") and stored_office_row() == office_row and len(user_pushes) == 1


def test_org_contact_known_addresses_equal_verified_addresses(org_preference_handler):
    handler, _pushed_messages, _user_pushes = org_preference_handler
    seed_resident(GRANDMA_ADDRESS, "grandma", "Grandma", org_id=SUNRISE_ORG_ID)
    org_seed = OrgSeed(SUNRISE_ORG_ID, "Sunrise Commons", ("Office@Sunrise.example", "billing@sunrise.example"), (SUNRISE_NAME_VARIANT,),
                       TEST_TIMEZONE, (AdminSeed("ann", AdminRole.VIEWER),), (MemberSeed(GRANDMA_ADDRESS, ()),))
    orgs.write_org_seed(orgs.validate_org_seed(org_seed))
    send_action(handler, "grandma", {"action": "add_org_contact"})
    office_row = stored_office_row()
    assert office_row["knownAddresses"] == ["office@sunrise.example", "billing@sunrise.example"]
    assert (office_row["contactName"], office_row["nameVariants"]) == ("Sunrise Commons", [SUNRISE_NAME_VARIANT])
    assert (office_row["contactPwaUserId"], office_row["verifiedVia"]) == ("org-sunrise", "org")
    [office] = [contact for contact in graph.list_contacts(GRANDMA_ADDRESS) if contact.contact_key == OFFICE_SORT_KEY]
    assert office.known_addresses == ("office@sunrise.example", "billing@sunrise.example")
    assert office.name_variants == (SUNRISE_NAME_VARIANT,)


def test_visibility_written_by_resident_with_time_and_source(org_preference_handler):
    handler, pushed_messages, user_pushes = org_preference_handler
    seed_sunrise_and_maple()
    written_after = graph.now_timestamp()
    send_action(handler, "grandma", {"action": "set_org_visibility", "visibility": "alerts_and_cases", "orgId": MAPLE_ORG_ID})
    stored = graph.get_table().get_item(Key=SUNRISE_PREFERENCE_KEY)["Item"]
    assert (stored["orgVisibility"], stored["orgVisibilitySource"]) == ("alerts_and_cases", "resident_app")
    assert written_after <= stored["orgVisibilitySetAt"] <= graph.now_timestamp()
    assert [row for row in read_all_rows() if row["SK"].startswith("ORGPREF#")] == [stored]  # nothing under maple
    assert user_pushes[-1] == ("grandma", {"type": "circle_state", "circle": [],
                                           "orgPreference": sunrise_preference("alerts_and_cases", False)})
    send_action(handler, "grandma", {"action": "set_org_visibility", "visibility": "everything"})
    send_action(handler, "grandma", {"action": "set_org_visibility", "visibility": ["alerts_only"]})
    assert pushed_messages == [rejection("bad_message")] * 2
    assert graph.get_table().get_item(Key=SUNRISE_PREFERENCE_KEY)["Item"] == stored
    send_action(handler, "grandma", {"action": "sync"})
    assert pushed_messages[-1]["orgPreference"] == sunrise_preference("alerts_and_cases", False)
    seed_resident("carol@example.com", "carol", "Carol")  # Carol's org (org-test) has no PROFILE: no home section, no write
    send_action(handler, "carol", {"action": "set_org_visibility", "visibility": "alerts_only"})
    send_action(handler, "carol", {"action": "add_org_contact"})
    send_action(handler, "carol", {"action": "sync"})
    assert pushed_messages[-3:-1] == [rejection("not_allowed")] * 2 and pushed_messages[-1]["orgPreference"] is None
    assert [row["SK"] for row in read_all_rows() if row["PK"] == "RECEIVER#carol@example.com"] == ["PROFILE"]


def test_removed_org_contact_leaves_circle_and_edit_refused(org_preference_handler):
    handler, pushed_messages, user_pushes = org_preference_handler
    seed_sunrise_and_maple()
    send_action(handler, "grandma", {"action": "add_org_contact"})
    office_row = stored_office_row()
    send_action(handler, "grandma", EDIT_OFFICE_FORM)
    assert pushed_messages == [rejection("not_allowed")] and stored_office_row() == office_row
    send_action(handler, "grandma", {"action": "remove_org_contact"})
    assert stored_office_row() is None and graph.list_contacts(GRANDMA_ADDRESS) == []
    assert user_pushes[-1] == ("grandma", {"type": "circle_state", "circle": [], "orgPreference": sunrise_preference("none", False)})
    send_action(handler, "grandma", {"action": "remove_org_contact"})
    send_action(handler, "grandma", EDIT_OFFICE_FORM)
    assert pushed_messages[1:] == [rejection("not_applied")] * 2


def test_org_name_clash_is_duplicate_name(org_preference_handler):
    handler, pushed_messages, _user_pushes = org_preference_handler
    seed_sunrise_and_maple()
    send_action(handler, "grandma", {**MINSOO_FORM, "contactName": f" {SUNRISE_NAME_VARIANT.upper()} "})  # Sunrise's name variant
    send_action(handler, "bob", {**MINSOO_FORM, "contactName": "maple  commons"})  # Maple's org name
    pushed_messages.clear()  # the two contact_added notes
    send_action(handler, "grandma", {"action": "add_org_contact"})
    send_action(handler, "bob", {"action": "add_org_contact"})
    assert pushed_messages == [rejection("duplicate_name")] * 2
    assert stored_office_row() is None and graph.get_contact_row(BOB_ADDRESS, "CONTACT#org-maple") is None


def test_office_removed_with_open_request(org_preference_handler, monkeypatch, caplog):
    handler, _pushed_messages, user_pushes = org_preference_handler
    seed_sunrise_and_maple()
    request_ids = released_id, timed_out_id, confirmed_id = hold_three_office_requests(handler)
    send_action(handler, "grandma", {"action": "remove_org_contact"})
    assert stored_office_row() is None
    user_pushes.clear()
    scheduled, office_log_request_ids = run_release_retries_and_confirm(handler, monkeypatch, caplog, request_ids)
    assert scheduled == [(f"retry-{timed_out_id}-2", {"requestId": timed_out_id, "attempt": 2})]
    assert summarize_request_pushes(user_pushes) == [("grandma", "request_state", released_id, "confirmed", "Sunrise Commons"),
                                                     ("grandma", "request_state", timed_out_id, "timeout", "Sunrise Commons"),
                                                     ("grandma", "request_state", confirmed_id, "denied", "Sunrise Commons")]
    assert office_log_request_ids == [[released_id], [timed_out_id], [timed_out_id], [confirmed_id]]

    seed_circle_contact(GRANDMA_ADDRESS, "jimin")  # a family row is never deleted: one that is gone still raises
    jimin = next(contact for contact in graph.list_contacts(GRANDMA_ADDRESS) if contact.contact_key == "CONTACT#jimin")
    family_request_id = graph.request_id_of(
        graph.create_request("inbox/family-1.eml", GRANDMA_ADDRESS, jimin, OFFICE_VERDICT_FIELDS, Friction.SINGLE_TAP, None, Channel.EMAIL))
    graph.get_table().delete_item(Key={"PK": f"RECEIVER#{GRANDMA_ADDRESS}", "SK": "CONTACT#jimin"})
    with pytest.raises(LookupError):
        importlib.import_module("pidge_retry.handler").handle({"requestId": family_request_id, "attempt": 2}, FakeContext())
