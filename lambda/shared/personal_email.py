"""A resident's personal email: the mailbox her recovery pack is mailed to, set from her Settings screen in two steps.
start_change stores the address as pending with a 6-digit code and mails the code to it; confirm_change saves it when the
same code comes back from her own app before it expires. The fields live on RECEIVER#<address>/PROFILE: personalEmail and
personalEmailVerifiedAt once confirmed; pendingPersonalEmail, pendingPersonalEmailCodeHash, pendingPersonalEmailExpiresAt
and pendingPersonalEmailAttempts while a change waits. Logs carry the user id, never the address or the code."""

import hmac
import logging
import re
from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError

from shared import graph, hourly_counters
from shared.circle import PROFILE_SORT_KEY, new_verification_code
from shared.enums import ActionOutcome
from shared.metrics import emit_count_metric
from shared.profiles import ReceiverProfile, is_filled_text, read_profile_row

logger = logging.getLogger(__name__)

PERSONAL_EMAIL_FIELD = "personalEmail"
PERSONAL_EMAIL_VERIFIED_AT_FIELD = "personalEmailVerifiedAt"
PENDING_EMAIL_FIELD = "pendingPersonalEmail"
PENDING_CODE_HASH_FIELD = "pendingPersonalEmailCodeHash"
PENDING_EXPIRES_AT_FIELD = "pendingPersonalEmailExpiresAt"
PENDING_ATTEMPTS_FIELD = "pendingPersonalEmailAttempts"
PENDING_FIELDS = (PENDING_EMAIL_FIELD, PENDING_CODE_HASH_FIELD, PENDING_EXPIRES_AT_FIELD, PENDING_ATTEMPTS_FIELD)
CODE_LIFETIME = timedelta(minutes=15)
CODE_ATTEMPTS_LIMIT = 5  # wrong codes per pending change; then she asks for a new code
CODE_EMAILS_PER_HOUR = 5  # code emails per resident per hour (fails closed)
CODE_RATE_PARTITION_PREFIX = "MAILCODERATE#"
ADDRESS_MAXIMUM_LENGTH = 254
ADDRESS_PATTERN = re.compile(r"^[^@\s<>,;:\"()\[\]\\]+@[a-z0-9-]+(\.[a-z0-9-]+)+$")
CODE_EMAIL_SUBJECT = "SendPidge: your code"
CODE_EMAIL_FAILED_METRIC_NAME = "PersonalEmailCodeFailed"


def parse_personal_email(raw_address: object, resident: ReceiverProfile, protected_domains: frozenset[str]) -> str | None:
    """The address lower-cased, or None when it is not text, malformed, too long, her own protected address, or on one of
    our own mail domains (mail there re-enters Mail Manager instead of reaching her)."""
    if not isinstance(raw_address, str):
        return None
    address = raw_address.strip().casefold()
    if len(address) > ADDRESS_MAXIMUM_LENGTH or ADDRESS_PATTERN.fullmatch(address) is None:
        return None
    domain = address.rsplit("@", 1)[1]
    if address == resident.receiver_address or any(domain == own or domain.endswith(f".{own}") for own in protected_domains):
        return None
    return address


def parse_code(raw_code: object) -> str | None:
    return raw_code.strip() if isinstance(raw_code, str) and re.fullmatch(r"\d{6}", raw_code.strip()) else None


def profile_key(resident: ReceiverProfile) -> dict:
    return {"PK": f"RECEIVER#{resident.receiver_address}", "SK": PROFILE_SORT_KEY}


def write_pending_change(resident: ReceiverProfile, address: str, code: str, now: datetime) -> None:
    """Replaces any earlier pending change; only on her own PROFILE row, still naming her."""
    graph.get_table().update_item(
        Key=profile_key(resident),
        UpdateExpression="SET #email = :email, #hash = :hash, #expires = :expires, #attempts = :zero",
        ConditionExpression="attribute_exists(PK) AND receiverUserId = :userId",
        ExpressionAttributeNames={"#email": PENDING_EMAIL_FIELD, "#hash": PENDING_CODE_HASH_FIELD,
                                  "#expires": PENDING_EXPIRES_AT_FIELD, "#attempts": PENDING_ATTEMPTS_FIELD},
        ExpressionAttributeValues={":email": address, ":hash": graph.hash_access_token(code),
                                   ":expires": graph.iso_timestamp(now + CODE_LIFETIME), ":zero": 0,
                                   ":userId": resident.receiver_user_id},
    )


def send_code_email(ses_client, sender_address: str, address: str, code: str) -> None:
    body = (f"Your SendPidge code is {code}.\n\nType it in the SendPidge app to save this as your personal email. "
            f"It expires in {int(CODE_LIFETIME.total_seconds() // 60)} minutes.\n\nIf you did not ask for this, ignore this email.")
    ses_client.send_email(
        FromEmailAddress=sender_address,
        Destination={"ToAddresses": [address]},
        Content={"Simple": {"Subject": {"Data": CODE_EMAIL_SUBJECT}, "Body": {"Text": {"Data": body}}}},
    )


def start_change(resident: ReceiverProfile, raw_address: object, now: datetime, ses_client, sender_address: str,
                 protected_domains: frozenset[str]) -> ActionOutcome:
    """BAD_MESSAGE for an unusable address; OVER_CAP when this hour's code emails are used up or the counter cannot be
    written; SEND_FAILED when SES refuses (the pending change stays, so she can ask again); RECORDED once the code is mailed."""
    address = parse_personal_email(raw_address, resident, protected_domains)
    if address is None:
        return ActionOutcome.BAD_MESSAGE
    if not hourly_counters.take_hourly_slot(f"{CODE_RATE_PARTITION_PREFIX}{resident.receiver_user_id}", CODE_EMAILS_PER_HOUR):
        return ActionOutcome.OVER_CAP
    code = new_verification_code()
    write_pending_change(resident, address, code, now)
    try:
        send_code_email(ses_client, sender_address, address, code)
    except Exception:
        logger.exception("personal email code for %s was not sent", resident.receiver_user_id)
        emit_count_metric(CODE_EMAIL_FAILED_METRIC_NAME)
        return ActionOutcome.SEND_FAILED
    logger.info("personal email code sent for %s", resident.receiver_user_id)
    return ActionOutcome.RECORDED


def is_pending_live(profile_row: dict, now: datetime) -> bool:
    return all(field_name in profile_row for field_name in PENDING_FIELDS) and (
        datetime.fromisoformat(profile_row[PENDING_EXPIRES_AT_FIELD].replace("Z", "+00:00")) >= now.astimezone(timezone.utc))


def take_code_attempt(resident: ReceiverProfile, code_hash: str) -> bool:
    """Counts one attempt on this exact pending change; False once CODE_ATTEMPTS_LIMIT is used or the change was replaced."""
    try:
        graph.get_table().update_item(
            Key=profile_key(resident),
            UpdateExpression="ADD #attempts :one",
            ConditionExpression="#hash = :hash AND #attempts < :limit",
            ExpressionAttributeNames={"#attempts": PENDING_ATTEMPTS_FIELD, "#hash": PENDING_CODE_HASH_FIELD},
            ExpressionAttributeValues={":one": 1, ":hash": code_hash, ":limit": CODE_ATTEMPTS_LIMIT},
        )
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] == graph.CONDITIONAL_CHECK_FAILED:
            return False
        raise


def save_confirmed_address(resident: ReceiverProfile, profile_row: dict, now: datetime) -> bool:
    """Moves the pending address to personalEmail, only while the same pending change is on the row."""
    try:
        graph.get_table().update_item(
            Key=profile_key(resident),
            UpdateExpression="SET #email = :pending, #verifiedAt = :now REMOVE " + ", ".join(f"#p{index}" for index in range(len(PENDING_FIELDS))),
            ConditionExpression="#p0 = :pending AND #p1 = :hash",
            ExpressionAttributeNames={"#email": PERSONAL_EMAIL_FIELD, "#verifiedAt": PERSONAL_EMAIL_VERIFIED_AT_FIELD,
                                      **{f"#p{index}": field_name for index, field_name in enumerate(PENDING_FIELDS)}},
            ExpressionAttributeValues={":pending": profile_row[PENDING_EMAIL_FIELD], ":hash": profile_row[PENDING_CODE_HASH_FIELD],
                                       ":now": graph.iso_timestamp(now)},
        )
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] == graph.CONDITIONAL_CHECK_FAILED:
            return False
        raise


def confirm_change(resident: ReceiverProfile, raw_code: object, now: datetime) -> ActionOutcome:
    """BAD_MESSAGE for a code that is not 6 digits; NOT_APPLIED when no change is waiting; CODE_EXPIRED when it expired or its
    attempts are used up; WRONG_CODE for another code; RECORDED once the address is saved."""
    code = parse_code(raw_code)
    if code is None:
        return ActionOutcome.BAD_MESSAGE
    profile_row = read_profile_row(f"RECEIVER#{resident.receiver_address}")
    if profile_row is None or not all(field_name in profile_row for field_name in PENDING_FIELDS):
        return ActionOutcome.NOT_APPLIED
    if not is_pending_live(profile_row, now) or not take_code_attempt(resident, profile_row[PENDING_CODE_HASH_FIELD]):
        return ActionOutcome.CODE_EXPIRED
    if not hmac.compare_digest(graph.hash_access_token(code).encode(), profile_row[PENDING_CODE_HASH_FIELD].encode()):
        logger.info("wrong personal email code for %s", resident.receiver_user_id)
        return ActionOutcome.WRONG_CODE
    if not save_confirmed_address(resident, profile_row, now):
        return ActionOutcome.NOT_APPLIED  # a newer change replaced this one meanwhile
    logger.info("personal email saved for %s", resident.receiver_user_id)
    return ActionOutcome.RECORDED


def verified_personal_email(receiver_address: str) -> str | None:
    """Her confirmed personal email, or None when she has not saved one (the recovery pack then follows its older rule)."""
    profile_row = read_profile_row(f"RECEIVER#{receiver_address}")
    return profile_row[PERSONAL_EMAIL_FIELD] if profile_row is not None and is_filled_text(profile_row, PERSONAL_EMAIL_FIELD) else None


def personal_email_for_sync(resident: ReceiverProfile, now: datetime) -> dict:
    """Her Settings: the protected address, the saved personal email (or None), and the address waiting for its code (or None)."""
    profile_row = read_profile_row(f"RECEIVER#{resident.receiver_address}")
    if profile_row is None:
        raise ValueError("the resident's PROFILE row vanished between authorisation and sync")
    saved = profile_row[PERSONAL_EMAIL_FIELD] if is_filled_text(profile_row, PERSONAL_EMAIL_FIELD) else None
    pending = profile_row[PENDING_EMAIL_FIELD] if is_pending_live(profile_row, now) else None
    return {"protectedEmail": resident.receiver_address, "personalEmail": saved, "pendingPersonalEmail": pending}
