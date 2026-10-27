"""Enrolment: the resident's rows are written before the address is listed and deleted only after it is delisted, an unknown
address is counted and raised, a contact's user id never becomes a resident, and a token rotation keeps the role."""

import json
from datetime import datetime, timezone

import pytest
from botocore.exceptions import ClientError

from shared import circle, graph, profiles, residents
from shared.enums import ActionOutcome
from shared.profiles import ReceiverProfile, UnknownReceiverError
from shared.residents import ResidentEnrolment
from tests.helpers.profiles import seed_resident
from tests.test_verdict import aws  # noqa: F401

ADDRESS_LIST_ID = "al-test0001"
BOB_ENROLMENT = ResidentEnrolment(" Bob@Example.com", "bob", "Bob", "org-test", "America/New_York")
BOB_ADDRESS = "bob@example.com"
CONTACT_FORM = {"contactName": "Minsoo", "relationship": "son", "whatsappNumbers": ["+821098765432"], "phoneNumbers": [],
                "emailAddresses": ["minsoo@example.com"]}


def read_row(partition_key: str) -> dict | None:
    return graph.get_table().get_item(Key={"PK": partition_key, "SK": "PROFILE"}).get("Item")


class RecordingMailManagerClient:
    """Records each address-list call and whether RECEIVER#bob@example.com/PROFILE existed when it was made."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.profile_row_existed: list[bool] = []

    def record(self, call_name: str, arguments: dict) -> dict:
        self.calls.append((call_name, arguments))
        self.profile_row_existed.append(read_row(f"RECEIVER#{BOB_ADDRESS}") is not None)
        return {}

    def register_member_to_address_list(self, **arguments) -> dict:
        return self.record("register_member_to_address_list", arguments)

    def deregister_member_from_address_list(self, **arguments) -> dict:
        return self.record("deregister_member_from_address_list", arguments)


class ConflictingMailManagerClient:
    def register_member_to_address_list(self, **arguments) -> dict:
        raise ClientError({"Error": {"Code": "ConflictException"}}, "RegisterMemberToAddressList")


def test_enrolment_registers_address_list_member(aws):  # noqa: F811
    client = RecordingMailManagerClient()
    assert residents.enrol_resident(client, ADDRESS_LIST_ID, BOB_ENROLMENT) == []
    assert client.calls == [("register_member_to_address_list", {"AddressListId": ADDRESS_LIST_ID, "Address": BOB_ADDRESS})]
    assert client.profile_row_existed == [True]
    assert read_row(f"RECEIVER#{BOB_ADDRESS}") == {"PK": f"RECEIVER#{BOB_ADDRESS}", "SK": "PROFILE", "receiverUserId": "bob",
                                                   "residentName": "Bob", "orgId": "org-test", "timezone": "America/New_York"}
    user_profile = read_row("USER#bob")
    assert (user_profile["role"], user_profile["receiverEmail"], user_profile["orgId"]) == ("receiver", BOB_ADDRESS, "org-test")
    assert profiles.require_receiver_profile(BOB_ADDRESS) == ReceiverProfile(BOB_ADDRESS, "bob", "Bob", "org-test", "America/New_York")
    with pytest.raises(ClientError):
        residents.enrol_resident(ConflictingMailManagerClient(), ADDRESS_LIST_ID, BOB_ENROLMENT)


def test_unenrolment_deregisters_before_deleting_profile(aws):  # noqa: F811
    residents.enrol_resident(RecordingMailManagerClient(), ADDRESS_LIST_ID, BOB_ENROLMENT)
    client = RecordingMailManagerClient()
    residents.unenrol_resident(client, ADDRESS_LIST_ID, BOB_ADDRESS)
    assert client.calls == [("deregister_member_from_address_list", {"AddressListId": ADDRESS_LIST_ID, "Address": BOB_ADDRESS})]
    assert client.profile_row_existed == [True]
    assert read_row(f"RECEIVER#{BOB_ADDRESS}") is None
    assert not {"role", "receiverEmail", "orgId"} & read_row("USER#bob").keys()


def test_unknown_receiver_raises_and_counts(aws, capsys):  # noqa: F811
    with pytest.raises(UnknownReceiverError):
        profiles.require_receiver_profile("nobody@example.com")
    [metric_line] = [line for line in capsys.readouterr().out.splitlines() if "UnknownReceiver" in line]
    assert json.loads(metric_line)["UnknownReceiver"] == 1


def test_a_contact_user_id_cannot_become_a_resident(aws):  # noqa: F811
    enrolment = circle.enrol_contact("grandma@example.com", circle.parse_contact_form(CONTACT_FORM), datetime.now(timezone.utc))
    assert enrolment.outcome is ActionOutcome.RECORDED
    contact_user_id = enrolment.contact_row["contactPwaUserId"]
    assert read_row(f"USER#{contact_user_id}")["role"] == "contact"
    with pytest.raises(ValueError):
        residents.write_resident_rows(ResidentEnrolment(BOB_ADDRESS, contact_user_id, "Bob", "org-test", "America/New_York"))
    assert read_row(f"RECEIVER#{BOB_ADDRESS}") is None
    assert read_row(f"USER#{contact_user_id}")["role"] == "contact"


def test_token_rotation_keeps_the_role(aws):  # noqa: F811
    seed_resident(BOB_ADDRESS, "bob", "Bob")
    access_token = circle.issue_access_token("bob")
    user_profile = read_row("USER#bob")
    assert (user_profile["role"], user_profile["receiverEmail"], user_profile["orgId"]) == ("receiver", BOB_ADDRESS, "org-test")
    assert user_profile["tokenHash"] == graph.hash_access_token(access_token)
    assert graph.find_user_id_for_access_token(access_token) == "bob"
