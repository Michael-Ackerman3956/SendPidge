"""Enrolling a resident (scripts/enrol_resident.py): the rows first, then the Mail Manager address list, so mail accepted the
moment the address is listed always finds a profile. Unenrolling runs the other way: delisted first, then the rows go."""

import re
from dataclasses import dataclass, replace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from botocore.exceptions import ClientError

from shared import circle, graph, profiles
from shared.enums import UserRole
from shared.org_ids import is_org_contact_sort_key, is_org_user_id, org_user_id_for

USER_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")  # used with fullmatch; it also names the .env.tokens.<userId> file


@dataclass(frozen=True)
class ResidentEnrolment:
    receiver_address: str
    receiver_user_id: str
    resident_name: str
    org_id: str
    timezone: str  # an IANA name


def write_resident_rows(resident: ResidentEnrolment) -> None:
    """In one transaction: RECEIVER#<address>/PROFILE (new, or already this user's) and role RECEIVER, receiverEmail, orgId on
    USER#<id>/PROFILE (no role yet or already RECEIVER, no other address). Raises ValueError when either condition fails: the
    user id is a contact's or another resident's, or the address belongs to another user."""
    address = profiles.normalize_receiver_address(resident.receiver_address)
    table = graph.get_table()
    receiver_profile_row = {
        "PK": f"RECEIVER#{address}", "SK": circle.PROFILE_SORT_KEY, "receiverUserId": resident.receiver_user_id,
        "residentName": resident.resident_name, "orgId": resident.org_id, "timezone": resident.timezone,
    }
    transaction_items = [
        {"Put": {"TableName": table.name, "Item": receiver_profile_row,
                 "ConditionExpression": "attribute_not_exists(PK) OR receiverUserId = :userId",
                 "ExpressionAttributeValues": {":userId": resident.receiver_user_id}}},
        {"Update": {"TableName": table.name, "Key": {"PK": f"USER#{resident.receiver_user_id}", "SK": circle.PROFILE_SORT_KEY},
                    "UpdateExpression": "SET #role = :receiver, receiverEmail = :address, orgId = :orgId",
                    "ConditionExpression": ("(attribute_not_exists(#role) OR #role = :receiver) "
                                            "AND (attribute_not_exists(receiverEmail) OR receiverEmail = :address)"),
                    "ExpressionAttributeNames": {"#role": "role"},
                    "ExpressionAttributeValues": {":receiver": str(UserRole.RECEIVER), ":address": address,
                                                  ":orgId": resident.org_id}}},
    ]
    try:
        table.meta.client.transact_write_items(TransactItems=transaction_items)
    except ClientError as error:
        if error.response["Error"]["Code"] == circle.TRANSACTION_CANCELLED:
            raise ValueError("the user id or the address already belongs to someone else") from None
        raise


def mark_circle_contacts(receiver_address: str) -> list[str]:
    """Sets role CONTACT on the PROFILE of every person in the resident's circle; the home office's CONTACT#org-<orgId> row
    is passed over, unreported (the org user never has a PROFILE). Returns the contact user ids skipped: no PROFILE row yet
    (no token issued), or a PROFILE that already holds another role."""
    skipped_contact_user_ids = []
    for contact_row in graph.list_contact_rows(profiles.normalize_receiver_address(receiver_address)):
        if is_org_contact_sort_key(contact_row["SK"]):
            continue
        contact_user_id = contact_row["contactPwaUserId"]
        try:
            graph.get_table().update_item(
                Key={"PK": f"USER#{contact_user_id}", "SK": circle.PROFILE_SORT_KEY},
                UpdateExpression="SET #role = :contact",
                ConditionExpression="attribute_exists(PK) AND (attribute_not_exists(#role) OR #role = :contact)",
                ExpressionAttributeNames={"#role": "role"},
                ExpressionAttributeValues={":contact": str(UserRole.CONTACT)},
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != graph.CONDITIONAL_CHECK_FAILED:
                raise
            skipped_contact_user_ids.append(contact_user_id)
    return skipped_contact_user_ids


def is_enrollable_user_id(user_id) -> bool:
    """A user id a resident or an org admin may hold: text matching USER_ID_PATTERN, never an org's own "org-<orgId>"."""
    return isinstance(user_id, str) and USER_ID_PATTERN.fullmatch(user_id) is not None and not is_org_user_id(user_id)


def validate_timezone(timezone) -> None:
    """Raises ValueError when the timezone is not text or not a loadable IANA name."""
    if not isinstance(timezone, str):
        raise ValueError("timezone is not text")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("timezone is not an IANA name") from None


def validate_enrolment(resident: ResidentEnrolment) -> ResidentEnrolment:
    """The enrolment with its address parsed and normalized; raises ValueError on a malformed address, a user id outside
    USER_ID_PATTERN or starting "org-", an empty name, an org id outside org_ids.ORG_ID_PATTERN, or a timezone that is not a
    loadable IANA name."""
    address = circle.parse_email_address(resident.receiver_address)
    if not is_enrollable_user_id(resident.receiver_user_id):
        raise ValueError("user id")
    if not isinstance(resident.resident_name, str) or not resident.resident_name.strip():
        raise ValueError("resident name is empty")
    org_user_id_for(resident.org_id)  # raises ValueError off ORG_ID_PATTERN
    validate_timezone(resident.timezone)
    return replace(resident, receiver_address=address)


def enrol_resident(mailmanager_client, address_list_id: str, resident: ResidentEnrolment) -> list[str]:
    """Validates, writes the resident's rows, marks the circle's contacts, then lists the address on the Mail Manager address
    list. Returns the contact user ids mark_circle_contacts skipped. Every error raises, the address list's included."""
    resident = validate_enrolment(resident)
    write_resident_rows(resident)
    skipped_contact_user_ids = mark_circle_contacts(resident.receiver_address)
    mailmanager_client.register_member_to_address_list(AddressListId=address_list_id, Address=resident.receiver_address)
    return skipped_contact_user_ids


def unenrol_resident(mailmanager_client, address_list_id: str, receiver_address: str) -> None:
    """Delists the address first, so no new mail is accepted for it, then in one transaction deletes RECEIVER#<address>/PROFILE
    and the resident's TOKEN# row and removes role, receiverEmail, orgId (and the dead tokenHash) from USER#<id>/PROFILE.
    Raises UnknownReceiverError, before anything changes, when the address has no complete RECEIVER# profile."""
    resident = profiles.require_receiver_profile(receiver_address)
    mailmanager_client.deregister_member_from_address_list(AddressListId=address_list_id, Address=resident.receiver_address)
    table = graph.get_table()
    user_profile_key = {"PK": f"USER#{resident.receiver_user_id}", "SK": circle.PROFILE_SORT_KEY}
    user_profile_response = table.get_item(Key=user_profile_key)
    token_hash = (user_profile_response["Item"]["tokenHash"]
                  if "Item" in user_profile_response and "tokenHash" in user_profile_response["Item"] else None)
    transaction_items = [
        {"Delete": {"TableName": table.name, "Key": {"PK": f"RECEIVER#{resident.receiver_address}", "SK": circle.PROFILE_SORT_KEY}}},
        {"Update": {"TableName": table.name, "Key": user_profile_key,
                    "UpdateExpression": "REMOVE #role, receiverEmail, orgId" + (", tokenHash" if token_hash is not None else ""),
                    "ExpressionAttributeNames": {"#role": "role"}}},
    ]
    if token_hash is not None:
        transaction_items.append({"Delete": {"TableName": table.name,
                                             "Key": {"PK": f"TOKEN#{token_hash}", "SK": graph.METADATA_SORT_KEY}}})
    table.meta.client.transact_write_items(TransactItems=transaction_items)
