"""A contact's numbers and emails, read from one place: the CONTACT#.contactReachPoints list, or the single-point fields
of a row written before it. Which of them Pidge may ask, and so whose answer counts."""

import logging
from dataclasses import dataclass

from shared.enums import Channel, VerifiedVia

logger = logging.getLogger(__name__)


def verified_via_of(contact_row: dict) -> VerifiedVia:
    """A row without the field is a seeded one (scripts/seed_graph.py wrote it before the field existed); it is logged, never
    silent. An unknown value raises."""
    if "verifiedVia" not in contact_row:
        logger.warning("contact row %s has no verifiedVia; treating as seed (re-run scripts/seed_graph.py)", contact_row["SK"])
        return VerifiedVia.SEED
    return VerifiedVia(contact_row["verifiedVia"])


@dataclass(frozen=True)
class ReachPoint:
    """One number or email a contact is reached on. A WhatsApp or RCS number carries how it was checked and is asked only
    once checked; an email is never checked (verified_via None) and is asked as it is, since a Yes by email never counts."""

    channel: Channel
    address: str  # E.164 with "+" for WhatsApp and RCS (as each reports the sender); the email exactly as stored
    verified_via: VerifiedVia | None

    @property
    def is_askable(self) -> bool:
        return self.channel is Channel.EMAIL or self.verified_via is not VerifiedVia.PENDING


REACH_POINT_CHANNELS = (Channel.WHATSAPP, Channel.RCS, Channel.EMAIL)
# A row written before CONTACT#.contactReachPoints holds at most one point per channel in these fields.
SINGLE_POINT_FIELDS = ((Channel.WHATSAPP, "contactWhatsApp"), (Channel.RCS, "contactRcs"), (Channel.EMAIL, "contactEmail"))
# Removed from a row the first time it is written as a contactReachPoints list (the list is then the one source). An older
# row's knownAddresses stay: they are addresses it may write from, never asked, and not shown on the edit form.
SINGLE_POINT_ROW_FIELDS = tuple(field_name for _, field_name in SINGLE_POINT_FIELDS)


def reach_points_of(contact_row: dict) -> tuple[ReachPoint, ...]:
    """Every number and email on a CONTACT# row; the one place that reads them. The contactReachPoints list when present,
    else the single-point fields of a seeded or slice-10c row, whose numbers share the row's verifiedVia. Raises on a
    malformed point."""
    if "contactReachPoints" in contact_row:
        return tuple(reach_point_from_item(item) for item in contact_row["contactReachPoints"])
    row_verified_via = verified_via_of(contact_row)
    return tuple(
        ReachPoint(channel, str(contact_row[field_name]), None if channel is Channel.EMAIL else row_verified_via)
        for channel, field_name in SINGLE_POINT_FIELDS if contact_row.get(field_name)
    )


def reach_point_from_item(item: dict) -> ReachPoint:
    channel = Channel(item["channel"])
    if channel not in REACH_POINT_CHANNELS:
        raise ValueError(f"a reach point cannot be on {channel}")  # the app is reached through the PWA token, not a point
    if channel is Channel.EMAIL:
        return ReachPoint(channel, str(item["address"]), None)
    if "verifiedVia" not in item:
        raise ValueError(f"a {channel} point has no verifiedVia")
    return ReachPoint(channel, str(item["address"]), VerifiedVia(item["verifiedVia"]))


def askable_addresses(reach_points: tuple[ReachPoint, ...], channel: Channel) -> tuple[str, ...]:
    """The addresses on one channel Pidge may ask, and so the only ones whose answer counts."""
    return tuple(point.address for point in reach_points if point.channel is channel and point.is_askable)


def item_of(point: ReachPoint) -> dict:
    """The contactReachPoints entry for one point; an email carries no verifiedVia."""
    item = {"channel": str(point.channel), "address": point.address}
    if point.channel is not Channel.EMAIL:
        item["verifiedVia"] = str(point.verified_via)
    return item


def known_sender_addresses_of(contact_row: dict) -> tuple[str, ...]:
    """The addresses an email in this contact's name may come from (gate 1), lower-cased: the knownAddresses field of an
    older row (kept when it is first edited), then every email point of a contactReachPoints row."""
    known_addresses = [str(address) for address in contact_row.get("knownAddresses", [])]
    if "contactReachPoints" in contact_row:
        known_addresses += [point.address for point in reach_points_of(contact_row) if point.channel is Channel.EMAIL]
    return tuple(dict.fromkeys(address.casefold() for address in known_addresses))


def pending_whatsapp_index(contact_row: dict, number: str | None) -> int | None:
    """Where that WhatsApp number waits for its code on the row (number None: the first one waiting); None when it does not."""
    return next((index for index, point in enumerate(reach_points_of(contact_row))
                 if point.channel is Channel.WHATSAPP and point.verified_via is VerifiedVia.PENDING
                 and number in (None, point.address)), None)


def stored_items_by_point(contact_row: dict) -> dict[tuple[Channel, str], dict]:
    """The row's points keyed by (channel, address), each as its stored contactReachPoints entry (an older row's derived)."""
    points = reach_points_of(contact_row)
    items = contact_row["contactReachPoints"] if "contactReachPoints" in contact_row else [item_of(point) for point in points]
    return {(point.channel, point.address): item for point, item in zip(points, items)}


def reach_channel_of(channel: Channel) -> Channel:
    """The channel whose points stand for this one: an SMS number is the contact's RCS point (one E.164 serves both), every
    other phone channel and email is itself. INAPP has no point (the app is reached through the PWA token): ValueError."""
    if channel is Channel.SMS:
        return Channel.RCS
    if channel is Channel.INAPP:
        raise ValueError("the app has no reach point")
    return channel


def has_checked_whatsapp_number(points: tuple[ReachPoint, ...]) -> bool:
    return any(point.channel is Channel.WHATSAPP and point.is_askable for point in points)
