"""Two modes, both from a laptop.

Default: syncs every live snapshot SOURCE# feed (no feedParser) now from infra/seed/lists.public.json (LIST_FEEDS_MOCK=true):
LIST# rows get a new generation and expiresAt, and each source is stamped synced. Run scripts/seed_rows.py first. Live-fetch
feeds are synced by pidge-list-dispatch, never here.

--source <id> --dry-run <file>: parses a downloaded feed file offline with that source's feed parser (from
infra/seed/sources.json), prints its row counts by kind, the skipped count and three keys, and writes nothing. Exits 1
when the file does not parse or yields no rows.

Usage: AWS_PROFILE=sendpidge python scripts/sync_list_feeds.py
       python scripts/sync_list_feeds.py --source amf-listes-noires --dry-run lambda/tests/fixtures/feeds/amf_5rows.csv
"""

import argparse
import json
import os
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT / "lambda"))
os.environ["TABLE_NAME"] = "sendpidge"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["LIST_FEEDS_MOCK"] = "true"

from shared.enums import IndicatorKind  # noqa: E402
from shared.feed_parsers import FeedParseError, parse_feed  # noqa: E402
from shared.graph import now_timestamp  # noqa: E402
from shared.list_sync import sync_snapshot_sources  # noqa: E402
from shared.lists import indicator_key  # noqa: E402
from shared.sources import parse_source_row, source_row_from_seed  # noqa: E402

SOURCES_SEED = REPOSITORY_ROOT / "infra" / "seed" / "sources.json"
DRY_RUN_SAMPLE_KEY_COUNT = 3


def sync_snapshots() -> None:
    reports = sync_snapshot_sources(now_timestamp())
    for report in reports:
        print(f"{report.source_id}: generation {report.generation}, {report.written_count} written, {report.deleted_count} deleted")
    print(f"Synced {len(reports)} snapshot feed(s)")


def dry_run_feed_file(source_id: str, feed_path: Path) -> int:
    """Parses only: no table is read or written. Returns the exit code."""
    matching_seeds = [seed for seed in json.loads(SOURCES_SEED.read_text(), parse_float=Decimal) if seed["id"] == source_id]
    if not matching_seeds:
        print(f"{source_id} is not in {SOURCES_SEED.name}")
        return 1
    source = parse_source_row(source_row_from_seed(matching_seeds[0]))
    try:
        parsed_feed = parse_feed(source, feed_path.read_bytes(), now_timestamp())
    except FeedParseError as error:
        print(f"{source_id}: the file does not parse ({error})")
        return 1
    row_counts = Counter(row["kind"] for row in parsed_feed.rows)
    counts_by_kind = ", ".join(f"{count} {kind}" for kind, count in sorted(row_counts.items()))
    print(f"{source_id}: {counts_by_kind}, {parsed_feed.skipped_count} skipped")
    for row in parsed_feed.rows[:DRY_RUN_SAMPLE_KEY_COUNT]:
        print(f"  LIST#{row['kind']} {indicator_key(IndicatorKind(row['kind']), row['value'])}")
    if not parsed_feed.rows:
        print(f"{source_id}: no rows; a sync would refuse this feed")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync snapshot feeds, or dry-run one live feed file offline.")
    parser.add_argument("--source", help="the SOURCE# id whose feed parser reads the file")
    parser.add_argument("--dry-run", type=Path, help="a downloaded feed file to parse without writing")
    arguments = parser.parse_args()
    if (arguments.source is None) != (arguments.dry_run is None):
        parser.error("--source and --dry-run go together")
    if arguments.dry_run is None:
        sync_snapshots()
        return
    sys.exit(dry_run_feed_file(arguments.source, arguments.dry_run))


if __name__ == "__main__":
    main()
