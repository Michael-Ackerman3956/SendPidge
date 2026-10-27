"""Approves or rejects one proposed RULE# version. Approve makes it live and keeps the older live version; reject stores the
reason so the keeper never proposes the same steps again. Only a proposed version can be approved or rejected.

Usage: AWS_PROFILE=sendpidge python scripts/approve_rule.py approve --rule-key 'RULE#US#card#any' --version 2
       AWS_PROFILE=sendpidge python scripts/approve_rule.py reject --rule-key 'RULE#US#card#any' --version 2 --reason "Copies the FTC page"
"""

import argparse
import os
import sys
from enum import StrEnum
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT / "lambda"))
os.environ["TABLE_NAME"] = "sendpidge"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"

from shared import refund_rules  # noqa: E402


class RuleReviewCommand(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command in RuleReviewCommand:
        subcommand = subcommands.add_parser(str(command))
        subcommand.add_argument("--rule-key", required=True, help="RULE#<jurisdiction>#<paymentRail>#<party>")
        subcommand.add_argument("--version", required=True, type=int)
        if command is RuleReviewCommand.REJECT:
            subcommand.add_argument("--reason", required=True)
    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()
    if RuleReviewCommand(arguments.command) is RuleReviewCommand.APPROVE:
        refund_rules.approve_rule(arguments.rule_key, arguments.version)
        print(f"{arguments.rule_key} v{arguments.version} is live")
        return
    refund_rules.reject_rule(arguments.rule_key, arguments.version, arguments.reason)
    print(f"{arguments.rule_key} v{arguments.version} is rejected")


if __name__ == "__main__":
    main()
