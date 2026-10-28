"""Writes the seeded reference rows from infra/seed/, after every row is built and parsed:
- refund rules from every refund_rules[.<CC>].json (RULE# rows, live or proposed as filed);
- scam-list sample rows from lists.sample.json (LIST# rows, source: sample);
- the public-feed snapshot from lists.public.json (LIST# rows, source: public-feed, generation 0, the file's expiresAt), for
  snapshot sources only: a source with a feedParser gets its rows from the sync alone, so a re-seed never brings back a
  de-listed row;
- the source registry from sources.json (SOURCE#<id> / v1).
A (PK, SK) seen twice, or a public row already past its expiresAt, stops the script before any write; refresh expired
public rows with scripts/sync_list_feeds.py.

Usage: AWS_PROFILE=sendpidge python scripts/seed_rows.py
"""

import json
import sys
from decimal import Decimal
from pathlib import Path

import boto3

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT / "lambda"))

from shared.graph import now_timestamp  # noqa: E402
from shared.lists import list_row_from_seed, parse_list_row, public_list_row_from_seed  # noqa: E402
from shared.refund_rules import read_rule_seed_rows  # noqa: E402
from shared.sources import source_row_from_seed  # noqa: E402

TABLE_NAME = "sendpidge"
SEED_DIRECTORY = REPOSITORY_ROOT / "infra" / "seed"
LISTS_SAMPLE_SEED = SEED_DIRECTORY / "lists.sample.json"
LISTS_PUBLIC_SEED = SEED_DIRECTORY / "lists.public.json"
SOURCES_SEED = SEED_DIRECTORY / "sources.json"
REFUND_RULES_LABEL = "refund_rules*.json"
PUBLIC_SNAPSHOT_GENERATION = 0


def read_seed_file(seed_path: Path) -> list[dict]:
    return json.loads(seed_path.read_text(), parse_float=Decimal)


def list_live_fetch_source_ids() -> set[str]:
    return {str(seed["id"]) for seed in read_seed_file(SOURCES_SEED) if seed["feedParser"] is not None}


def build_public_rows(now: str) -> list[dict]:
    live_fetch_source_ids = list_live_fetch_source_ids()
    rows = [public_list_row_from_seed(seed, PUBLIC_SNAPSHOT_GENERATION, seed["expiresAt"]) for seed in read_seed_file(LISTS_PUBLIC_SEED)
            if seed["sourceId"] not in live_fetch_source_ids]
    expired_count = sum(1 for row in rows if row["expiresAt"] <= now)
    if expired_count:
        print("run scripts/sync_list_feeds.py")
        raise ValueError(f"{expired_count} row(s) in {LISTS_PUBLIC_SEED.name} are past their expiresAt")
    return rows


def require_unique_keys(rows_by_file: dict[str, list[dict]]) -> None:
    seen_keys: set[tuple[str, str]] = set()
    for file_label, rows in rows_by_file.items():
        for row in rows:
            row_key = (row["PK"], row["SK"])
            if row_key in seen_keys:
                raise ValueError(f"{row['PK']} {row['SK']} from {file_label} is seeded twice")
            seen_keys.add(row_key)


def build_rows_by_file() -> dict[str, list[dict]]:
    """Every row from every seed file, built and parsed by its lambda/shared module; nothing is written here."""
    sample_rows = [list_row_from_seed(seed) for seed in read_seed_file(LISTS_SAMPLE_SEED)]
    for row in sample_rows:
        parse_list_row(row)
    rows_by_file = {
        REFUND_RULES_LABEL: read_rule_seed_rows(SEED_DIRECTORY),
        LISTS_SAMPLE_SEED.name: sample_rows,
        LISTS_PUBLIC_SEED.name: build_public_rows(now_timestamp()),
        SOURCES_SEED.name: [source_row_from_seed(seed) for seed in read_seed_file(SOURCES_SEED)],
    }
    require_unique_keys(rows_by_file)
    return rows_by_file


def main() -> None:
    rows_by_file = build_rows_by_file()
    table = boto3.Session(region_name="us-east-1").resource("dynamodb").Table(TABLE_NAME)
    with table.batch_writer() as batch:
        for rows in rows_by_file.values():
            for row in rows:
                batch.put_item(Item=row)
    for file_label, rows in rows_by_file.items():
        print(f"Seeded {len(rows)} row(s) from {file_label}")


if __name__ == "__main__":
    main()
