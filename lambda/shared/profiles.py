"""Who a resident is and who a PWA user is, read from their PROFILE rows: RECEIVER#<address>/PROFILE {receiverUserId,
residentName, orgId, timezone} and USER#<id>/PROFILE {tokenHash, role, and for a resident receiverEmail, orgId}. A missing or
incomplete row is never filled with a default: a receiver profile is counted and raised, a user profile is None."""

import hmac
from dataclasses import dataclass

from shared import graph
from shared.circle import PROFILE_SORT_KEY
from shared.enums import UserRole
from shared.metrics import emit_count_metric

UNKNOWN_RECEIVER_METRIC_NAME = "UnknownReceiver"
RECEIVER_PROFILE_FIELDS = ("receiverUserId", "residentName", "orgId", "timezone")


class UnknownReceiverError(Exception):
    """No complete RECEIVER#<address>/PROFILE row: the address is not an enrolled resident."""


@dataclass(frozen=True)
class ReceiverProfile:
    receiver_address: str
    receiver_user_id: str
    resident_name: str
    org_id: str
    timezone: str  # an IANA name


@dataclass(frozen=True)
class UserProfile:
    user_id: str
    role: UserRole
    receiver_email: str | None  # set for a RECEIVER


def normalize_receiver_address(address: str) -> str:
    return address.strip().casefold()


def is_filled_text(row: dict, field_name: str) -> bool:
    return field_name in row and isinstance(row[field_name], str) and row[field_name] != ""


def read_profile_row(partition_key: str) -> dict | None:
    response = graph.get_table().get_item(Key={"PK": partition_key, "SK": PROFILE_SORT_KEY})
    return response["Item"] if "Item" in response else None


def require_receiver_profile(receiver_address: str) -> ReceiverProfile:
    """The resident behind this address. No row, or any field absent, empty or not text: counts UnknownReceiver (no
    dimension, never the address) and raises UnknownReceiverError."""
    address = normalize_receiver_address(receiver_address)
    row = read_profile_row(f"RECEIVER#{address}")
    if row is None or not all(is_filled_text(row, field_name) for field_name in RECEIVER_PROFILE_FIELDS):
        emit_count_metric(UNKNOWN_RECEIVER_METRIC_NAME)
        raise UnknownReceiverError("no complete RECEIVER# profile for this address")
    return ReceiverProfile(address, row["receiverUserId"], row["residentName"], row["orgId"], row["timezone"])


def get_user_profile(user_id: str) -> UserProfile | None:
    """The user's role from USER#<id>/PROFILE. None when there is no row, the role is absent or not a UserRole value, or a
    RECEIVER row has no receiverEmail."""
    row = read_profile_row(f"USER#{user_id}")
    if row is None or "role" not in row:
        return None
    try:
        role = UserRole(row["role"])
    except ValueError:
        return None
    receiver_email = row["receiverEmail"] if is_filled_text(row, "receiverEmail") else None
    if role is UserRole.RECEIVER and receiver_email is None:
        return None
    return UserProfile(user_id, role, receiver_email)


def require_resident_caller(user_id: str) -> ReceiverProfile | None:
    """The caller's own resident profile: their PROFILE role is RECEIVER and RECEIVER#<receiverEmail> names them back
    (constant-time compare). Else None; a missing or incomplete RECEIVER# row is also counted by require_receiver_profile."""
    user_profile = get_user_profile(user_id)
    if user_profile is None or user_profile.role is not UserRole.RECEIVER:
        return None
    try:
        resident = require_receiver_profile(user_profile.receiver_email)
    except UnknownReceiverError:
        return None
    if not hmac.compare_digest(resident.receiver_user_id.encode(), user_id.encode()):
        return None
    return resident
