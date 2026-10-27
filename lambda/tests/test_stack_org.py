"""CDK assertions on the org dashboard's resources (S3): scoped org roles, the projection's filters and dead-letter queue, the
OrgActions route and the synth guard. test_stack.py is at its line limit, so these live here and reuse its helpers and the session
synth (conftest.py)."""
import fnmatch
import json

from pidge_org import handler as org_actions_handler
from tests.test_stack import (
    as_list, collect_role_allow_statements, find_alarm_properties, find_function_by_handler, render_intrinsic_as_text, resources_of_type,
    run_synth_for_tests, statement_grants, strip_trailing_logical_id_hash,
)

ORG_ROLES = ("OrgProjectionFunctionServiceRole", "OrgActionsFunctionServiceRole", "OrgCanaryRole")
TABLE_WRITE_ACTIONS = (
    "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem", "dynamodb:ConditionCheckItem",
    "dynamodb:PartiQLInsert", "dynamodb:PartiQLUpdate", "dynamodb:PartiQLDelete",
)
RESIDENT_PARTITION_KEY = "RECEIVER#grandma@sendpidge.com"
CANARY_PARTITION_KEY = "ORG#test-home"  # synth-for-tests.ts orgId
RELEASE_SCHEDULER_REFUSAL = "orgEnabled needs releaseSchedulerEnabled"
# (eventName, PK, SK) of rows the projection maps, and of rows it must never wake on (its own writes among them).
PROJECTED_RECORD_KEYS = (
    ("INSERT", "REQUEST#req-1", "META"), ("MODIFY", "REQUEST#req-1", "META"), ("INSERT", "REQUEST#req-1", "NOTE#2026-10-28"),
    ("MODIFY", "EMAIL#email-1", "META"), ("INSERT", "CASE#case-1", "META"), ("MODIFY", "CASE#case-1", "META"),
    ("INSERT", "ORG#home", "RESOLVE#REQUEST#req-1"), ("MODIFY", "ORG#home", "CANARY"),
    ("MODIFY", RESIDENT_PARTITION_KEY, "ORGPREF#home"), ("INSERT", RESIDENT_PARTITION_KEY, "CONTACT#jimin"),
    ("INSERT", "USER#grandma", "CONN#connection-1"),
)
IGNORED_RECORD_KEYS = (
    ("REMOVE", "REQUEST#req-1", "META"), ("MODIFY", "USER#grandma", "CONN#connection-1"), ("INSERT", RESIDENT_PARTITION_KEY, "PROFILE"),
    ("INSERT", "CHAT#grandma", "MSG#1"), ("MODIFY", "ORG#home", "MEMBER#grandma@sendpidge.com"), ("MODIFY", "ORG#home", "PROFILE"),
    ("INSERT", "ORGLOG#home", "REQUEST#req-1#message_held#2026-10-28T00:00:00Z"), ("MODIFY", "ORGSTAT#home", "DAY#2026-10-28"),
    ("INSERT", "ORGAUDIT#home", "TS#2026-10-28T00:00:00Z#audit-1"),
)


def stream_filter_matches(filter_pattern: dict, event_name: str, partition_key: str, sort_key: str) -> bool:
    """Event-pattern match for the rules the stack writes: a list of exact strings or {"prefix": ...}; any other rule raises."""
    assert filter_pattern.keys() == {"eventName", "dynamodb"} and filter_pattern["dynamodb"].keys() == {"Keys"}, filter_pattern

    def any_rule_matches(rules: list, value: str) -> bool:
        return any(rule == value if isinstance(rule, str) else value.startswith(rule["prefix"]) for rule in rules)

    keys = filter_pattern["dynamodb"]["Keys"]
    return (any_rule_matches(filter_pattern["eventName"], event_name) and any_rule_matches(keys["PK"]["S"], partition_key)
            and any_rule_matches(keys["SK"]["S"], sort_key))


def test_org_functions_cannot_write_receiver_partition(synthesized_template):
    for role in ORG_ROLES:
        statements = collect_role_allow_statements(synthesized_template, role)
        write_statements = [statement for statement in statements if any(statement_grants(statement, action) for action in TABLE_WRITE_ACTIONS)]
        assert write_statements, f"{role} has no table write"
        assert not any(statement_grants(statement, "dynamodb:Scan") for statement in statements), role  # also catches dynamodb:*
        for statement in write_statements:
            leading_key_patterns = as_list(statement["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"])
            assert statement["Condition"]["Null"] == {"dynamodb:LeadingKeys": "false"}, role
            assert not any(fnmatch.fnmatchcase(RESIDENT_PARTITION_KEY, pattern) for pattern in leading_key_patterns), role
            if role == "OrgCanaryRole":
                assert leading_key_patterns == [CANARY_PARTITION_KEY]


def test_projection_dlq_and_alarm_present(synthesized_template):
    projection_logical_id, _function = find_function_by_handler(synthesized_template, "pidge_org_projection.handler.handle")
    [queue_logical_id] = [logical_id for logical_id in resources_of_type(synthesized_template, "AWS::SQS::Queue")
                          if strip_trailing_logical_id_hash(logical_id) == "OrgProjectionDeadLetterQueue"]
    [mapping] = [mapping["Properties"] for mapping in resources_of_type(synthesized_template, "AWS::Lambda::EventSourceMapping").values()
                 if mapping["Properties"]["FunctionName"] == {"Ref": projection_logical_id}]
    assert mapping["DestinationConfig"]["OnFailure"]["Destination"] == {"Fn::GetAtt": [queue_logical_id, "Arn"]}
    assert (mapping["FunctionResponseTypes"], mapping["BisectBatchOnFunctionError"], mapping["MaximumRetryAttempts"]) == (
        ["ReportBatchItemFailures"], True, 2)
    assert (mapping["StartingPosition"], mapping["BatchSize"]) == ("LATEST", 10)
    filter_patterns = [json.loads(stream_filter["Pattern"]) for stream_filter in mapping["FilterCriteria"]["Filters"]]
    assert len(filter_patterns) == 5
    assert [keys for keys in PROJECTED_RECORD_KEYS if not any(stream_filter_matches(pattern, *keys) for pattern in filter_patterns)] == []
    assert [keys for keys in IGNORED_RECORD_KEYS if any(stream_filter_matches(pattern, *keys) for pattern in filter_patterns)] == []
    alarm = find_alarm_properties(synthesized_template, "OrgProjectionDeadLetterAlarm")
    assert alarm["Dimensions"] == [{"Name": "QueueName", "Value": {"Fn::GetAtt": [queue_logical_id, "QueueName"]}}]
    assert (alarm["MetricName"], alarm["Threshold"], alarm["ComparisonOperator"]) == (
        "ApproximateNumberOfMessagesVisible", 1, "GreaterThanOrEqualToThreshold")


def test_org_action_handlers_are_the_org_actions_route_keys(synthesized_template):
    [integration_logical_id] = [logical_id for logical_id in resources_of_type(synthesized_template, "AWS::ApiGatewayV2::Integration")
                                if "OrgActionsIntegration" in logical_id]
    route_keys = {route["Properties"]["RouteKey"] for route in resources_of_type(synthesized_template, "AWS::ApiGatewayV2::Route").values()
                  if render_intrinsic_as_text(route["Properties"]["Target"]) == "integrations/${%s}" % integration_logical_id}
    assert route_keys == set(org_actions_handler.ORG_ACTION_HANDLERS)


def test_org_enabled_requires_release_scheduler(tmp_path):
    completed = run_synth_for_tests(tmp_path, {"releaseSchedulerEnabled": False})
    assert completed.returncode != 0
    assert RELEASE_SCHEDULER_REFUSAL in completed.stderr
