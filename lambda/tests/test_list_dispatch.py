"""pidge-list-dispatch on moto: the two mock feeds sync into LIST# rows that check() returns with attribution, the bucket
diff rewrites only changed rows, a stale feed reads unavailable, an empty or live feed is refused and never stamped, and
the list keeper proposes a SOURCE# row (never live) behind its flag, with the real Strands loop and a scripted model."""

import json
from decimal import Decimal

import pytest

from shared import graph, list_sync
from shared.budget import BudgetLedger, BudgetLine, KeeperSessionOutcome, KeeperSessionResult
from shared.enums import IndicatorKind, ListStrength, ProposedBy, ReaderModelChoice
from shared.list_keeper import ListKeeperSession, SourceProposal, build_list_keeper_agent, run_list_keeper_session, run_prompt
from shared.lists import CheckCoverage, Indicator
from shared.sources import (
    LicenceRefused, LicenceTag, SourceState, approve_source, check_against_live_sources, list_newest_live_sources,
    list_source_versions,
)
from tests.helpers.scripted_model import ScriptedModel, tool_call_step, verdict_step
from tests.test_sources import AMF, PHISHING_DATABASE, put_source, seed_source
from tests.test_verdict import aws  # noqa: F401

NOW = "2026-10-27T15:00:00Z"
TEST_FEED = "test-feed"
PUBLIC_SNAPSHOT = json.loads(list_sync.PUBLIC_SNAPSHOT_FILE.read_text(), parse_float=Decimal)
PROPOSAL = {
    "has_source": True, "name": "Phishing.Database active domains", "indicator_kind": "domain",
    "mapping": {"format": "text", "encoding": "utf-8"}, "ttl_days": 180, "sync_interval_minutes": 1440,
    "attribution_text": "Phishing.Database, MIT License", "attribution_url": "https://github.com/Phishing-Database/Phishing.Database",
}
LICENCE_STEP = tool_call_step("check_licence", {"terms_url": "https://github.com/Phishing-Database/Phishing.Database/blob/master/LICENSE"})


@pytest.fixture
def feeds_mock(aws, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LIST_FEEDS_MOCK", "true")


def build_test_feed_seed(source_id: str = TEST_FEED) -> dict:
    """A live, licence-checked source shaped like the Phishing.Database seed, hourly, rows kept 30 days."""
    return {**seed_source(PHISHING_DATABASE), "id": source_id, "syncIntervalMinutes": 60, "ttlDays": 30,
            "lastSyncAt": None, "lastGeneration": None}


def parcel_fee_rows(indexes) -> list[dict]:
    return [{"kind": "domain", "value": f"parcel-fee-{index:02d}.example", "category": "scam", "strength": "listed", "tier": 1,
             "reason": "Sample parcel-fee lure domain", "attribution": "Test feed", "addedAt": NOW, "expiresAt": NOW,
             "source": "public-feed", "sourceId": TEST_FEED} for index in indexes]


def snapshot_attribution(source_id: str, value: str) -> str:
    return next(row["attribution"] for row in PUBLIC_SNAPSHOT if row["sourceId"] == source_id and row["value"] == value)


def parcel_fee_row(index: int) -> dict | None:
    return graph.get_table().get_item(Key={"PK": "LIST#domain", "SK": f"parcel-fee-{index:02d}.example"}).get("Item")


def test_the_two_mock_feeds_seed_rows_that_check_returns_with_attribution(feeds_mock):
    put_source(seed_source(PHISHING_DATABASE))
    put_source(seed_source(AMF))
    reports = list_sync.sync_live_sources(NOW, only_due=False)
    assert [(report.source_id, report.written_count, report.deleted_count, report.generation) for report in reports] == [
        (AMF, 145, 0, 1), (PHISHING_DATABASE, 142, 0, 1)]
    assert {(source.source_id, source.last_sync_at, source.last_generation) for source in list_newest_live_sources()} == {
        (AMF, NOW, 1), (PHISHING_DATABASE, NOW, 1)}
    synced = graph.get_table().get_item(Key={"PK": "LIST#domain", "SK": "1password-login.com"})["Item"]
    assert (synced["expiresAt"], synced["ttlEpochSeconds"], synced["generation"]) == ("2027-04-25T15:00:00Z", 1808665200, 1)

    indicators = [Indicator(IndicatorKind.DOMAIN, "Mail.1Password-Login.com"), Indicator(IndicatorKind.URL, "HTTPS://MarketOrbitLabs.IT.com")]
    result = check_against_live_sources(indicators, "2026-10-28T15:00:00Z")
    assert [(hit.source_id, hit.strength, hit.attribution) for hit in result.hits] == [
        (PHISHING_DATABASE, ListStrength.LISTED, snapshot_attribution(PHISHING_DATABASE, "1password-login.com")),
        (AMF, ListStrength.LISTED, snapshot_attribution(AMF, "https://marketorbitlabs.it.com/"))]
    assert (result.coverage, result.cache_until) == (CheckCoverage.COMPLETE, "2026-10-29T15:00:00Z")
    assert [attribution["sourceId"] for attribution in result.as_payload()["attributions"]] == [PHISHING_DATABASE, AMF]

    stale = check_against_live_sources(indicators, "2026-10-30T15:00:01Z")
    assert (len(stale.hits), stale.coverage) == (2, CheckCoverage.UNAVAILABLE)
    assert list_sync.sync_live_sources("2026-10-27T16:00:00Z", only_due=True) == []


def test_the_bucket_diff_writes_only_changed_rows(feeds_mock):
    put_source(build_test_feed_seed())
    source = list_source_versions(TEST_FEED)[0]
    assert len({list_sync.bucket_of(IndicatorKind.DOMAIN, f"parcel-fee-{index:02d}.example") for index in range(1, 14)}) == 13
    first = list_sync.sync_source(source, parcel_fee_rows(range(1, 13)), NOW)
    assert (first.written_count, first.deleted_count, first.generation) == (12, 0, 1)
    second = list_sync.sync_source(source, parcel_fee_rows([*range(1, 12), 13]), NOW)
    assert (second.written_count, second.deleted_count, second.generation) == (1, 1, 2)
    assert [parcel_fee_row(index)["generation"] for index in range(1, 12)] == [1] * 11
    assert parcel_fee_row(13)["generation"] == 2 and parcel_fee_row(12) is None
    assert list_source_versions(TEST_FEED)[0].last_generation == 2


def test_an_empty_feed_or_live_fetch_is_refused_and_never_stamped(aws, monkeypatch, capsys):  # noqa: F811
    monkeypatch.delenv("LIST_FEEDS_MOCK", raising=False)
    with pytest.raises(list_sync.LiveFeedFetchNotBuilt):
        list_sync.sync_live_sources(NOW, only_due=False)

    monkeypatch.setenv("LIST_FEEDS_MOCK", "true")
    put_source({**build_test_feed_seed("test-empty"), "lastSyncAt": "2026-10-20T00:00:00Z"})
    capsys.readouterr()
    assert list_sync.sync_live_sources(NOW, only_due=False) == []
    assert list_source_versions("test-empty")[0].last_sync_at == "2026-10-20T00:00:00Z"
    assert graph.get_table().get_item(Key={"PK": "SOURCE#test-empty", "SK": list_sync.BUCKETS_SORT_KEY}).get("Item") is None
    metric_lines = [line for line in capsys.readouterr().out.splitlines() if list_sync.LIST_SYNC_EMPTY_METRIC_NAME in line]
    assert len(metric_lines) == 1 and json.loads(metric_lines[0])["source"] == "test-empty"


def keeper_with_scripted_model(session: ListKeeperSession, steps: list) -> tuple:
    agent = build_list_keeper_agent(ReaderModelChoice.HAIKU_4_5, session)
    scripted_model = ScriptedModel(SourceProposal, {run_prompt(session): steps})
    agent.model = scripted_model
    return agent, scripted_model


def test_the_list_keeper_proposes_a_source_from_a_mocked_sample_behind_its_flag(feeds_mock, monkeypatch):
    from pidge_list_dispatch import handler

    monkeypatch.delenv("LIST_KEEPER_ENABLED", raising=False)
    assert handler.handle({"trigger": "onboard", "sourceId": PHISHING_DATABASE}, None) == {"started": False}
    assert list_source_versions(PHISHING_DATABASE) == []

    ledger = BudgetLedger(BudgetLine.LISTS, "2026-10-27")
    session = ListKeeperSession(PHISHING_DATABASE, ledger)
    steps = [tool_call_step("read_source", {}), tool_call_step("fetch_sample", {}), LICENCE_STEP, verdict_step(PROPOSAL)]
    agent, scripted_model = keeper_with_scripted_model(session, steps)
    assert run_list_keeper_session(agent, session) == KeeperSessionResult(KeeperSessionOutcome.PROPOSED, 1)
    assert scripted_model.raised_errors == [] and len(scripted_model.recorded_calls) == 4
    proposed = list_source_versions(PHISHING_DATABASE)[0]
    assert (proposed.version, proposed.status, proposed.proposed_by, proposed.licence_tag) == (
        1, SourceState.PROPOSED, ProposedBy.KEEPER, LicenceTag.UNVERIFIED)
    assert proposed.mapping == {"format": "text", "encoding": "utf-8"}
    with pytest.raises(LicenceRefused):
        approve_source(PHISHING_DATABASE, 1)
    budget = graph.get_table().get_item(Key={"PK": "BUDGET#2026-10-27", "SK": "LISTS"})["Item"]
    assert budget["spentUsd"] == Decimal("0.24")  # 4 model calls and 2 fetches

    second_session = ListKeeperSession(PHISHING_DATABASE, ledger)
    missing_column = {**PROPOSAL, "mapping": {"format": "csv", "encoding": "utf-8", "delimiter": ",", "indicator_column": "missing"}}
    second_agent, _ = keeper_with_scripted_model(second_session, [LICENCE_STEP, verdict_step(missing_column)])
    assert run_list_keeper_session(second_agent, second_session) == KeeperSessionResult(KeeperSessionOutcome.REFUSED, None)
    assert len(list_source_versions(PHISHING_DATABASE)) == 1
