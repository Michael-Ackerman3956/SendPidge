"""Syncs every live SOURCE# feed now, due or not, from the mocked snapshot infra/seed/lists.public.json (LIST_FEEDS_MOCK=true):
LIST# rows get a new generation and expiresAt, and each source is stamped synced. Run scripts/seed_rows.py first.

Usage: AWS_PROFILE=sendpidge python scripts/sync_list_feeds.py
"""

import os
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT / "lambda"))
os.environ["TABLE_NAME"] = "sendpidge"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["LIST_FEEDS_MOCK"] = "true"

from shared.graph import now_timestamp  # noqa: E402
from shared.list_sync import sync_live_sources  # noqa: E402


def main() -> None:
    reports = sync_live_sources(now_timestamp(), only_due=False)
    for report in reports:
        print(f"{report.source_id}: generation {report.generation}, {report.written_count} written, {report.deleted_count} deleted")
    print(f"Synced {len(reports)} live feed(s)")


if __name__ == "__main__":
    main()
