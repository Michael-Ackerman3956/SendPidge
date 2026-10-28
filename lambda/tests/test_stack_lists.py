"""CDK assertions on the live scam-list sync (SX3): pidge-list-dispatch on an hourly rule with table access only, and the
FeedStale alarm. test_stack.py is near its line limit, so these live here and reuse its helpers and the session synth
(conftest.py)."""
import json

from pidge_list_dispatch.handler import ListDispatchTrigger
from shared.list_sync import FEED_STALE_METRIC_NAME
from shared.metrics import METRIC_NAMESPACE
from tests.test_stack import (
    TABLE_READ_WRITE_ACTIONS, as_list, collect_role_allow_statements, find_alarm_properties, find_function_by_handler,
    render_intrinsic_as_text, resources_of_type, strip_logical_id_hashes, strip_trailing_logical_id_hash,
)

FORBIDDEN_ACTION_PREFIXES = ("bedrock:", "secretsmanager:", "s3:")
TABLE_RESOURCES = {"${Table.Arn}", "${Table.Arn}/index/*"}


def test_list_dispatch_syncs_hourly_with_table_access_only(synthesized_template):
    function_logical_id, list_dispatch_function = find_function_by_handler(synthesized_template, "pidge_list_dispatch.handler.handle")
    function_properties = list_dispatch_function["Properties"]
    assert (function_properties["Timeout"], function_properties["ReservedConcurrentExecutions"]) == (300, 1)
    environment = function_properties["Environment"]["Variables"]
    assert environment["LIST_KEEPER_ENABLED"] == "false"
    assert "LIST_FEEDS_MOCK" not in environment
    [(schedule_logical_id, schedule)] = [(logical_id, rule["Properties"])
                                         for logical_id, rule in resources_of_type(synthesized_template, "AWS::Events::Rule").items()
                                         if strip_trailing_logical_id_hash(logical_id) == "ListSyncSchedule"]
    assert (schedule["ScheduleExpression"], schedule["State"]) == ("rate(1 hour)", "ENABLED")
    [target] = schedule["Targets"]
    assert target["Arn"] == {"Fn::GetAtt": [function_logical_id, "Arn"]}
    assert json.loads(render_intrinsic_as_text(target["Input"])) == {"trigger": str(ListDispatchTrigger.SYNC)}
    statements = collect_role_allow_statements(synthesized_template, function_properties["Role"]["Fn::GetAtt"][0])
    actions = [action for statement in statements for action in as_list(statement["Action"])]
    assert actions
    assert [action for action in actions if action.lower().startswith(FORBIDDEN_ACTION_PREFIXES)] == []
    assert set(actions) <= TABLE_READ_WRITE_ACTIONS
    assert {strip_logical_id_hashes(render_intrinsic_as_text(resource))
            for statement in statements for resource in as_list(statement["Resource"])} == TABLE_RESOURCES
    outputs = synthesized_template["Outputs"]
    assert outputs["ListDispatchFunctionName"]["Value"] == {"Ref": function_logical_id}
    assert outputs["ListSyncScheduleName"]["Value"] == {"Ref": schedule_logical_id}


def test_feed_stale_alarm_pages_on_the_first_stale_feed(synthesized_template):
    alarm = find_alarm_properties(synthesized_template, "FeedStaleAlarm")
    assert (alarm["Namespace"], alarm["MetricName"], alarm["Statistic"], alarm["Period"]) == (
        METRIC_NAMESPACE, FEED_STALE_METRIC_NAME, "Sum", 3600)
    assert (alarm["Threshold"], alarm["EvaluationPeriods"], alarm["ComparisonOperator"], alarm["TreatMissingData"]) == (
        1, 1, "GreaterThanOrEqualToThreshold", "notBreaching")
    assert "Dimensions" not in alarm
