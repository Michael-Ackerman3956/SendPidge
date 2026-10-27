"""Runs one rules keeper session for one RULE# key against the mocked pages (infra/seed/mock_pages) and prints the outcome,
the proposed version and the day's spentUsd. A manual admin run: RULES_KEEPER_ENABLED is ignored, RULES_KEEPER_MOCK is set.

Usage: AWS_PROFILE=sendpidge python scripts/run_rules_keeper.py --rule-key 'RULE#US#card#any' [--model haiku-4.5]
"""

import argparse
import os
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT / "lambda"))
os.environ["TABLE_NAME"] = "sendpidge"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["RULES_KEEPER_MOCK"] = "true"

from shared import graph, refund_rules, rules_keeper  # noqa: E402
from shared.budget import BudgetLine, budget_partition_key, start_ledger  # noqa: E402
from shared.enums import ReaderModelChoice  # noqa: E402


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rule-key", required=True, help="RULE#<jurisdiction>#<paymentRail>#<party>")
    parser.add_argument("--model", default=str(ReaderModelChoice.HAIKU_4_5), choices=[str(choice) for choice in ReaderModelChoice])
    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()
    rule_key = refund_rules.rule_partition_key(*refund_rules.parse_rule_partition_key(arguments.rule_key))
    ledger = start_ledger(BudgetLine.RULES)
    result = rules_keeper.run_rules_keeper_for_key(rule_key, ledger, ReaderModelChoice(arguments.model))
    budget_row = graph.get_table().get_item(Key={"PK": budget_partition_key(ledger.day), "SK": str(ledger.line)}, ConsistentRead=True)["Item"]
    print(f"{rule_key}: {result.outcome}, proposed version {result.proposed_version}")
    print(f"BUDGET#{ledger.day}/{ledger.line}: spentUsd {budget_row['spentUsd']} of {budget_row['limitUsd']}")


if __name__ == "__main__":
    main()
