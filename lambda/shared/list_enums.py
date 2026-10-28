"""Closed sets for the scam-list feeds: what a check covered, how one feed sync ended, and which parser reads a live feed."""

from enum import StrEnum


class ScamListCoverageState(StrEnum):
    """Whether a check() answer covers every live-fetch feed it relies on. UNAVAILABLE is never read as clean."""

    COMPLETE = "complete"
    UNAVAILABLE = "unavailable"


class FeedFetchOutcome(StrEnum):
    """How one live feed's sync ended; only FETCHED and NOT_MODIFIED stamp the source synced."""

    FETCHED = "fetched"
    NOT_MODIFIED = "not_modified"        # a 304 to the validators the last sync stored
    TOO_LARGE = "too_large"              # over the size cap, by Content-Length or by the bytes read
    HTTP_ERROR = "http_error"            # a non-https URL or redirect, a network error or an error status
    PARSE_ERROR = "parse_error"          # the body does not decode or lacks a column the parser reads
    REFUSED_LICENCE = "refused_licence"  # the licence is not one a sync may copy; nothing was fetched
    REFUSED_DRY_RUN = "refused_dry_run"  # no rows, or fewer than half of the last generation's keys


class FeedParser(StrEnum):
    """The code that reads one live feed's bytes; a SOURCE# row without one is a hand-loaded snapshot."""

    KISA_PHISHING_CSV = "kisa_phishing_csv"
    AMF_BLACKLIST_CSV = "amf_blacklist_csv"
