"""pidge-list-dispatch: {"trigger": "sync"} syncs every live feed that is due (hourly schedule); {"trigger": "onboard",
"sourceId": ...} runs one list-keeper session when LIST_KEEPER_ENABLED is "true". Flags are read on each call, never at import."""

import logging
import os
from enum import StrEnum

from shared import graph, list_keeper, list_sync
from shared.budget import BudgetLine, start_ledger
from shared.enums import ReaderModelChoice

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class ListDispatchTrigger(StrEnum):
    SYNC = "sync"
    ONBOARD = "onboard"


def handle(event, context):
    trigger = ListDispatchTrigger(event["trigger"])
    if trigger is ListDispatchTrigger.SYNC:
        reports = list_sync.sync_live_sources(graph.now_timestamp(), only_due=True)
        return {"synced": [{"sourceId": report.source_id, "generation": report.generation, "written": report.written_count,
                            "deleted": report.deleted_count} for report in reports]}
    if os.environ.get("LIST_KEEPER_ENABLED") != "true":
        logger.info("list keeper is off (LIST_KEEPER_ENABLED); onboarding not started")
        return {"started": False}
    result = list_keeper.run_list_keeper_for_source(
        event["sourceId"], start_ledger(BudgetLine.LISTS), ReaderModelChoice(os.environ["LIST_KEEPER_MODEL"]),
    )
    return {"started": True, "outcome": str(result.outcome), "proposedVersion": result.proposed_version}
