"""Writes the seeded reference rows: the four US refund rules from infra/seed/refund_rules.json (RULE# rows, live).

Usage: AWS_PROFILE=sendpidge python scripts/seed_rows.py
"""

import json
import sys
from pathlib import Path

import boto3

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT / "lambda"))

from shared.refund_rules import parse_rule_row, rule_row_from_seed  # noqa: E402

TABLE_NAME = "sendpidge"
REFUND_RULES_SEED = REPOSITORY_ROOT / "infra" / "seed" / "refund_rules.json"


def main() -> None:
    table = boto3.Session(region_name="us-east-1").resource("dynamodb").Table(TABLE_NAME)
    rows = [rule_row_from_seed(seed_rule) for seed_rule in json.loads(REFUND_RULES_SEED.read_text())]
    for row in rows:
        parse_rule_row(row)  # the envelope must parse before anything is written
        table.put_item(Item=row)
    print(f"Seeded {len(rows)} refund rules: {', '.join(row['PK'] for row in rows)}")


if __name__ == "__main__":
    main()
