"""BUDGET#<day> ledgers: every keeper model call and page fetch debits the day's line with a conditional write before it runs.

A refused debit marks the ledger exhausted and raises BudgetExhausted; callers check `ledger.is_exhausted`, never the
exception type (Strands may wrap a hook's exception). Money is Decimal built from a string, never a float.
"""

import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from botocore.exceptions import ClientError
from strands.hooks import BeforeModelCallEvent, HookProvider, HookRegistry

from shared import graph
from shared.metrics import emit_count_metric

logger = logging.getLogger(__name__)


class BudgetLine(StrEnum):
    """The SK under BUDGET#<day>: one daily line per keeper."""

    RULES = "META"
    LISTS = "LISTS"


DAILY_LIMIT_USD = {BudgetLine.RULES: Decimal("20"), BudgetLine.LISTS: Decimal("10")}
MODEL_CALL_COST_USD = Decimal("0.05")    # pre-call estimate, not the billed cost
BROWSER_CALL_COST_USD = Decimal("0.02")  # pre-call estimate, not the billed cost
BUDGET_EXHAUSTED_METRIC_NAME = "KeeperBudgetExhausted"
BUDGET_DAY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CONDITIONAL_CHECK_FAILED = "ConditionalCheckFailedException"


class BudgetExhausted(Exception):
    """A debit was refused: the day's line would pass its limit."""


class KeeperSessionOutcome(StrEnum):
    PROPOSED = "proposed"
    NO_PROPOSAL = "no_proposal"
    REFUSED = "refused"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True)
class KeeperSessionResult:
    outcome: KeeperSessionOutcome
    proposed_version: int | None


def budget_partition_key(day: str) -> str:
    if not BUDGET_DAY_PATTERN.match(day):
        raise ValueError("a budget day is YYYY-MM-DD")
    return f"BUDGET#{day}"


@dataclass
class BudgetLedger:
    """One day's line. `is_exhausted` turns True on the first refused debit and every later debit raises without a write."""

    line: BudgetLine
    day: str
    is_exhausted: bool = False

    @property
    def limit_usd(self) -> Decimal:
        return DAILY_LIMIT_USD[self.line]

    def row_key(self) -> dict:
        return {"PK": budget_partition_key(self.day), "SK": str(self.line)}

    def count_session(self) -> None:
        graph.get_table().update_item(
            Key=self.row_key(),
            UpdateExpression="SET spentUsd = if_not_exists(spentUsd, :zero), limitUsd = :limit ADD sessions :one",
            ExpressionAttributeValues={":zero": Decimal("0"), ":limit": self.limit_usd, ":one": 1},
        )

    def count_proposal(self) -> None:
        graph.get_table().update_item(
            Key=self.row_key(), UpdateExpression="ADD proposals :one", ExpressionAttributeValues={":one": 1},
        )

    def has_room_for(self, cost_usd: Decimal) -> bool:
        """True when no row exists yet or the spent amount leaves room for the cost."""
        item = graph.get_table().get_item(Key=self.row_key(), ConsistentRead=True).get("Item")
        if item is None:
            return True
        return item["spentUsd"] <= self.limit_usd - cost_usd

    def debit(self, cost_usd: Decimal) -> Decimal:
        """Adds the cost only while spentUsd + cost stays within the limit; returns the new spentUsd. Fails closed."""
        if cost_usd <= 0 or cost_usd > self.limit_usd:
            raise ValueError("a debit is positive and within the day's limit")
        if self.is_exhausted:
            raise BudgetExhausted(f"{self.line} budget for {self.day} is exhausted")
        try:
            response = graph.get_table().update_item(
                Key=self.row_key(),
                UpdateExpression="SET limitUsd = :limit ADD spentUsd :cost",
                ConditionExpression="attribute_not_exists(spentUsd) OR spentUsd <= :ceiling",
                ExpressionAttributeValues={":limit": self.limit_usd, ":cost": cost_usd, ":ceiling": self.limit_usd - cost_usd},
                ReturnValues="UPDATED_NEW",
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != CONDITIONAL_CHECK_FAILED:
                raise
            self.is_exhausted = True
            emit_count_metric(BUDGET_EXHAUSTED_METRIC_NAME, "line", str(self.line))
            logger.warning("budget %s for %s refused a debit: exhausted", self.line, self.day)
            raise BudgetExhausted(f"{self.line} budget for {self.day} is exhausted") from None
        return response["Attributes"]["spentUsd"]


def start_ledger(line: BudgetLine) -> BudgetLedger:
    return BudgetLedger(line, graph.now_timestamp()[:10])


class BudgetHook(HookProvider):
    """Debits one model call before Strands makes it; a refused debit raises out of the agent loop."""

    def __init__(self, ledger: BudgetLedger) -> None:
        self.ledger = ledger

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeModelCallEvent, self.debit_before_model_call)

    def debit_before_model_call(self, event: BeforeModelCallEvent) -> None:
        self.ledger.debit(MODEL_CALL_COST_USD)
