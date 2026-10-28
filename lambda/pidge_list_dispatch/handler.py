"""pidge-list-dispatch: {"trigger": "sync"} fetches every live-fetch feed that is due (hourly schedule) and {"trigger":
"sync_now"} every one, due or not; {"trigger": "onboard", "sourceId": ...} runs one list-keeper session when
LIST_KEEPER_ENABLED is "true". Flags are read on each call, never at import."""

import logging
import os
from enum import StrEnum

from pidge_list_dispatch import feed_fetch
from shared import graph, list_keeper, list_sync
from shared.budget import BudgetLine, start_ledger
from shared.enums import ReaderModelChoice

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class ListDispatchTrigger(StrEnum):
    SYNC = "sync"
    SYNC_NOW = "sync_now"
    ONBOARD = "onboard"


def sync_live_fetch_sources(only_due: bool) -> dict:
    opener = feed_fetch.build_feed_opener()
    reports = list_sync.sync_fetched_sources(
        graph.now_timestamp(), only_due, lambda url, validators: feed_fetch.fetch_feed(url, validators, opener),
    )
    return {"synced": [{"sourceId": report.source_id, "outcome": str(report.outcome), "generation": report.generation,
                        "written": report.written_count, "deleted": report.deleted_count, "skipped": report.skipped_count}
                       for report in reports]}


def handle(event, context):
    trigger = ListDispatchTrigger(event["trigger"])
    if trigger is ListDispatchTrigger.SYNC:
        return sync_live_fetch_sources(only_due=True)
    if trigger is ListDispatchTrigger.SYNC_NOW:
        return sync_live_fetch_sources(only_due=False)
    if os.environ.get("LIST_KEEPER_ENABLED") != "true":
        logger.info("list keeper is off (LIST_KEEPER_ENABLED); onboarding not started")
        return {"started": False}
    result = list_keeper.run_list_keeper_for_source(
        event["sourceId"], start_ledger(BudgetLine.LISTS), ReaderModelChoice(os.environ["LIST_KEEPER_MODEL"]),
    )
    return {"started": True, "outcome": str(result.outcome), "proposedVersion": result.proposed_version}
