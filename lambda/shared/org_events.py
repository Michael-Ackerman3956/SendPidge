"""The org safety log's rules: which stream record stands for which OrgEvent (events_for_record), which events a resident's
visibility lets through (passes_visibility) and the state an episode's events fold to (episode_state). Writes nothing: the
projection (shared/org_projection.py) writes; events_for_record reads only the member's RECEIVER#/PROFILE (and, for a NOTE#,
its REQUEST#/META) to name the member's org. An episode is one sourceKey, "REQUEST#req-<uuid>" or "CASE#case-<uuid>"."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from boto3.dynamodb.types import TypeDeserializer

from shared import graph, profiles
from shared.enums import CaseState, Channel, PolicyDecision, RequestState, WarnReason
from shared.org_enums import MemberSafetyState, OrgEvent, OrgVisibility, ProjectedRecordKind, StreamRecordEventName
from shared.org_preferences import ORG_PREFERENCE_SORT_KEY_PREFIX
from shared.profiles import ReceiverProfile

ORG_LOG_PARTITION_PREFIX = "ORGLOG#"
ORG_STATS_PARTITION_PREFIX = "ORGSTAT#"
ORG_STATS_DAY_SORT_KEY_PREFIX = "DAY#"
RESOLVE_SORT_KEY_PREFIX = "RESOLVE#"
CANARY_SORT_KEY = "CANARY"
REQUEST_SOURCE_PREFIX = "REQUEST#"
CASE_SOURCE_PREFIX = "CASE#"
RESOLVE_LOG_RETENTION = timedelta(days=90)  # a RESOLVE# row's ttl, from its at; its org_resolved log row copies it

CASE_EVENTS = frozenset({OrgEvent.WRONG_PAYMENT_REPORTED, OrgEvent.RECOVERY_PACK_DRAFTED, OrgEvent.CASE_CLOSED})
ANY_VISIBILITY_EVENTS = frozenset({OrgEvent.HELP_REQUESTED, OrgEvent.BROADCAST_ACKNOWLEDGED, OrgEvent.ORG_RESOLVED})

# state (None: no open episode) -> {event: next state}; an event not listed for a state leaves the state as it is.
EPISODE_TRANSITIONS: dict[MemberSafetyState | None, dict[OrgEvent, MemberSafetyState | None]] = {
    None: {OrgEvent.MESSAGE_HELD: MemberSafetyState.HELD, OrgEvent.CONTACT_CONFIRMED_NOT_ME: MemberSafetyState.CONFIRMED_NOT_ME,
           OrgEvent.WRONG_PAYMENT_REPORTED: MemberSafetyState.CASE_OPEN, OrgEvent.PAYMENT_DOUBTED: MemberSafetyState.WARNED,
           OrgEvent.CONTACT_UNREACHABLE: MemberSafetyState.CONTACT_UNREACHABLE},  # its record time may precede message_held
    MemberSafetyState.HELD: {
        OrgEvent.CONTACT_CONFIRMED_YES: None, OrgEvent.CONTACT_CONFIRMED_NOT_ME: MemberSafetyState.CONFIRMED_NOT_ME,
        OrgEvent.ASK_TIMED_OUT: MemberSafetyState.HELD_NO_ANSWER, OrgEvent.ASK_CAPPED: MemberSafetyState.HELD_NO_ANSWER,
        OrgEvent.CONTACT_UNREACHABLE: MemberSafetyState.CONTACT_UNREACHABLE},
    MemberSafetyState.HELD_NO_ANSWER: {
        OrgEvent.CONTACT_CONFIRMED_YES: None, OrgEvent.CONTACT_CONFIRMED_NOT_ME: MemberSafetyState.CONFIRMED_NOT_ME,
        OrgEvent.ORG_RESOLVED: None},
    MemberSafetyState.CONTACT_UNREACHABLE: {
        OrgEvent.CONTACT_CONFIRMED_YES: None, OrgEvent.CONTACT_CONFIRMED_NOT_ME: MemberSafetyState.CONFIRMED_NOT_ME,
        OrgEvent.ORG_RESOLVED: None},
    MemberSafetyState.CONFIRMED_NOT_ME: {OrgEvent.ANSWER_REVERSED: None, OrgEvent.ORG_RESOLVED: None},
    MemberSafetyState.WARNED: {OrgEvent.ORG_RESOLVED: None},
    MemberSafetyState.CASE_OPEN: {OrgEvent.CASE_CLOSED: None},
}
EVENT_FOLD_ORDER = {event: position for position, event in enumerate(OrgEvent)}
STATE_PRIORITY = (MemberSafetyState.CASE_OPEN, MemberSafetyState.CONFIRMED_NOT_ME, MemberSafetyState.HELD_NO_ANSWER,
                  MemberSafetyState.CONTACT_UNREACHABLE, MemberSafetyState.HELD, MemberSafetyState.WARNED)
RESOLVABLE_STATES = frozenset({MemberSafetyState.HELD_NO_ANSWER, MemberSafetyState.CONTACT_UNREACHABLE,
                               MemberSafetyState.CONFIRMED_NOT_ME, MemberSafetyState.WARNED})
ACTIONABLE_STATES = RESOLVABLE_STATES  # staff see every episode they may resolve

ANSWER_EVENTS = {  # a REQUEST# status an answer wrote -> its event, timed at resolvedAt, on the channel in repliedVia
    RequestState.CONFIRMED_PENDING: OrgEvent.CONTACT_CONFIRMED_YES,
    RequestState.CONFIRMED: OrgEvent.CONTACT_CONFIRMED_YES,  # the release keeps resolvedAt: the same row as confirmed_pending
    RequestState.DENIED: OrgEvent.CONTACT_CONFIRMED_NOT_ME,
    RequestState.REVERSED_NO: OrgEvent.ANSWER_REVERSED,
}
RECORD_KINDS = (  # (PK prefix, SK prefix or whole SK, whether the SK is a prefix, kind)
    ("REQUEST#", graph.METADATA_SORT_KEY, False, ProjectedRecordKind.REQUEST_META),
    ("REQUEST#", graph.NOTE_SORT_KEY_PREFIX, True, ProjectedRecordKind.REQUEST_NOTE),
    ("EMAIL#", graph.METADATA_SORT_KEY, False, ProjectedRecordKind.EMAIL_META),
    ("CASE#", graph.METADATA_SORT_KEY, False, ProjectedRecordKind.CASE_META),
    ("ORG#", RESOLVE_SORT_KEY_PREFIX, True, ProjectedRecordKind.ORG_RESOLVE),
    ("ORG#", CANARY_SORT_KEY, False, ProjectedRecordKind.ORG_CANARY),
    ("RECEIVER#", ORG_PREFERENCE_SORT_KEY_PREFIX, True, ProjectedRecordKind.RECEIVER_ORG_PREFERENCE),
    ("RECEIVER#", "CONTACT#", True, ProjectedRecordKind.RECEIVER_CONTACT),
    ("USER#", "CONN#", True, ProjectedRecordKind.USER_CONNECTION),
)
stream_value_deserializer = TypeDeserializer()


@dataclass(frozen=True)
class ProjectedEvent:
    org_id: str
    member_ref: str  # the resident's receiver address
    event: OrgEvent
    source_key: str  # the episode: "REQUEST#req-<uuid>" or "CASE#case-<uuid>"
    at: str  # graph.iso_timestamp text
    channel: Channel | None  # how the message came in (message_held) or the answer came back; None for other events
    request_id: str | None  # set for a REQUEST# episode
    case_id: str | None  # set for a CASE# episode
    ttl_epoch_seconds: int


def passes_visibility(event: OrgEvent, visibility: OrgVisibility) -> bool:
    """ALERTS_AND_CASES lets every event through, ALERTS_ONLY all but CASE_EVENTS, NONE only ANY_VISIBILITY_EVENTS."""
    if visibility is OrgVisibility.ALERTS_AND_CASES:
        return True
    if visibility is OrgVisibility.ALERTS_ONLY:
        return event not in CASE_EVENTS
    return event in ANY_VISIBILITY_EVENTS


def episode_state(events: Iterable[ProjectedEvent]) -> MemberSafetyState | None:
    """One episode's events folded from no state, ordered by at, then by OrgEvent order; None when the episode is closed."""
    state = None
    for projected_event in sorted(events, key=lambda event: (event.at, EVENT_FOLD_ORDER[event.event])):
        transitions = EPISODE_TRANSITIONS[state]
        if projected_event.event in transitions:
            state = transitions[projected_event.event]
    return state


def image_of(record: dict, image_name: str) -> dict | None:
    """Keys, NewImage or OldImage of a stream record as plain values (numbers as Decimal); None when the record has none
    (OldImage on an INSERT, NewImage on a REMOVE)."""
    if image_name not in record["dynamodb"]:
        return None
    return {name: stream_value_deserializer.deserialize(value) for name, value in record["dynamodb"][image_name].items()}


def record_time_of(record: dict) -> datetime:
    """When DynamoDB wrote the record's change (ApproximateCreationDateTime, epoch seconds)."""
    return datetime.fromtimestamp(float(record["dynamodb"]["ApproximateCreationDateTime"]), timezone.utc)


def record_kind_of(keys: dict) -> ProjectedRecordKind | None:
    """The kind of row the record's keys name; None for a row the projection does not read."""
    for partition_prefix, sort_key_part, is_sort_key_prefix, kind in RECORD_KINDS:
        is_sort_key_match = keys["SK"].startswith(sort_key_part) if is_sort_key_prefix else keys["SK"] == sort_key_part
        if keys["PK"].startswith(partition_prefix) and is_sort_key_match:
            return kind
    return None


def events_for_record(record: dict) -> list[ProjectedEvent]:
    """The events one stream record stands for, in OrgEvent order; [] for a record that stands for none (a REMOVE, an
    unmatched row or status). Raises when the member has no complete RECEIVER#/PROFILE, a NOTE# has no REQUEST#/META, or
    a RESOLVE# row names a resident of another org."""
    event_name = StreamRecordEventName(record["eventName"])
    if event_name is StreamRecordEventName.REMOVE:
        return []
    kind, new_image = record_kind_of(image_of(record, "Keys")), image_of(record, "NewImage")
    if kind is ProjectedRecordKind.REQUEST_META:
        return request_events(new_image)
    if kind is ProjectedRecordKind.EMAIL_META:
        return unreachable_events(new_image, image_of(record, "OldImage"), record_time_of(record))
    if kind is ProjectedRecordKind.CASE_META:
        return case_events(new_image)
    if event_name is not StreamRecordEventName.INSERT:
        return []
    if kind is ProjectedRecordKind.REQUEST_NOTE:
        return recovery_pack_events(new_image)
    if kind is ProjectedRecordKind.ORG_RESOLVE:
        return resolve_events(new_image)
    return []


def request_event(member: ReceiverProfile, request_row: dict, event: OrgEvent, at: str, channel: Channel | None) -> ProjectedEvent:
    request_id = graph.request_id_of(request_row)
    return ProjectedEvent(member.org_id, member.receiver_address, event, f"{REQUEST_SOURCE_PREFIX}{request_id}", at, channel,
                          request_id, None, int(request_row["ttlEpochSeconds"]))


def request_events(new_image: dict) -> list[ProjectedEvent]:
    """pending: message_held at createdAt on inboundChannel, plus ask_capped (same time, no channel) once askCapped is
    set; an answer: its ANSWER_EVENTS event at resolvedAt on repliedVia; timeout: ask_timed_out at resolvedAt."""
    status = RequestState(new_image["status"])
    member = profiles.require_receiver_profile(new_image["receiverEmail"])
    if status is RequestState.PENDING:
        events = [request_event(member, new_image, OrgEvent.MESSAGE_HELD, new_image["createdAt"], Channel(new_image["inboundChannel"]))]
        if "askCapped" in new_image and new_image["askCapped"] is True:
            events.append(request_event(member, new_image, OrgEvent.ASK_CAPPED, new_image["createdAt"], None))
        return events
    if status is RequestState.TIMEOUT:
        return [request_event(member, new_image, OrgEvent.ASK_TIMED_OUT, new_image["resolvedAt"], None)]
    return [request_event(member, new_image, ANSWER_EVENTS[status], new_image["resolvedAt"], Channel(new_image["repliedVia"]))]


def is_unreachable_warning(email_image: dict | None) -> bool:
    """An EMAIL# row the Reader marked WARN unreachable for a request (pidge_reader keep_held_with_warning). A claimed row has
    no state yet; a PASS row carries warnReason and requestId as NULL."""
    return (email_image is not None and "state" in email_image and email_image["state"] == PolicyDecision.WARN
            and "warnReason" in email_image and email_image["warnReason"] == WarnReason.UNREACHABLE
            and "requestId" in email_image and email_image["requestId"] is not None)


def unreachable_events(new_image: dict, old_image: dict | None, record_time: datetime) -> list[ProjectedEvent]:
    """contact_unreachable on the request's episode when the row turns WARN unreachable (a later change to an already
    unreachable row, e.g. its quarantine, is not a second event), timed when the record was written: receivedAt precedes the
    request's createdAt and would fold before message_held."""
    if not is_unreachable_warning(new_image) or is_unreachable_warning(old_image):
        return []
    member = profiles.require_receiver_profile(new_image["receiverEmail"])
    request_id = new_image["requestId"]
    return [ProjectedEvent(member.org_id, member.receiver_address, OrgEvent.CONTACT_UNREACHABLE, f"{REQUEST_SOURCE_PREFIX}{request_id}",
                           graph.iso_timestamp(record_time), None, request_id, None, int(new_image["ttlEpochSeconds"]))]


def recovery_pack_events(note_row: dict) -> list[ProjectedEvent]:
    """recovery_pack_drafted at writtenAt for a new NOTE# that carries a pack; the member is the request's receiverEmail."""
    if "recoveryPack" not in note_row or note_row["recoveryPack"] is None:
        return []
    request_id = note_row["PK"].removeprefix(REQUEST_SOURCE_PREFIX)
    request_row = graph.get_request(request_id)
    if request_row is None:
        raise ValueError("a NOTE# row without its REQUEST#/META row")
    member = profiles.require_receiver_profile(request_row["receiverEmail"])
    return [ProjectedEvent(member.org_id, member.receiver_address, OrgEvent.RECOVERY_PACK_DRAFTED, note_row["PK"],
                           note_row["writtenAt"], None, request_id, None, int(note_row["ttlEpochSeconds"]))]


def case_events(new_image: dict) -> list[ProjectedEvent]:
    """open: wrong_payment_reported at createdAt; closed: case_closed at closedAt; reported: none."""
    status = CaseState(new_image["status"])
    if status is CaseState.REPORTED:
        return []
    event, at = ((OrgEvent.WRONG_PAYMENT_REPORTED, new_image["createdAt"]) if status is CaseState.OPEN
                 else (OrgEvent.CASE_CLOSED, new_image["closedAt"]))
    member = profiles.require_receiver_profile(new_image["receiverEmail"])
    return [ProjectedEvent(member.org_id, member.receiver_address, event, new_image["PK"], at, None, None,
                           graph.case_id_of(new_image), int(new_image["ttlEpochSeconds"]))]


def resolve_sort_key_for(source_key: str, at: str) -> str:
    """RESOLVE#<sourceKey>#<at>: one row per resolve, so an episode reopened after a resolve can be resolved again."""
    return f"{RESOLVE_SORT_KEY_PREFIX}{source_key}#{at}"


def resolve_row_expiry(at: str) -> int:
    """A RESOLVE# row's ttlEpochSeconds: its at plus RESOLVE_LOG_RETENTION."""
    return int((datetime.fromisoformat(at.replace("Z", "+00:00")) + RESOLVE_LOG_RETENTION).timestamp())


def resolve_events(resolve_row: dict) -> list[ProjectedEvent]:
    """org_resolved at the row's at on the episode in its sourceKey attribute, with the row's ttl. Raises ValueError when the
    member's resident belongs to another org or the sourceKey names no REQUEST#/CASE# episode."""
    org_id = resolve_row["PK"].removeprefix("ORG#")
    member = profiles.require_receiver_profile(resolve_row["memberRef"])
    if member.org_id != org_id:
        raise ValueError("a RESOLVE# row names a resident of another org")
    source_key = resolve_row["sourceKey"]
    if source_key.startswith(REQUEST_SOURCE_PREFIX):
        request_id, case_id = source_key.removeprefix(REQUEST_SOURCE_PREFIX), None
    elif source_key.startswith(CASE_SOURCE_PREFIX):
        request_id, case_id = None, source_key.removeprefix(CASE_SOURCE_PREFIX)
    else:
        raise ValueError("a RESOLVE# row names no episode")
    return [ProjectedEvent(org_id, member.receiver_address, OrgEvent.ORG_RESOLVED, source_key, resolve_row["at"], None, request_id,
                           case_id, int(resolve_row["ttlEpochSeconds"]))]
