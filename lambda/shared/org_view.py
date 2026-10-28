"""What an org admin sees of their own org, the safety overview: each member named from the resident's own
RECEIVER#<address>/PROFILE row and read as one MemberSafetyState, the open episodes staff can act on, the coverage counts, who
has not opened the app for a week, and how fresh the projection is. Built only for the OrgAdmin orgs.require_org_admin resolved,
never for a body org id. Reads ORG#, ORGLOG#, ORGSTAT# and RECEIVER#/PROFILE rows only, never REQUEST#/EMAIL#/CASE#: what it
reads past the profiles was written by pidge_org_projection, which holds the only visibility filter."""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from boto3.dynamodb.conditions import Attr, Key

from shared import graph, orgs, profiles
from shared.enums import Channel
from shared.metrics import emit_count_metric
from shared.org_enums import MemberSafetyState, OrgEvent, OrgVisibility
from shared.org_events import (
    ACTIONABLE_STATES, ORG_LOG_PARTITION_PREFIX, ORG_STATS_DAY_SORT_KEY_PREFIX, ORG_STATS_PARTITION_PREFIX, STATE_PRIORITY,
    ProjectedEvent, episode_state,
)
from shared.orgs import OrgAdmin
from shared.profiles import ReceiverProfile, UnknownReceiverError
from shared.sources import query_all_pages

logger = logging.getLogger(__name__)
ORG_MEMBER_REJECTED_METRIC_NAME = "OrgMemberRejected"
STALE_AFTER = timedelta(minutes=15)  # the canary stamps every 5 minutes: three missed stamps turn the dashboard amber
SEEN_WINDOW_DAYS = 7
NEWEST_STATS_DAYS_READ = 2  # just after midnight the newest DAY# row may hold event counts but no canary stamp yet
SHARING_VISIBILITIES = frozenset({OrgVisibility.ALERTS_ONLY, OrgVisibility.ALERTS_AND_CASES})


@dataclass(frozen=True)
class OpenEpisode:
    source_key: str
    state: MemberSafetyState
    at: str  # the time of the episode's newest event


@dataclass(frozen=True)
class MemberOverview:
    member_ref: str
    resident_name: str
    tags: list[str]
    state: MemberSafetyState
    open_episodes: tuple[OpenEpisode, ...]  # empty when the member does not share
    shares: OrgVisibility  # the MEMBER# coverage fact; NONE when the projection never wrote one
    last_event_at: str | None  # the newest ORGLOG# at of a sharing member; None when they share nothing or have no event
    is_sharing: bool
    has_verified_contact: bool
    is_seen_recently: bool


def org_log_partition(org_id: str) -> str:
    return f"{ORG_LOG_PARTITION_PREFIX}{org_id}"


def org_dashboard_for(org_admin: OrgAdmin, now: datetime) -> dict | None:
    """The admin's org overview at this moment; None (logged) when the org has no PROFILE row. A member without a resident
    profile, or whose resident belongs to another org, is left out and logged."""
    org_profile = orgs.read_org_profile(org_admin.org_id)
    if org_profile is None:
        logger.warning("org %s has no PROFILE row; admin %s gets no dashboard", org_admin.org_id, org_admin.user_id)
        return None
    events_by_member = read_events_by_member(org_admin.org_id)
    overviews = []
    for member_row in orgs.list_member_rows(org_admin.org_id):
        overview = member_overview_for(org_admin.org_id, member_row, events_by_member, now.date())
        if overview is not None:
            overviews.append(overview)
    last_projected_at = read_last_projected_at(org_admin.org_id)
    return {
        "orgId": org_profile.org_id, "orgName": org_profile.org_name, "lastProjectedAt": last_projected_at,
        "isStale": is_projection_stale(last_projected_at, now),
        "members": [{"memberRef": overview.member_ref, "residentName": overview.resident_name, "tags": overview.tags,
                     "state": str(overview.state), "shares": str(overview.shares), "lastEventAt": overview.last_event_at}
                    for overview in overviews],
        "actionable": [{"memberRef": overview.member_ref, "residentName": overview.resident_name, "sourceKey": episode.source_key,
                        "state": str(episode.state), "at": episode.at}
                       for overview in overviews for episode in overview.open_episodes if episode.state in ACTIONABLE_STATES],
        "coverage": coverage_counts(overviews),
        "callList": [{"memberRef": overview.member_ref, "residentName": overview.resident_name}
                     for overview in overviews if not overview.is_seen_recently],
    }


def resident_of_member(org_id: str, member_ref: str) -> ReceiverProfile | None:
    """The member's resident, or None (logged; never the address) when there is no profile (counted UnknownReceiver by
    require_receiver_profile) or the resident names another org (counted OrgMemberRejected)."""
    try:
        resident = profiles.require_receiver_profile(member_ref)
    except UnknownReceiverError:
        logger.warning("a member of org %s has no resident profile; left off the dashboard", org_id)
        return None
    if resident.org_id != org_id:
        logger.warning("member %s is listed in org %s but enrolled in another org; left off the dashboard",
                       resident.receiver_user_id, org_id)
        emit_count_metric(ORG_MEMBER_REJECTED_METRIC_NAME)
        return None
    return resident


def member_overview_for(org_id: str, member_row: dict, events_by_member: dict[str, list[ProjectedEvent]],
                        today: date) -> MemberOverview | None:
    """One member read from the MEMBER# coverage fact and their ORGLOG# events. A member who has chosen no visibility, or
    none, is NOT_SHARED whatever the log holds, and no episode or last event of theirs is listed. Tags are absent on rows the
    seed did not write; hasVerifiedContact and lastSeenDay are absent until the projection writes them."""
    member_ref = member_row["SK"].removeprefix(orgs.MEMBER_SORT_KEY_PREFIX)
    resident = resident_of_member(org_id, member_ref)
    if resident is None:
        return None
    shares = OrgVisibility(member_row["coverageVisibility"]) if "coverageVisibility" in member_row else OrgVisibility.NONE
    is_sharing = shares in SHARING_VISIBILITIES
    member_events = events_by_member[member_ref] if is_sharing and member_ref in events_by_member else []
    open_episodes = open_episodes_of(member_events) if member_events else ()
    return MemberOverview(
        member_ref=member_ref, resident_name=resident.resident_name,
        tags=list(member_row["tags"]) if "tags" in member_row else [],
        state=member_state_of(is_sharing, open_episodes), open_episodes=open_episodes, shares=shares,
        last_event_at=max(member_event.at for member_event in member_events) if member_events else None, is_sharing=is_sharing,
        has_verified_contact="hasVerifiedContact" in member_row and member_row["hasVerifiedContact"] is True,
        is_seen_recently=("lastSeenDay" in member_row
                          and date.fromisoformat(member_row["lastSeenDay"]) > today - timedelta(days=SEEN_WINDOW_DAYS)),
    )


def member_state_of(is_sharing: bool, open_episodes: tuple[OpenEpisode, ...]) -> MemberSafetyState:
    """NOT_SHARED when the member does not share; else the first open state in STATE_PRIORITY; else NOTHING_OPEN."""
    if not is_sharing:
        return MemberSafetyState.NOT_SHARED
    open_states = {episode.state for episode in open_episodes}
    return next((state for state in STATE_PRIORITY if state in open_states), MemberSafetyState.NOTHING_OPEN)


def open_episodes_of(member_events: list[ProjectedEvent]) -> tuple[OpenEpisode, ...]:
    """The member's episodes (events grouped by sourceKey) that episode_state leaves open, in sourceKey order."""
    events_by_source: dict[str, list[ProjectedEvent]] = {}
    for projected_event in member_events:
        events_by_source.setdefault(projected_event.source_key, []).append(projected_event)
    open_episodes = []
    for source_key in sorted(events_by_source):
        state = episode_state(events_by_source[source_key])
        if state is not None:
            open_episodes.append(OpenEpisode(source_key, state, max(event.at for event in events_by_source[source_key])))
    return tuple(open_episodes)


def projected_event_from_log_row(org_id: str, log_row: dict) -> ProjectedEvent:
    """An ORGLOG# row read back as the event the projection wrote. channel, requestId and caseId are absent on events
    that have none."""
    return ProjectedEvent(
        org_id=org_id, member_ref=log_row["memberRef"], event=OrgEvent(log_row["event"]), source_key=log_row["sourceKey"],
        at=log_row["at"], channel=Channel(log_row["channel"]) if "channel" in log_row else None,
        request_id=log_row["requestId"] if "requestId" in log_row else None,
        case_id=log_row["caseId"] if "caseId" in log_row else None, ttl_epoch_seconds=int(log_row["ttlEpochSeconds"]),
    )


def read_events_by_member(org_id: str) -> dict[str, list[ProjectedEvent]]:
    """Every ORGLOG#<orgId> row, every page read, as events grouped by memberRef."""
    events_by_member: dict[str, list[ProjectedEvent]] = {}
    for log_row in query_all_pages(KeyConditionExpression=Key("PK").eq(org_log_partition(org_id))):
        events_by_member.setdefault(log_row["memberRef"], []).append(projected_event_from_log_row(org_id, log_row))
    return events_by_member


def episode_state_for(org_id: str, member_ref: str, source_key: str) -> MemberSafetyState | None:
    """The state of one member's episode, folded from its ORGLOG#<orgId>/<sourceKey>#... rows that name this member; None
    when there are none or the episode is closed."""
    log_rows = query_all_pages(
        KeyConditionExpression=Key("PK").eq(org_log_partition(org_id)) & Key("SK").begins_with(f"{source_key}#"),
        FilterExpression=Attr("memberRef").eq(member_ref))
    if not log_rows:
        return None
    return episode_state([projected_event_from_log_row(org_id, log_row) for log_row in log_rows])


def read_last_projected_at(org_id: str) -> str | None:
    """The newest canary stamp (lastProjectedAt) on the org's two newest ORGSTAT# DAY# rows; None when neither has one."""
    response = graph.get_table().query(
        KeyConditionExpression=(Key("PK").eq(f"{ORG_STATS_PARTITION_PREFIX}{org_id}")
                                & Key("SK").begins_with(ORG_STATS_DAY_SORT_KEY_PREFIX)),
        ScanIndexForward=False, Limit=NEWEST_STATS_DAYS_READ)
    return max((stats_row["lastProjectedAt"] for stats_row in response["Items"] if "lastProjectedAt" in stats_row), default=None)


def is_projection_stale(last_projected_at: str | None, now: datetime) -> bool:
    """Stale when no canary stamp was ever projected or the newest is older than STALE_AFTER."""
    return last_projected_at is None or now - datetime.fromisoformat(last_projected_at) > STALE_AFTER


def coverage_counts(overviews: list[MemberOverview]) -> dict:
    """visibilityChosen counts members who share (alerts_only or alerts_and_cases): a resident who chose none counts as
    one who chose nothing, as their state reads NOT_SHARED either way."""
    return {"enrolled": len(overviews),
            "withVerifiedContact": sum(1 for overview in overviews if overview.has_verified_contact),
            "visibilityChosen": sum(1 for overview in overviews if overview.is_sharing),
            "seenInLastSevenDays": sum(1 for overview in overviews if overview.is_seen_recently)}
