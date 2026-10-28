"""A home (org) and its office staff: ORG#<orgId>/PROFILE {orgName, verifiedAddresses, nameVariants, timezone},
ORG#<orgId>/ADMIN#<userId> {role: AdminRole}, ORG#<orgId>/MEMBER#<receiverEmail> {tags, ...} and the office's row in a
resident's circle, RECEIVER#<address>/CONTACT#org-<orgId>. An admin's org comes from their own USER#/PROFILE and the org's
ADMIN# row, never from a request body. An office row is honoured only in the circle of a resident of that same org, with
the live PROFILE's addresses and name variants (contacts_with_live_office). scripts/seed_org.py writes the org rows in one
transaction."""

import hmac
import logging
from dataclasses import dataclass, replace

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared import circle, graph, profiles, residents
from shared.enums import UserRole, VerifiedVia
from shared.org_enums import AdminRole
from shared.org_ids import (
    ORG_CONTACT_SORT_KEY_PREFIX, is_org_contact_sort_key, is_org_user_id, org_contact_sort_key_for, org_id_of_org_user,
    org_user_id_for,
)
from shared.profiles import ProfileRejectionReason, ReceiverProfile
from shared.sources import query_all_pages

logger = logging.getLogger(__name__)
ADMIN_SORT_KEY_PREFIX = "ADMIN#"
MEMBER_SORT_KEY_PREFIX = "MEMBER#"
ORG_PROFILE_TEXT_FIELDS = ("orgName", "timezone")
ORG_PROFILE_LIST_FIELDS = ("verifiedAddresses", "nameVariants")
TRANSACTION_ITEMS_MAXIMUM = 100  # DynamoDB's TransactWriteItems limit: the seed is one transaction


@dataclass(frozen=True)
class OrgProfile:
    org_id: str
    org_name: str
    verified_addresses: tuple[str, ...]
    name_variants: tuple[str, ...]
    timezone: str  # an IANA name


@dataclass(frozen=True)
class OrgAdmin:
    user_id: str
    org_id: str
    admin_role: AdminRole


@dataclass(frozen=True)
class AdminSeed:
    user_id: str
    admin_role: AdminRole


@dataclass(frozen=True)
class MemberSeed:
    receiver_address: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class OrgSeed:
    org_id: str
    org_name: str
    verified_addresses: tuple[str, ...]
    name_variants: tuple[str, ...]
    timezone: str  # an IANA name
    admins: tuple[AdminSeed, ...]
    members: tuple[MemberSeed, ...]


def org_partition(org_id: str) -> str:
    return f"ORG#{org_id}"


def is_text_list(row: dict, field_name: str) -> bool:
    return field_name in row and isinstance(row[field_name], list) and all(isinstance(value, str) for value in row[field_name])


def read_org_profile(org_id: str) -> OrgProfile | None:
    """ORG#<orgId>/PROFILE, or None when there is no row. Raises ValueError when a field is absent or not of its type."""
    response = graph.get_table().get_item(Key={"PK": org_partition(org_id), "SK": circle.PROFILE_SORT_KEY})
    if "Item" not in response:
        return None
    row = response["Item"]
    for field_name in ORG_PROFILE_TEXT_FIELDS:
        if not profiles.is_filled_text(row, field_name):
            raise ValueError(f"org profile {field_name} is not filled text")
    for field_name in ORG_PROFILE_LIST_FIELDS:
        if not is_text_list(row, field_name):
            raise ValueError(f"org profile {field_name} is not a list of text")
    return OrgProfile(org_id, row["orgName"], tuple(row["verifiedAddresses"]), tuple(row["nameVariants"]), row["timezone"])


def read_admin_role(org_id: str, user_id: str) -> AdminRole | None:
    """The role on ORG#<orgId>/ADMIN#<userId>; None when there is no row, no role, or a role outside AdminRole."""
    response = graph.get_table().get_item(Key={"PK": org_partition(org_id), "SK": f"{ADMIN_SORT_KEY_PREFIX}{user_id}"})
    if "Item" not in response or "role" not in response["Item"]:
        return None
    try:
        return AdminRole(response["Item"]["role"])
    except ValueError:
        return None


def require_org_admin(user_id: str) -> OrgAdmin | None:
    """The caller as an org admin: USER#<id>/PROFILE role ORG_ADMIN with an orgId, and an ORG#<orgId>/ADMIN#<userId> row
    holding an AdminRole. Else None, counted once as ProfileRejected: by get_user_profile (no row, unknown role), here as
    NOT_ORG_ADMIN, NO_ORG_ID or NOT_IN_ORG."""
    user_profile = profiles.get_user_profile(user_id)
    if user_profile is None:
        return None
    if user_profile.role is not UserRole.ORG_ADMIN:
        profiles.reject_profile(user_id, ProfileRejectionReason.NOT_ORG_ADMIN)
        return None
    if user_profile.org_id is None:
        profiles.reject_profile(user_id, ProfileRejectionReason.NO_ORG_ID)
        return None
    admin_role = read_admin_role(user_profile.org_id, user_id)
    if admin_role is None:
        profiles.reject_profile(user_id, ProfileRejectionReason.NOT_IN_ORG)
        return None
    return OrgAdmin(user_id, user_profile.org_id, admin_role)


def list_admin_user_ids(org_id: str) -> list[str]:
    """The user id of every ORG#<orgId>/ADMIN#<userId> row, every page read."""
    admin_rows = query_all_pages(
        KeyConditionExpression=Key("PK").eq(org_partition(org_id)) & Key("SK").begins_with(ADMIN_SORT_KEY_PREFIX))
    return [admin_row["SK"].removeprefix(ADMIN_SORT_KEY_PREFIX) for admin_row in admin_rows]


def recipient_user_ids_for(user_id: str) -> list[str]:
    """Who a message to this user reaches: for an org user "org-<orgId>", every admin of the org (none: logged as a warning,
    and nobody is reached); for anyone else, the user alone."""
    if not is_org_user_id(user_id):
        return [user_id]
    admin_user_ids = list_admin_user_ids(org_id_of_org_user(user_id))
    if not admin_user_ids:
        logger.warning("org user %s has no admins; the message reaches nobody", user_id)
    return admin_user_ids


def is_admin_of_org_user(sender_user_id: str, org_user_id: str) -> bool:
    """Whether the sender is a complete admin (require_org_admin) of the org behind "org-<orgId>", the org ids compared in
    constant time. Raises ValueError when org_user_id is not an org user id."""
    org_id = org_id_of_org_user(org_user_id)
    org_admin = require_org_admin(sender_user_id)
    return org_admin is not None and hmac.compare_digest(org_admin.org_id.encode(), org_id.encode())


def list_member_rows(org_id: str) -> list[dict]:
    """Every ORG#<orgId>/MEMBER#<receiverEmail> row, every page read."""
    return query_all_pages(
        KeyConditionExpression=Key("PK").eq(org_partition(org_id)) & Key("SK").begins_with(MEMBER_SORT_KEY_PREFIX))


def org_contact_row(receiver_address: str, org_profile: OrgProfile, added_at: str) -> dict:
    """The office's CONTACT#org-<orgId> row in the resident's circle: named after the org, its verified addresses (casefolded)
    as the only known sender addresses, its name variants, verifiedVia ORG. No reach points: asks reach the admins in-app."""
    address = profiles.normalize_receiver_address(receiver_address)
    return {
        "PK": f"RECEIVER#{address}", "SK": org_contact_sort_key_for(org_profile.org_id), "contactName": org_profile.org_name,
        "contactPwaUserId": org_user_id_for(org_profile.org_id),
        "knownAddresses": [verified_address.casefold() for verified_address in org_profile.verified_addresses],
        "nameVariants": list(org_profile.name_variants), "verifiedVia": str(VerifiedVia.ORG), "verifiedAt": added_at,
        "addedAt": added_at,
    }


def is_residents_own_org(resident: ReceiverProfile, org_id: str) -> bool:
    """The one rule for an office: it counts for a resident only when its org is the resident's own orgId (constant time)."""
    return hmac.compare_digest(org_id.encode(), resident.org_id.encode())


def contacts_with_live_office(resident: ReceiverProfile, contacts: list[graph.Contact]) -> list[graph.Contact]:
    """Gate 1's contact list for this resident: the office row of the resident's own home (CONTACT#org-<resident.org_id>,
    is_residents_own_org) carries the live verifiedAddresses (casefolded) and nameVariants of ORG#<resident.org_id>/PROFILE
    instead of the copy taken when it was added; any other home's office row is dropped (logged). Raises LookupError when the
    own office row is there and that PROFILE is not."""
    trusted_contacts = []
    for contact in contacts:
        if not is_org_contact_sort_key(contact.contact_key):
            trusted_contacts.append(contact)
        elif is_residents_own_org(resident, contact.contact_key.removeprefix(ORG_CONTACT_SORT_KEY_PREFIX)):
            trusted_contacts.append(with_live_org_profile(contact, resident.org_id))
        else:
            logger.info("office row %s of a home other than %s's is not honoured", contact.contact_key, resident.receiver_user_id)
    return trusted_contacts


def with_live_org_profile(office: graph.Contact, org_id: str) -> graph.Contact:
    """The office contact with the known addresses and name variants of its org's PROFILE; LookupError when there is none."""
    org_profile = read_org_profile(org_id)
    if org_profile is None:
        raise LookupError(f"org {org_id} has no PROFILE: its office row cannot be trusted")
    return replace(office, known_addresses=tuple(address.casefold() for address in org_profile.verified_addresses),
                   name_variants=org_profile.name_variants)


def read_request_contact_row(request_row: dict, resident: ReceiverProfile) -> dict | None:
    """The CONTACT# row a request was held for, when it still counts. A family row must exist (a missing one raises
    LookupError: only remove_org_contact deletes a row). An office row counts only when it exists and its org is the
    resident's own (is_residents_own_org); a removed one, or a former home's after re-enrolment, is None, logged with the
    request id: that office is not reached."""
    contact_sort_key = request_row["contactSK"]
    contact_row = graph.get_contact_row(request_row["receiverEmail"], contact_sort_key)
    request_id = graph.request_id_of(request_row)
    if not is_org_contact_sort_key(contact_sort_key):
        if contact_row is None:
            raise LookupError(f"request {request_id}: its contact row is gone")
        return contact_row
    if contact_row is None:
        logger.info("request %s: the resident removed the office row; the office is not reached", request_id)
        return None
    if not is_residents_own_org(resident, contact_sort_key.removeprefix(ORG_CONTACT_SORT_KEY_PREFIX)):
        logger.info("request %s: held for a former home's office; that office is not reached", request_id)
        return None
    return contact_row


def contact_name_for_request(contact_row: dict | None, resident: ReceiverProfile) -> str:
    """The name the resident's request_state push shows: the contactName of the row read_request_contact_row returned, or,
    when it returned None (an office that is not reached), the orgName of the resident's current home,
    ORG#<resident.org_id>/PROFILE. Raises LookupError when that PROFILE is missing."""
    if contact_row is not None:
        return contact_row["contactName"]
    org_profile = read_org_profile(resident.org_id)
    if org_profile is None:
        raise LookupError(f"org {resident.org_id} has no PROFILE: the office has no name")
    return org_profile.org_name


def require_filled_text(value, field_name: str) -> str:
    """The text with its outer spaces removed; raises ValueError when it is not text or is empty."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is empty")
    return value.strip()


def validate_admin_seed(admin: AdminSeed) -> AdminSeed:
    if not residents.is_enrollable_user_id(admin.user_id):
        raise ValueError("admin user id")
    return AdminSeed(admin.user_id, AdminRole(admin.admin_role))


def validate_member_seed(member: MemberSeed, org_id: str) -> MemberSeed:
    """The member with its address parsed; raises ValueError when it is not an enrolled resident of this org or has an
    empty tag."""
    address = circle.parse_email_address(member.receiver_address)
    try:
        resident = profiles.require_receiver_profile(address)
    except profiles.UnknownReceiverError:
        raise ValueError("a member is not an enrolled resident") from None
    if resident.org_id != org_id:
        raise ValueError("a member is enrolled in another org")
    return MemberSeed(address, tuple(require_filled_text(tag, "tag") for tag in member.tags))


def validate_org_seed(seed: OrgSeed) -> OrgSeed:
    """The seed with its text trimmed and its addresses parsed and normalized. Raises ValueError on an org id outside
    ORG_ID_PATTERN, an empty org name or name variant, no verified address or a malformed one, a timezone that is not a
    loadable IANA name, no admin, a repeated admin or member, more items than one transaction holds, an admin id outside
    USER_ID_PATTERN or starting "org-", a role outside AdminRole, or a member who is not an enrolled resident of this org."""
    org_user_id_for(seed.org_id)  # raises ValueError off ORG_ID_PATTERN
    org_name = require_filled_text(seed.org_name, "org name")
    verified_addresses = tuple(circle.parse_email_address(address) for address in seed.verified_addresses)
    if not verified_addresses:
        raise ValueError("no verified address")
    name_variants = tuple(require_filled_text(name_variant, "name variant") for name_variant in seed.name_variants)
    residents.validate_timezone(seed.timezone)
    if not seed.admins:
        raise ValueError("no admin")
    if 1 + 2 * len(seed.admins) + len(seed.members) > TRANSACTION_ITEMS_MAXIMUM:
        raise ValueError(f"more than {TRANSACTION_ITEMS_MAXIMUM} items for one transaction")
    admins = tuple(validate_admin_seed(admin) for admin in seed.admins)
    members = tuple(validate_member_seed(member, seed.org_id) for member in seed.members)
    if len({admin.user_id for admin in admins}) != len(admins):
        raise ValueError("an admin is listed twice")
    if len({member.receiver_address for member in members}) != len(members):
        raise ValueError("a member is listed twice")
    return OrgSeed(seed.org_id, org_name, verified_addresses, name_variants, seed.timezone, admins, members)


def admin_transaction_items(table_name: str, org_id: str, admin: AdminSeed) -> list[dict]:
    """Put ORG#<orgId>/ADMIN#<userId>; on USER#<userId>/PROFILE SET role ORG_ADMIN and orgId, only when the row has no role
    yet or ORG_ADMIN, and no orgId yet or this one."""
    admin_row = {"PK": org_partition(org_id), "SK": f"{ADMIN_SORT_KEY_PREFIX}{admin.user_id}", "role": str(admin.admin_role)}
    return [
        {"Put": {"TableName": table_name, "Item": admin_row}},
        {"Update": {"TableName": table_name, "Key": {"PK": f"USER#{admin.user_id}", "SK": circle.PROFILE_SORT_KEY},
                    "UpdateExpression": "SET #role = :orgAdmin, orgId = :orgId",
                    "ConditionExpression": ("(attribute_not_exists(#role) OR #role = :orgAdmin) "
                                            "AND (attribute_not_exists(orgId) OR orgId = :orgId)"),
                    "ExpressionAttributeNames": {"#role": "role"},
                    "ExpressionAttributeValues": {":orgAdmin": str(UserRole.ORG_ADMIN), ":orgId": org_id}}},
    ]


def member_tags_update(table_name: str, org_id: str, member: MemberSeed) -> dict:
    """SET tags on ORG#<orgId>/MEMBER#<address>, never a Put: the member's other fields survive a re-seed."""
    return {"Update": {"TableName": table_name,
                       "Key": {"PK": org_partition(org_id), "SK": f"{MEMBER_SORT_KEY_PREFIX}{member.receiver_address}"},
                       "UpdateExpression": "SET tags = :tags", "ExpressionAttributeValues": {":tags": list(member.tags)}}}


def unlisted_admin_deletes(table_name: str, seed: OrgSeed) -> list[dict]:
    """Delete ORG#<orgId>/ADMIN#<userId> for every stored admin the seed does not list; their USER#/PROFILE is left as it is
    (require_org_admin refuses them: NOT_IN_ORG)."""
    listed_user_ids = {admin.user_id for admin in seed.admins}
    return [{"Delete": {"TableName": table_name, "Key": {"PK": org_partition(seed.org_id), "SK": f"{ADMIN_SORT_KEY_PREFIX}{user_id}"}}}
            for user_id in list_admin_user_ids(seed.org_id) if user_id not in listed_user_ids]


def write_org_seed(seed: OrgSeed) -> None:
    """In ONE transaction: Put ORG#<orgId>/PROFILE, each admin's ADMIN# row and role, each member's tags, and delete the ADMIN#
    row of every admin of this org the seed does not list. Writes no RECEIVER# row. Raises ValueError when the items with
    those deletes exceed one transaction, or when an admin's condition fails (the user id holds another role, e.g. a
    resident's or a contact's, or is another org's admin); nothing is written then. The seed is validate_org_seed's output."""
    table = graph.get_table()
    profile_row = {"PK": org_partition(seed.org_id), "SK": circle.PROFILE_SORT_KEY, "orgName": seed.org_name,
                   "verifiedAddresses": list(seed.verified_addresses), "nameVariants": list(seed.name_variants),
                   "timezone": seed.timezone}
    transaction_items = [{"Put": {"TableName": table.name, "Item": profile_row}}]
    for admin in seed.admins:
        transaction_items.extend(admin_transaction_items(table.name, seed.org_id, admin))
    transaction_items.extend(member_tags_update(table.name, seed.org_id, member) for member in seed.members)
    transaction_items.extend(unlisted_admin_deletes(table.name, seed))
    if len(transaction_items) > TRANSACTION_ITEMS_MAXIMUM:
        raise ValueError(f"more than {TRANSACTION_ITEMS_MAXIMUM} items for one transaction")
    try:
        table.meta.client.transact_write_items(TransactItems=transaction_items)
    except ClientError as error:
        if error.response["Error"]["Code"] == circle.TRANSACTION_CANCELLED:
            raise ValueError("an admin user id already holds another role or belongs to another org") from None
        raise
