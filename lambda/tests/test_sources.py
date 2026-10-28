"""SOURCE# registry: every peer seed source parses, live is refused for a non-commercial-only or unverified licence (in code
and in the DynamoDB condition), and a proposed mapping must read at least ten indicators from its sample. The no-derivatives
and share-alike refusals are proven with the sync in test_list_dispatch.py."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from shared import graph, sources
from shared.enums import IndicatorKind
from shared.sources import (
    LicenceRefused, LicenceTag, MappingRejected, SourceNotProposed, SourceState, approve_source, dry_run_mapping,
    list_newest_live_sources, list_source_versions, parse_source_row, source_row_from_seed,
)
from tests.test_verdict import aws  # noqa: F401

SEED_DIRECTORY = Path(__file__).resolve().parents[2] / "infra" / "seed"
FEEDS_DIRECTORY = SEED_DIRECTORY / "feeds"
PHISHING_DATABASE = "phishing-database"
AMF = "amf-listes-noires"
TEXT_MAPPING = {"format": "text", "encoding": "utf-8"}
AMF_MAPPING = {"format": "csv", "encoding": "utf-8-sig", "delimiter": ";", "indicatorColumn": "nom"}


def seed_sources() -> list[dict]:
    return json.loads((SEED_DIRECTORY / "sources.json").read_text(), parse_float=Decimal)


def seed_source(source_id: str) -> dict:
    return next(seed for seed in seed_sources() if seed["id"] == source_id)


def put_source(seed: dict) -> None:
    graph.get_table().put_item(Item=source_row_from_seed(seed))


def parcel_fee_domains(count: int) -> bytes:
    return "\n".join(f"parcel-fee-{index:02d}.example" for index in range(1, count + 1)).encode()


def test_every_seed_source_parses_and_the_licence_rule_holds():
    parsed = {source.source_id: source for source in (parse_source_row(source_row_from_seed(seed)) for seed in seed_sources())}
    assert len(parsed) == len(seed_sources())
    for source_id in (PHISHING_DATABASE, AMF):
        assert (parsed[source_id].status, parsed[source_id].licence_tag) == (SourceState.LIVE, LicenceTag.COMMERCIAL_OK_ATTRIBUTION)
    unverified = parsed["ftc-dnc-reported-calls"]
    assert (unverified.status, unverified.licence_tag) == (SourceState.PROPOSED, LicenceTag.UNVERIFIED)
    for refused_tag in ("non-commercial-only", "unverified"):
        with pytest.raises(LicenceRefused):
            source_row_from_seed({**seed_source(PHISHING_DATABASE), "licenceTag": refused_tag})


def test_approve_refuses_an_unverified_licence(aws, monkeypatch):  # noqa: F811
    for seed in seed_sources():
        put_source(seed)
    with pytest.raises(LicenceRefused):
        approve_source("ftc-dnc-reported-calls", 1)
    assert list_source_versions("ftc-dnc-reported-calls")[0].status is SourceState.PROPOSED
    approve_source("fcc-cgb-complaints", 1)
    assert list_source_versions("fcc-cgb-complaints")[0].status is SourceState.LIVE
    with pytest.raises(SourceNotProposed):
        approve_source("fcc-cgb-complaints", 1)
    live_ids = {source.source_id for source in list_newest_live_sources()}
    assert {PHISHING_DATABASE, AMF, "fcc-cgb-complaints"} <= live_ids and "jfsa-mutouroku" not in live_ids

    monkeypatch.setattr(sources, "REFUSED_LIVE_LICENCES", frozenset())  # with the code check gone, the DynamoDB condition refuses
    with pytest.raises(SourceNotProposed):
        approve_source("ftc-dnc-reported-calls", 1)
    assert graph.get_table().get_item(Key={"PK": "SOURCE#ftc-dnc-reported-calls", "SK": "v1"})["Item"]["status"] == "proposed"


def test_a_mapping_dry_run_under_ten_indicators_is_rejected():
    with pytest.raises(MappingRejected):
        dry_run_mapping(TEXT_MAPPING, IndicatorKind.DOMAIN, parcel_fee_domains(9))
    assert len(dry_run_mapping(TEXT_MAPPING, IndicatorKind.DOMAIN, parcel_fee_domains(10))) == 10
    phishing_database_sample = (FEEDS_DIRECTORY / "phishing-database.sample.txt").read_bytes()
    assert len(dry_run_mapping(TEXT_MAPPING, IndicatorKind.DOMAIN, phishing_database_sample)) == 124  # co.com hosts collapse
    amf_sample = (FEEDS_DIRECTORY / "amf-listes-noires.sample.csv").read_bytes()
    assert len(dry_run_mapping(AMF_MAPPING, IndicatorKind.DOMAIN, amf_sample)) == 145
    for broken_mapping in ({**AMF_MAPPING, "indicatorColumn": "missing"}, {**AMF_MAPPING, "encoding": "rot13"}):
        with pytest.raises(MappingRejected):
            dry_run_mapping(broken_mapping, IndicatorKind.DOMAIN, amf_sample)
