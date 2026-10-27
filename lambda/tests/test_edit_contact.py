"""Edit a contact (checklist 10g): the receiver saves the same form on an existing row in place. A number already on the row
keeps its check state, so a checked number keeps being asked; a new WhatsApp number waits for its own code; the last checked
WhatsApp number cannot be dropped. Her circle carries each person's numbers and emails to her own sockets only."""

from datetime import datetime, timezone

from shared import circle, graph, reach_points
from shared.enums import ActionOutcome, Channel, VerifiedVia
from tests.test_circle import (  # noqa: F401
    MINSOO_FORM, MINSOO_NUMBER, MINSOO_PHONE, MINSOO_SECOND_NUMBER, TWO_NUMBER_FORM, added_contact, circle_handler, point_states,
    whatsapp_text_event,
)
from tests.test_verdict import RECEIVER_ADDRESS, RECEIVER_USER_ID, aws  # noqa: F401
from tests.test_verify_contact import JIMIN_WHATSAPP_NUMBER, rejection, seed_circle_contact, send_action, verdict_handler  # noqa: F401

NEW_NUMBER = "+821055554444"


def checked_minsoo(circle_handler, form: dict = MINSOO_FORM) -> dict:  # noqa: F811
    """Minsoo added with the form's numbers and his first WhatsApp number checked by its code."""
    handler, pushed_messages, _user_pushes, _sent = circle_handler
    send_action(handler, RECEIVER_USER_ID, form)
    [contact_added] = [message for message in pushed_messages if message["type"] == "contact_added"]
    [row] = [row for row in graph.list_contact_rows(RECEIVER_ADDRESS) if row["contactName"] == "Minsoo Kim"]
    handler.handle(whatsapp_text_event(MINSOO_NUMBER.removeprefix("+"), contact_added["code"]), None)
    return graph.get_contact_row(RECEIVER_ADDRESS, row["SK"])


def edit_form(row: dict, **changes) -> dict:
    return {**MINSOO_FORM, "action": "edit_contact", "contactUserId": row["contactPwaUserId"], **changes}


def edited_pushes(pushed_messages: list[dict]) -> list[dict]:
    return [message for message in pushed_messages if message["type"] == "contact_edited"]


def test_an_edit_renames_adds_an_email_removes_a_phone_and_keeps_the_checked_number_checked(circle_handler):  # noqa: F811
    handler, pushed_messages, user_pushes, _sent = circle_handler
    row = checked_minsoo(circle_handler, TWO_NUMBER_FORM)
    assert ("rcs", MINSOO_PHONE, "pending") in point_states(row)
    send_action(handler, RECEIVER_USER_ID, edit_form(row, contactName="Minsoo K", relationship="friend",
                                                     emailAddresses=["minsoo@example.com", "mk@new.example"]))
    edited = graph.get_contact_row(RECEIVER_ADDRESS, row["SK"])
    assert (edited["contactName"], edited["relationship"], edited["verifiedVia"]) == ("Minsoo K", "friend", "whatsapp")
    assert point_states(edited) == [("whatsapp", MINSOO_NUMBER, "whatsapp"), ("email", "minsoo@example.com", None),
                                    ("email", "mk@new.example", None)]
    assert edited["contactReachPoints"][0] == row["contactReachPoints"][0]  # the checked point is kept as stored
    assert "verificationCodeHash" not in edited
    [edited_push] = edited_pushes(pushed_messages)
    assert edited_push["noteText"] is None and edited_push["code"] is None
    assert user_pushes[-1][0] == RECEIVER_USER_ID and user_pushes[-1][1]["circle"][0]["contactName"] == "Minsoo K"


def test_a_new_number_waits_for_its_own_code_while_the_checked_one_is_still_asked(circle_handler):  # noqa: F811
    handler, pushed_messages, user_pushes, sent_messages = circle_handler
    row = checked_minsoo(circle_handler)
    send_action(handler, RECEIVER_USER_ID, edit_form(row, whatsappNumbers=[MINSOO_NUMBER, NEW_NUMBER], phoneNumbers=[MINSOO_PHONE]))
    edited = graph.get_contact_row(RECEIVER_ADDRESS, row["SK"])
    assert point_states(edited)[:3] == [("whatsapp", MINSOO_NUMBER, "whatsapp"), ("whatsapp", NEW_NUMBER, "pending"),
                                        ("rcs", MINSOO_PHONE, "pending")]
    assert graph.contact_from_row(edited).addresses_to_ask(Channel.WHATSAPP) == (MINSOO_NUMBER,)
    [edited_push] = edited_pushes(pushed_messages)
    assert f"Your code is {edited_push['code']}." in edited_push["noteText"] and "?token=" not in edited_push["noteText"]
    # the wait step reads the waiting numbers from the pushed contact: the edited row, in the circle entry's shape
    assert {"channel": "whatsapp", "address": NEW_NUMBER, "verifiedVia": "pending"} in edited_push["contact"]["reachPoints"]
    [circle_entry] = [message for _user_id, message in user_pushes if message["type"] == "circle_state"][-1]["circle"]
    assert edited_push["contact"] == circle_entry
    assert edited["verificationCodeHash"] == graph.hash_access_token(edited_push["code"])
    handler.handle(whatsapp_text_event(NEW_NUMBER.removeprefix("+"), edited_push["code"], "wamid.NEW"), None)
    checked = graph.get_contact_row(RECEIVER_ADDRESS, row["SK"])
    assert graph.contact_from_row(checked).addresses_to_ask(Channel.WHATSAPP) == (MINSOO_NUMBER, NEW_NUMBER)
    assert "verificationCodeHash" not in checked and sent_messages[-1]["to"] == NEW_NUMBER


def test_dropping_the_last_checked_number_is_refused_and_writes_nothing(circle_handler):  # noqa: F811
    handler, pushed_messages, _user_pushes, _sent = circle_handler
    row = checked_minsoo(circle_handler)
    send_action(handler, RECEIVER_USER_ID, edit_form(row, whatsappNumbers=[NEW_NUMBER]))
    assert pushed_messages[-1] == rejection("last_checked_number")
    assert graph.get_contact_row(RECEIVER_ADDRESS, row["SK"]) == row


def test_a_pending_contact_can_change_every_number(circle_handler):  # noqa: F811
    handler, pushed_messages, _user_pushes, _sent = circle_handler
    contact_added, row = added_contact(circle_handler)
    send_action(handler, RECEIVER_USER_ID, edit_form(row, whatsappNumbers=[NEW_NUMBER]))
    edited = graph.get_contact_row(RECEIVER_ADDRESS, row["SK"])
    assert edited["verifiedVia"] == "pending" and point_states(edited)[0] == ("whatsapp", NEW_NUMBER, "pending")
    [edited_push] = edited_pushes(pushed_messages)
    assert edited_push["code"] != contact_added["code"]
    handler.handle(whatsapp_text_event(MINSOO_NUMBER.removeprefix("+"), contact_added["code"], "wamid.OLD"), None)
    assert graph.get_contact_row(RECEIVER_ADDRESS, row["SK"])["verifiedVia"] == "pending"  # the dropped number checks nothing


def test_a_seeded_row_moves_to_the_list_on_its_first_edit(circle_handler):  # noqa: F811
    handler, _pushed, _user_pushes, _sent = circle_handler
    seeded_row = seed_circle_contact()
    send_action(handler, RECEIVER_USER_ID, {
        "action": "edit_contact", "contactUserId": "jimin", "contactName": "Jimin", "relationship": "grandson",
        "whatsappNumbers": [JIMIN_WHATSAPP_NUMBER], "phoneNumbers": [], "emailAddresses": [seeded_row["contactEmail"]]})
    edited = graph.get_contact_row(RECEIVER_ADDRESS, "CONTACT#jimin")
    assert point_states(edited) == [("whatsapp", JIMIN_WHATSAPP_NUMBER, "seed"), ("email", seeded_row["contactEmail"], None)]
    assert not {"contactWhatsApp", "contactRcs", "contactEmail"} & edited.keys()
    assert edited["verifiedVia"] == "seed"
    # the address the sample email comes from stays a known sender, never asked
    assert reach_points.known_sender_addresses_of(edited) == ("jimin@family.example", seeded_row["contactEmail"])


def test_a_stranger_a_bad_field_an_unknown_contact_and_a_taken_name_write_nothing(circle_handler):  # noqa: F811
    handler, pushed_messages, _user_pushes, _sent = circle_handler
    seed_circle_contact()
    row = checked_minsoo(circle_handler)
    pushed_messages.clear()
    send_action(handler, "jimin", edit_form(row, contactName="Hacked"))
    send_action(handler, RECEIVER_USER_ID, edit_form(row, whatsappNumbers=["0109876"]))
    send_action(handler, RECEIVER_USER_ID, edit_form(row, contactUserId="nobody"))
    send_action(handler, RECEIVER_USER_ID, edit_form(row, contactName="JIMIN"))
    assert pushed_messages == [rejection("not_allowed"), rejection("bad_message"), rejection("not_applied"), rejection("duplicate_name")]
    assert graph.get_contact_row(RECEIVER_ADDRESS, row["SK"]) == row


def test_her_circle_carries_each_persons_numbers_to_her_own_sockets_only(circle_handler):  # noqa: F811
    _handler, _pushed, user_pushes, _sent = circle_handler
    checked_minsoo(circle_handler)
    assert {user_id for user_id, message in user_pushes if message["type"] == "circle_state"} == {RECEIVER_USER_ID}
    [entry] = user_pushes[-1][1]["circle"]
    assert entry["reachPoints"] == [{"channel": "whatsapp", "address": MINSOO_NUMBER, "verifiedVia": "whatsapp"},
                                    {"channel": "email", "address": "minsoo@example.com", "verifiedVia": None},
                                    {"channel": "email", "address": "minsoo.kim@work.example", "verifiedVia": None}]


def test_saving_again_keeps_a_live_code_and_renews_an_expired_one(circle_handler):  # noqa: F811
    handler, pushed_messages, _user_pushes, _sent = circle_handler
    contact_added, row = added_contact(circle_handler)
    send_action(handler, RECEIVER_USER_ID, edit_form(row, relationship="friend"))
    assert edited_pushes(pushed_messages)[-1]["code"] is None
    assert graph.get_contact_row(RECEIVER_ADDRESS, row["SK"])["verificationCodeHash"] == graph.hash_access_token(contact_added["code"])
    graph.get_table().update_item(Key={"PK": row["PK"], "SK": row["SK"]}, UpdateExpression="SET verificationExpiresAt = :past",
                                  ExpressionAttributeValues={":past": "2026-01-01T00:00:00Z"})
    send_action(handler, RECEIVER_USER_ID, edit_form(row, relationship="friend"))
    renewed_code = edited_pushes(pushed_messages)[-1]["code"]
    assert renewed_code is not None and f"Your code is {renewed_code}." in edited_pushes(pushed_messages)[-1]["noteText"]
    handler.handle(whatsapp_text_event(MINSOO_NUMBER.removeprefix("+"), renewed_code), None)
    assert graph.get_contact_row(RECEIVER_ADDRESS, row["SK"])["verifiedVia"] == "whatsapp"


def test_an_edit_on_a_row_that_changed_since_it_was_read_is_refused(circle_handler, monkeypatch):  # noqa: F811
    handler, _pushed, _user_pushes, _sent = circle_handler
    contact_added, row_before_check = added_contact(circle_handler)
    handler.handle(whatsapp_text_event(MINSOO_NUMBER.removeprefix("+"), contact_added["code"]), None)
    monkeypatch.setattr(graph, "list_contact_rows", lambda _receiver_address: [row_before_check])
    form = circle.parse_contact_form(edit_form(row_before_check, contactName="Minsoo K"))
    edit = circle.edit_contact(RECEIVER_ADDRESS, row_before_check["contactPwaUserId"], form, datetime.now(timezone.utc))
    assert edit.outcome is ActionOutcome.NOT_APPLIED
    stored_row = graph.get_contact_row(RECEIVER_ADDRESS, row_before_check["SK"])
    assert stored_row["contactName"] == "Minsoo Kim" and point_states(stored_row)[0] == ("whatsapp", MINSOO_NUMBER, "whatsapp")


def test_a_code_reply_read_before_the_numbers_moved_checks_nothing(circle_handler):  # noqa: F811
    handler, _pushed, _user_pushes, _sent = circle_handler
    send_action(handler, RECEIVER_USER_ID, TWO_NUMBER_FORM)
    [row_before_edit] = graph.list_contact_rows(RECEIVER_ADDRESS)
    send_action(handler, RECEIVER_USER_ID, edit_form(row_before_edit, whatsappNumbers=[MINSOO_SECOND_NUMBER, MINSOO_NUMBER]))
    reordered_row = graph.get_contact_row(RECEIVER_ADDRESS, row_before_edit["SK"])
    assert reordered_row["verificationCodeHash"] == row_before_edit["verificationCodeHash"]  # same code: only the address guards
    assert not circle.mark_whatsapp_number_checked(RECEIVER_ADDRESS, row_before_edit, 0, VerifiedVia.WHATSAPP, graph.iso_timestamp(
        datetime.now(timezone.utc)))
    assert graph.get_contact_row(RECEIVER_ADDRESS, row_before_edit["SK"]) == reordered_row


def test_adding_a_number_while_one_waits_kills_the_earlier_code(circle_handler):  # noqa: F811
    handler, pushed_messages, _user_pushes, _sent = circle_handler
    row = checked_minsoo(circle_handler)
    send_action(handler, RECEIVER_USER_ID, edit_form(row, whatsappNumbers=[MINSOO_NUMBER, NEW_NUMBER]))
    first_code = edited_pushes(pushed_messages)[-1]["code"]
    send_action(handler, RECEIVER_USER_ID, edit_form(row, whatsappNumbers=[MINSOO_NUMBER, NEW_NUMBER, MINSOO_SECOND_NUMBER]))
    second_code = edited_pushes(pushed_messages)[-1]["code"]
    assert second_code not in (None, first_code)
    handler.handle(whatsapp_text_event(NEW_NUMBER.removeprefix("+"), first_code, "wamid.FIRST"), None)
    assert ("whatsapp", NEW_NUMBER, "pending") in point_states(graph.get_contact_row(RECEIVER_ADDRESS, row["SK"]))
    handler.handle(whatsapp_text_event(NEW_NUMBER.removeprefix("+"), second_code, "wamid.SECOND"), None)
    assert ("whatsapp", NEW_NUMBER, "whatsapp") in point_states(graph.get_contact_row(RECEIVER_ADDRESS, row["SK"]))


def test_a_pending_row_from_before_the_list_keeps_its_code_and_known_addresses_on_edit(circle_handler):  # noqa: F811
    """The slice-10c row shape (one number in contactWhatsApp, knownAddresses, no contactReachPoints) may be in the live table."""
    handler, pushed_messages, _user_pushes, _sent = circle_handler
    old_code = "123456"
    older_row = {
        "PK": f"RECEIVER#{RECEIVER_ADDRESS}", "SK": "CONTACT#minsoo10c", "contactName": "Minsoo Kim", "relationship": "son",
        "contactPwaUserId": "minsoo10c", "contactWhatsApp": MINSOO_NUMBER, "contactEmail": "minsoo@example.com",
        "knownAddresses": ["minsoo.kim@work.example"], "verifiedVia": "pending", "verificationCodeHash": graph.hash_access_token(old_code),
        "verificationExpiresAt": "2099-01-01T00:00:00Z", "addedAt": "2026-10-20T00:00:00Z",
    }
    graph.get_table().put_item(Item=older_row)
    send_action(handler, RECEIVER_USER_ID, edit_form(older_row, emailAddresses=["minsoo@example.com"]))
    edited = graph.get_contact_row(RECEIVER_ADDRESS, older_row["SK"])
    assert point_states(edited) == [("whatsapp", MINSOO_NUMBER, "pending"), ("email", "minsoo@example.com", None)]
    assert edited["verificationCodeHash"] == older_row["verificationCodeHash"] and edited_pushes(pushed_messages)[-1]["code"] is None
    assert reach_points.known_sender_addresses_of(edited) == ("minsoo.kim@work.example", "minsoo@example.com")
    handler.handle(whatsapp_text_event(MINSOO_NUMBER.removeprefix("+"), old_code), None)
    assert graph.get_contact_row(RECEIVER_ADDRESS, older_row["SK"])["verifiedVia"] == "whatsapp"
