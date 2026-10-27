"""Feed sync: a live SOURCE# feed's rows become LIST# rows with a generation and an expiry, diffed by 256 SHA-256 buckets.

LIST_FEEDS_MOCK=true reads the feed from the peer's mapped snapshot infra/seed/lists.public.json by sourceId; a live
download is not built and raises. A bucket is rewritten only when its keys changed or its rows are half-way to expiry,
so an unchanged feed writes nothing but its BUCKETS row. The source is stamped synced last, after every write landed.
"""

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from shared import graph, sources
from shared.enums import IndicatorKind
from shared.lists import add_seconds_to_timestamp, list_partition, parse_timestamp, public_list_row_from_seed
from shared.metrics import emit_count_metric
from shared.sources import SourceRow

logger = logging.getLogger(__name__)

PUBLIC_SNAPSHOT_FILE = Path(__file__).resolve().parents[2] / "infra" / "seed" / "lists.public.json"
BUCKETS_SORT_KEY = "BUCKETS"
LIST_SYNC_EMPTY_METRIC_NAME = "ListSyncEmpty"
BUCKET_KEY_SEPARATOR = "|"
SECONDS_PER_DAY = 86400


class LiveFeedFetchNotBuilt(Exception):
    """Only the mocked feed (LIST_FEEDS_MOCK=true) exists; downloading a live feed is not built."""


class FeedEmpty(ValueError):
    """A feed with no rows is refused: an empty sync would delete every row and still stamp the source."""


@dataclass(frozen=True)
class SyncReport:
    source_id: str
    generation: int
    written_count: int
    deleted_count: int


def require_feed_mock() -> None:
    if os.environ.get("LIST_FEEDS_MOCK") != "true":
        raise LiveFeedFetchNotBuilt("live feed download is not built; set LIST_FEEDS_MOCK=true to read the snapshot")


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


def is_bucket_current(old_bucket: dict | None, new_digest: str, source: SourceRow, now: str) -> bool:
    """Same keys, and written less than ttlDays / 2 ago (so its rows never reach their expiry while the feed still lists them)."""
    if old_bucket is None or old_bucket["digest"] != new_digest:
        return False
    return parse_timestamp(now) - parse_timestamp(old_bucket["writtenAt"]) < timedelta(days=source.ttl_days / 2)


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


def sync_source(source: SourceRow, seed_rows: list[dict], now: str) -> SyncReport:
    """One feed → its LIST# rows, then the BUCKETS row, then the source's sync stamp, in that order; a failure stamps nothing."""
    if not seed_rows:
        raise FeedEmpty(f"{source.source_id} has no rows")
    buckets_row = read_buckets_row(source.source_id)
    generation = 1 if buckets_row is None else int(buckets_row["generation"]) + 1
    old_buckets = dict(buckets_row["buckets"]) if buckets_row is not None else {}
    rows_by_bucket = build_rows_by_bucket(source, seed_rows, generation, now)
    new_buckets, written_count, deleted_count = write_changed_buckets(source, rows_by_bucket, old_buckets, now)
    graph.get_table().put_item(Item={"PK": sources.source_partition(source.source_id), "SK": BUCKETS_SORT_KEY,
                                     "generation": generation, "buckets": new_buckets})
    sources.record_sync(source, generation, now)
    return SyncReport(source.source_id, generation, written_count, deleted_count)


def is_synced_within_interval(source: SourceRow, now: str) -> bool:
    if source.last_sync_at is None:
        return False
    return parse_timestamp(now) - parse_timestamp(source.last_sync_at) < timedelta(minutes=source.sync_interval_minutes)


def sync_live_sources(now: str, only_due: bool) -> list[SyncReport]:
    """Every live source with indicator kinds (only_due: skip one synced less than an interval ago). An empty feed is logged,
    counted and left unstamped, so it turns stale and check() reads it unavailable."""
    require_feed_mock()
    reports = []
    for source in sources.list_newest_live_sources():
        if not source.indicator_kinds or (only_due and is_synced_within_interval(source, now)):
            continue
        try:
            reports.append(sync_source(source, read_snapshot_rows(source.source_id), now))
        except FeedEmpty as error:
            logger.error("feed sync refused: %s", error)
            emit_count_metric(LIST_SYNC_EMPTY_METRIC_NAME, "source", source.source_id)
    return reports
