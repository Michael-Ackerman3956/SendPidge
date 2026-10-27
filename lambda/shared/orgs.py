"""A home (org) and its office staff: ORG#<orgId>/PROFILE {orgName, verifiedAddresses, nameVariants, timezone},
ORG#<orgId>/ADMIN#<userId> {role: AdminRole}, ORG#<orgId>/MEMBER#<receiverEmail> {tags, ...} and the office's row in a
resident's circle, RECEIVER#<address>/CONTACT#org-<orgId>. An admin's org comes from their own USER#/PROFILE and the org's
ADMIN# row, never from a request body. scripts/seed_org.py writes the org rows in one transaction."""

import hmac
import logging
from dataclasses import dataclass

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared import circle, graph, profiles, residents
from shared.enums import UserRole, VerifiedVia
from shared.org_enums import AdminRole
from shared.org_ids import is_org_user_id, org_contact_sort_key_for, org_id_of_org_user, org_user_id_for
from shared.profiles import ProfileRejectionReason
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


def write_org_seed(seed: OrgSeed) -> None:
    """In ONE transaction: Put ORG#<orgId>/PROFILE, each admin's ADMIN# row and role, each member's tags. Writes no RECEIVER#
    row. Raises ValueError when an admin's condition fails (the user id holds another role, e.g. a resident's or a contact's,
    or is another org's admin). The seed is validate_org_seed's output."""
    table = graph.get_table()
    profile_row = {"PK": org_partition(seed.org_id), "SK": circle.PROFILE_SORT_KEY, "orgName": seed.org_name,
                   "verifiedAddresses": list(seed.verified_addresses), "nameVariants": list(seed.name_variants),
                   "timezone": seed.timezone}
    transaction_items = [{"Put": {"TableName": table.name, "Item": profile_row}}]
    for admin in seed.admins:
        transaction_items.extend(admin_transaction_items(table.name, seed.org_id, admin))
    transaction_items.extend(member_tags_update(table.name, seed.org_id, member) for member in seed.members)
    try:
        table.meta.client.transact_write_items(TransactItems=transaction_items)
    except ClientError as error:
        if error.response["Error"]["Code"] == circle.TRANSACTION_CANCELLED:
            raise ValueError("an admin user id already holds another role or belongs to another org") from None
        raise
