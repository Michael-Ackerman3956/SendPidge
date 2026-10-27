"""Who a resident is and who a PWA user is, read from their PROFILE rows: RECEIVER#<address>/PROFILE {receiverUserId,
residentName, orgId, timezone} and USER#<id>/PROFILE {tokenHash, role, and for a resident receiverEmail, orgId}. A missing or
incomplete row is never filled with a default: a receiver profile is counted and raised; a user profile or a resident
caller is None, logged with the reason and counted as ProfileRejected."""

import hmac
import logging
from dataclasses import dataclass
from enum import StrEnum

from shared import graph
from shared.circle import PROFILE_SORT_KEY
from shared.enums import UserRole
from shared.metrics import emit_count_metric

logger = logging.getLogger(__name__)
UNKNOWN_RECEIVER_METRIC_NAME = "UnknownReceiver"
PROFILE_REJECTED_METRIC_NAME = "ProfileRejected"
PROFILE_REJECTED_DIMENSION_NAME = "reason"
RECEIVER_PROFILE_FIELDS = ("receiverUserId", "residentName", "orgId", "timezone")


class ProfileRejectionReason(StrEnum):
    NO_PROFILE = "no_profile"  # no USER#<id>/PROFILE row, or no role on it
    UNKNOWN_ROLE = "unknown_role"  # the role is not a UserRole value
    NO_RECEIVER_EMAIL = "no_receiver_email"  # a RECEIVER row without receiverEmail
    NOT_RECEIVER = "not_receiver"  # a resident action from a role other than RECEIVER
    RECEIVER_MISMATCH = "receiver_mismatch"  # RECEIVER#<receiverEmail> names another user: the cross-resident signal


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


def reject_profile(user_id: str, reason: ProfileRejectionReason) -> None:
    """Logs the reason and the user id (never a token) and counts ProfileRejected by reason."""
    logger.warning("profile rejected for user %s: %s", user_id, reason)
    emit_count_metric(PROFILE_REJECTED_METRIC_NAME, PROFILE_REJECTED_DIMENSION_NAME, str(reason))


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
    """The user's role from USER#<id>/PROFILE. None (rejected: logged and counted) when there is no row, the role is absent
    or not a UserRole value, or a RECEIVER row has no receiverEmail."""
    row = read_profile_row(f"USER#{user_id}")
    if row is None or "role" not in row:
        reject_profile(user_id, ProfileRejectionReason.NO_PROFILE)
        return None
    try:
        role = UserRole(row["role"])
    except ValueError:
        reject_profile(user_id, ProfileRejectionReason.UNKNOWN_ROLE)
        return None
    receiver_email = row["receiverEmail"] if is_filled_text(row, "receiverEmail") else None
    if role is UserRole.RECEIVER and receiver_email is None:
        reject_profile(user_id, ProfileRejectionReason.NO_RECEIVER_EMAIL)
        return None
    return UserProfile(user_id, role, receiver_email)


def require_resident_caller(user_id: str) -> ReceiverProfile | None:
    """The caller's own resident profile: their PROFILE role is RECEIVER and RECEIVER#<receiverEmail> names them back
    (constant-time compare). Else None: a rejected PROFILE is counted once by get_user_profile, another role or a RECEIVER#
    row naming another user as ProfileRejected here, and a missing or incomplete RECEIVER# row by require_receiver_profile."""
    user_profile = get_user_profile(user_id)
    if user_profile is None:
        return None
    if user_profile.role is not UserRole.RECEIVER:
        reject_profile(user_id, ProfileRejectionReason.NOT_RECEIVER)
        return None
    try:
        resident = require_receiver_profile(user_profile.receiver_email)
    except UnknownReceiverError:
        return None
    if not hmac.compare_digest(resident.receiver_user_id.encode(), user_id.encode()):
        reject_profile(user_id, ProfileRejectionReason.RECEIVER_MISMATCH)
        return None
    return resident
