"""Feed sync: a live SOURCE# feed's rows become LIST# rows with a generation and an expiry, diffed by 256 SHA-256 buckets.

A live-fetch source (one with a feedParser) is downloaded by the injected fetcher from its seed or admin mapping.url,
parsed by feed_parsers and refused before any write when it is empty or shrank below half of the last generation. A
snapshot source (no feedParser) syncs only from infra/seed/lists.public.json with LIST_FEEDS_MOCK=true, on a laptop or in
tests. A bucket is rewritten only when its keys changed or its rows are half-way to expiry, so an unchanged feed writes
nothing but its BUCKETS row. The source is stamped synced last, after every write landed; a failure stamps nothing.
"""

import hashlib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from shared import feed_parsers, graph, sources
from shared.enums import IndicatorKind
from shared.list_enums import FeedFetchOutcome
from shared.lists import add_seconds_to_timestamp, is_feed_stale, list_partition, parse_timestamp, public_list_row_from_seed
from shared.metrics import emit_count_metric
from shared.sources import LicenceRefused, SourceRow

logger = logging.getLogger(__name__)

PUBLIC_SNAPSHOT_FILE = Path(__file__).resolve().parents[2] / "infra" / "seed" / "lists.public.json"
BUCKETS_SORT_KEY = "BUCKETS"
LIST_SYNC_EMPTY_METRIC_NAME = "ListSyncEmpty"
FEED_STALE_METRIC_NAME = "FeedStale"
FEED_SYNC_FAILED_METRIC_NAME = "FeedSyncFailed"
BUCKET_KEY_SEPARATOR = "|"
SECONDS_PER_DAY = 86400


class SnapshotMockRequired(Exception):
    """A snapshot source syncs only from the seed file, with LIST_FEEDS_MOCK=true; the Lambda asset ships no infra/seed."""


class FeedEmpty(ValueError):
    """A feed with no rows is refused: an empty sync would delete every row and still stamp the source."""


@dataclass(frozen=True)
class SyncReport:
    source_id: str
    generation: int
    written_count: int
    deleted_count: int


@dataclass(frozen=True)
class FeedValidators:
    """The ETag and Last-Modified a fetched feed answered with, stored on BUCKETS and sent back on the next fetch."""

    etag: str | None
    last_modified: str | None


@dataclass(frozen=True)
class FeedFetchResult:
    outcome: FeedFetchOutcome
    body: bytes | None                   # FETCHED only
    validators: FeedValidators | None    # FETCHED only


FeedFetcher = Callable[[str, FeedValidators | None], FeedFetchResult]


@dataclass(frozen=True)
class FeedSyncReport:
    source_id: str
    outcome: FeedFetchOutcome
    generation: int | None  # None when the sync failed
    written_count: int
    deleted_count: int
    skipped_count: int


def require_feed_mock() -> None:
    if os.environ.get("LIST_FEEDS_MOCK") != "true":
        raise SnapshotMockRequired("snapshot sources sync only from infra/seed/lists.public.json; set LIST_FEEDS_MOCK=true")


def bucket_entry_of(kind: IndicatorKind, key: str) -> str:
    return f"{kind}{BUCKET_KEY_SEPARATOR}{key}"


def bucket_of(kind: IndicatorKind, key: str) -> str:
    """One of 256 buckets: the first two hex digits of sha256('<kind>|<key>')."""
    return hashlib.sha256(bucket_entry_of(kind, key).encode()).hexdigest()[:2]


def digest_of(bucket_entries: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(bucket_entries)).encode()).hexdigest()


def read_snapshot_rows(source_id: str) -> list[dict]:
    """The snapshot's rows for one source, as the mocked feed."""
    snapshot_rows = json.loads(PUBLIC_SNAPSHOT_FILE.read_text(), parse_float=Decimal)
    return [row for row in snapshot_rows if row["sourceId"] == source_id]


def read_buckets_row(source_id: str) -> dict | None:
    return graph.get_table().get_item(Key={"PK": sources.source_partition(source_id), "SK": BUCKETS_SORT_KEY}).get("Item")


def build_rows_by_bucket(source: SourceRow, seed_rows: list[dict], generation: int, now: str) -> dict[str, dict[str, dict]]:
    """bucket → {'<kind>|<key>': stored row}; a seed row from another source is refused."""
    expires_at = add_seconds_to_timestamp(now, source.ttl_days * SECONDS_PER_DAY)
    rows_by_bucket: dict[str, dict[str, dict]] = {}
    for seed in seed_rows:
        row = public_list_row_from_seed(seed, generation, expires_at)
        if row["sourceId"] != source.source_id:
            raise ValueError(f"a {row['sourceId']} row in the {source.source_id} feed")
        kind = IndicatorKind(seed["kind"])
        rows_by_bucket.setdefault(bucket_of(kind, row["SK"]), {})[bucket_entry_of(kind, row["SK"])] = row
    return rows_by_bucket


def is_bucket_young(bucket: dict, source: SourceRow, now: str) -> bool:
    """Written less than ttlDays / 2 ago, so its rows never reach their expiry while the feed still lists them."""
    return parse_timestamp(now) - parse_timestamp(bucket["writtenAt"]) < timedelta(days=source.ttl_days / 2)


def is_bucket_current(old_bucket: dict | None, new_digest: str, source: SourceRow, now: str) -> bool:
    """Same keys, and young."""
    if old_bucket is None or old_bucket["digest"] != new_digest:
        return False
    return is_bucket_young(old_bucket, source, now)


def list_key_of(bucket_entry: str) -> dict:
    kind, _, key = bucket_entry.partition(BUCKET_KEY_SEPARATOR)
    return {"PK": list_partition(IndicatorKind(kind)), "SK": key}


def write_changed_buckets(source: SourceRow, rows_by_bucket: dict, old_buckets: dict, now: str) -> tuple[dict, int, int]:
    """Puts the rows of every changed bucket and deletes its keys that left; returns the new bucket map and the counts."""
    new_buckets, written_count, deleted_count = {}, 0, 0
    with graph.get_table().batch_writer(overwrite_by_pkeys=["PK", "SK"]) as batch:
        for bucket in sorted(set(rows_by_bucket) | set(old_buckets)):
            rows_by_entry = rows_by_bucket[bucket] if bucket in rows_by_bucket else {}
            old_bucket = old_buckets[bucket] if bucket in old_buckets else None
            new_digest = digest_of(list(rows_by_entry))
            if is_bucket_current(old_bucket, new_digest, source, now):
                new_buckets[bucket] = old_bucket
                continue
            for row in rows_by_entry.values():
                batch.put_item(Item=row)
            leaving_entries = [entry for entry in (old_bucket["keys"] if old_bucket else []) if entry not in rows_by_entry]
            for entry in leaving_entries:
                batch.delete_item(Key=list_key_of(entry))
            written_count, deleted_count = written_count + len(rows_by_entry), deleted_count + len(leaving_entries)
            if rows_by_entry:
                new_buckets[bucket] = {"digest": new_digest, "keys": sorted(rows_by_entry), "writtenAt": now}
    return new_buckets, written_count, deleted_count


def sync_source(source: SourceRow, seed_rows: list[dict], now: str, validators: FeedValidators | None = None) -> SyncReport:
    """One feed → its LIST# rows, then the BUCKETS row (with the fetch's validators), then the source's sync stamp, in that
    order; a failure stamps nothing."""
    if not seed_rows:
        raise FeedEmpty(f"{source.source_id} has no rows")
    buckets_row = read_buckets_row(source.source_id)
    generation = 1 if buckets_row is None else int(buckets_row["generation"]) + 1
    old_buckets = dict(buckets_row["buckets"]) if buckets_row is not None else {}
    rows_by_bucket = build_rows_by_bucket(source, seed_rows, generation, now)
    new_buckets, written_count, deleted_count = write_changed_buckets(source, rows_by_bucket, old_buckets, now)
    graph.get_table().put_item(Item={
        "PK": sources.source_partition(source.source_id), "SK": BUCKETS_SORT_KEY, "generation": generation, "buckets": new_buckets,
        "etag": None if validators is None else validators.etag,
        "lastModified": None if validators is None else validators.last_modified,
    })
    sources.record_sync(source, generation, now)
    return SyncReport(source.source_id, generation, written_count, deleted_count)


def is_synced_within_interval(source: SourceRow, now: str) -> bool:
    if source.last_sync_at is None:
        return False
    return parse_timestamp(now) - parse_timestamp(source.last_sync_at) < timedelta(minutes=source.sync_interval_minutes)


def sync_snapshot_sources(now: str) -> list[SyncReport]:
    """Every live snapshot source (no feedParser) with indicator kinds, from the seed file. An empty feed is logged, counted
    and left unstamped (its rows are bounded by their expiresAt)."""
    require_feed_mock()
    reports = []
    for source in sources.list_newest_live_sources():
        if source.feed_parser is not None or not source.indicator_kinds:
            continue
        try:
            reports.append(sync_source(source, read_snapshot_rows(source.source_id), now))
        except FeedEmpty as error:
            logger.error("feed sync refused: %s", error)
            emit_count_metric(LIST_SYNC_EMPTY_METRIC_NAME, "source", source.source_id)
    return reports


def conditional_validators_of(buckets_row: dict | None, source: SourceRow, now: str) -> FeedValidators | None:
    """The stored validators, only while every bucket is young: a 304 must never stamp a sync over rows about to expire."""
    if buckets_row is None or not buckets_row["buckets"]:
        return None
    if not all(is_bucket_young(bucket, source, now) for bucket in buckets_row["buckets"].values()):
        return None
    etag, last_modified = buckets_row.get("etag"), buckets_row.get("lastModified")  # absent on a pre-SX3 BUCKETS row
    if etag is None and last_modified is None:
        return None
    return FeedValidators(etag, last_modified)


def previous_key_count(buckets_row: dict | None) -> int:
    if buckets_row is None:
        return 0
    return sum(len(bucket["keys"]) for bucket in buckets_row["buckets"].values())


def report_failed_sync(source: SourceRow, outcome: FeedFetchOutcome, byte_count: int, skipped_count: int = 0) -> FeedSyncReport:
    """Logs the source id, outcome and byte count only (never the URL, body or a row) and counts FeedSyncFailed."""
    logger.error("feed sync %s: %s, %d bytes", source.source_id, outcome, byte_count)
    emit_count_metric(FEED_SYNC_FAILED_METRIC_NAME, "outcome", str(outcome))
    return FeedSyncReport(source.source_id, outcome, None, 0, 0, skipped_count)


def sync_fetched_body(source: SourceRow, fetch_result: FeedFetchResult, buckets_row: dict | None, now: str) -> FeedSyncReport:
    """Parse, the dry-run guard (no rows, or fewer than half of the last generation's keys), then the bucket sync."""
    byte_count = len(fetch_result.body)
    try:
        parsed_feed = feed_parsers.parse_feed(source, fetch_result.body, now)
    except feed_parsers.FeedParseError:
        return report_failed_sync(source, FeedFetchOutcome.PARSE_ERROR, byte_count)
    if not parsed_feed.rows or 2 * len(parsed_feed.rows) < previous_key_count(buckets_row):
        return report_failed_sync(source, FeedFetchOutcome.REFUSED_DRY_RUN, byte_count, parsed_feed.skipped_count)
    report = sync_source(source, parsed_feed.rows, now, fetch_result.validators)
    logger.info("feed sync %s: %s, %d bytes", source.source_id, FeedFetchOutcome.FETCHED, byte_count)
    return FeedSyncReport(source.source_id, FeedFetchOutcome.FETCHED, report.generation, report.written_count,
                          report.deleted_count, parsed_feed.skipped_count)


def sync_fetched_source(source: SourceRow, now: str, fetch_feed: FeedFetcher) -> FeedSyncReport:
    """One live-fetch source: the licence gate (nothing fetched), the fetch with validators while the rows are young, then
    a 304 restamps the same generation with no BUCKETS write and a body is parsed and synced. A DynamoDB error raises."""
    try:
        sources.require_syncable_licence(source)
    except LicenceRefused:
        return report_failed_sync(source, FeedFetchOutcome.REFUSED_LICENCE, 0)
    buckets_row = read_buckets_row(source.source_id)
    validators = conditional_validators_of(buckets_row, source, now)
    fetch_result = fetch_feed(str(source.mapping["url"]), validators)
    if fetch_result.outcome is FeedFetchOutcome.NOT_MODIFIED and validators is not None:
        generation = int(buckets_row["generation"])
        sources.record_sync(source, generation, now)
        logger.info("feed sync %s: %s, 0 bytes", source.source_id, FeedFetchOutcome.NOT_MODIFIED)
        return FeedSyncReport(source.source_id, FeedFetchOutcome.NOT_MODIFIED, generation, 0, 0, 0)
    if fetch_result.outcome is FeedFetchOutcome.NOT_MODIFIED:
        return report_failed_sync(source, FeedFetchOutcome.HTTP_ERROR, 0)  # a 304 to a request that sent no validators
    if fetch_result.outcome is not FeedFetchOutcome.FETCHED:
        return report_failed_sync(source, fetch_result.outcome, 0)
    return sync_fetched_body(source, fetch_result, buckets_row, now)


def is_stale_after_sync(source: SourceRow, report: FeedSyncReport, now: str) -> bool:
    is_stamped = report.outcome in (FeedFetchOutcome.FETCHED, FeedFetchOutcome.NOT_MODIFIED)
    return is_feed_stale(now if is_stamped else source.last_sync_at, source.sync_interval_minutes, now)


def sync_fetched_sources(now: str, only_due: bool, fetch_feed: FeedFetcher) -> list[FeedSyncReport]:
    """Every live-fetch source (only_due: skip one synced less than an interval ago). One source's failure never stops the
    next; a source still stale after its sync is counted as FeedStale, once per run."""
    reports = []
    for source in sources.list_newest_live_sources():
        if source.feed_parser is None or (only_due and is_synced_within_interval(source, now)):
            continue
        report = sync_fetched_source(source, now, fetch_feed)
        reports.append(report)
        if is_stale_after_sync(source, report, now):
            emit_count_metric(FEED_STALE_METRIC_NAME)
    return reports
