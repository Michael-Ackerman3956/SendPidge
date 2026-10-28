"""CDK assertions on the keypad call (D9 wave 1): the retry role's Connect grant, Connect's invoke permission on pidge-verdict,
the VOICE_* environment, the two alarms, the VerdictFunctionArn output and the synth guard. test_stack.py is at its line limit,
so these live here and reuse its helpers and the session synth (conftest.py; synth-for-tests.ts turns voiceEnabled on)."""
import json

import pytest

from pidge_verdict.voice_answers import VOICE_ANSWER_FAILED_METRIC_NAME
from shared.metrics import METRIC_NAMESPACE
from shared.voice_ask import VOICE_ASK_FAILED_METRIC_NAME
from tests.test_stack import (
    INFRA_DIRECTORY, STACK_ACCOUNT, as_list, find_alarm_properties, find_function_by_handler, iterate_owned_policy_statements,
    resources_of_type, run_synth_for_tests, strip_trailing_logical_id_hash,
)
from tests.test_stack_messages import VERDICT_HANDLER, synthesize_template_with_overrides

RETRY_HANDLER = "pidge_retry.handler.handle"
VOICE_INSTANCE_ARN = "arn:aws:connect:ap-northeast-2:358148451054:instance/f6343b6c-43fc-4d43-b255-c54d8b0fb66a"  # cdk.json
CALL_ACTIONS = frozenset({"connect:StartOutboundVoiceContact", "connect:StopContact"})
CONNECT_SERVICE = "connect.amazonaws.com"
# cdk.json context key -> the pidge-retry environment name it becomes, beside VOICE_ENABLED (lambda/shared/voice_ask.py reads them).
RETRY_VOICE_ENVIRONMENT_NAME_BY_CONTEXT_KEY = {
    "voiceInstanceArn": "VOICE_INSTANCE_ARN", "voiceContactFlowId": "VOICE_CONTACT_FLOW_ID",
    "voiceSourcePhoneNumber": "VOICE_SOURCE_PHONE_NUMBER", "voiceCallsPerHour": "VOICE_CALLS_PER_HOUR",
    "voiceCallMaxSeconds": "VOICE_CALL_MAX_SECONDS",
}
VOICE_ALARM_METRIC_NAMES = {"VoiceAskFailedAlarm": VOICE_ASK_FAILED_METRIC_NAME, "VoiceAnswerFailedAlarm": VOICE_ANSWER_FAILED_METRIC_NAME}
VOICE_SETTINGS_REFUSAL = (
    "voiceEnabled needs voiceInstanceArn (a Connect instance ARN), voiceContactFlowId, voiceSourcePhoneNumber (E.164), "
    "voiceCallsPerHour > 0, voiceCallMaxSeconds 30-600 and retrySchedulerEnabled"
)


def read_cdk_json_context() -> dict:
    return json.loads((INFRA_DIRECTORY / "cdk.json").read_text())["context"]


def list_connect_grants(template: dict) -> list[tuple[str, dict]]:
    """(owner, statement) for every policy statement that names a connect:* action."""
    return [(owner, statement) for owner, statement in iterate_owned_policy_statements(template)
            if any(action.lower().startswith("connect:") for action in as_list(statement["Action"]))]


def list_connect_invoke_permissions(template: dict) -> list[dict]:
    return [permission["Properties"] for permission in resources_of_type(template, "AWS::Lambda::Permission").values()
            if permission["Properties"]["Principal"] == CONNECT_SERVICE]


def select_voice_environment(function: dict) -> dict:
    variables = function["Properties"].get("Environment", {}).get("Variables", {})
    return {name: value for name, value in variables.items() if name.startswith("VOICE_")}


def test_retry_may_start_and_stop_calls_only_on_the_one_instance(synthesized_template):
    _retry_logical_id, retry_function = find_function_by_handler(synthesized_template, RETRY_HANDLER)
    retry_role = strip_trailing_logical_id_hash(retry_function["Properties"]["Role"]["Fn::GetAtt"][0])
    assert [(owner, statement["Effect"], set(as_list(statement["Action"])), as_list(statement["Resource"]))
            for owner, statement in list_connect_grants(synthesized_template)] == [
        (retry_role, "Allow", CALL_ACTIONS, [f"{VOICE_INSTANCE_ARN}/contact/*"])]


def test_connect_may_invoke_only_verdict_from_our_instance(synthesized_template):
    verdict_logical_id, _verdict_function = find_function_by_handler(synthesized_template, VERDICT_HANDLER)
    verdict_function_arn = {"Fn::GetAtt": [verdict_logical_id, "Arn"]}
    connect_permissions = list_connect_invoke_permissions(synthesized_template)
    assert len(connect_permissions) == 1
    permission = connect_permissions[0]
    assert (permission["Action"], permission["FunctionName"], permission["SourceArn"], permission["SourceAccount"]) == (
        "lambda:InvokeFunction", verdict_function_arn, VOICE_INSTANCE_ARN, STACK_ACCOUNT)
    assert synthesized_template["Outputs"]["VerdictFunctionArn"]["Value"] == verdict_function_arn  # pasted into the flow


def test_voice_env(synthesized_template):
    cdk_context = read_cdk_json_context()
    retry_logical_id, retry_function = find_function_by_handler(synthesized_template, RETRY_HANDLER)
    verdict_logical_id, verdict_function = find_function_by_handler(synthesized_template, VERDICT_HANDLER)
    assert select_voice_environment(retry_function) == {"VOICE_ENABLED": "true", **{
        environment_name: str(cdk_context[context_key])
        for context_key, environment_name in RETRY_VOICE_ENVIRONMENT_NAME_BY_CONTEXT_KEY.items()}}
    assert select_voice_environment(verdict_function) == {"VOICE_ENABLED": "true", "VOICE_INSTANCE_ARN": cdk_context["voiceInstanceArn"]}
    assert [logical_id for logical_id, function in resources_of_type(synthesized_template, "AWS::Lambda::Function").items()
            if logical_id not in (retry_logical_id, verdict_logical_id) and select_voice_environment(function)] == []


def test_voice_alarms_read_the_lambda_metric_names(synthesized_template):
    for construct_id, metric_name in VOICE_ALARM_METRIC_NAMES.items():
        alarm = find_alarm_properties(synthesized_template, construct_id)
        assert (alarm["Namespace"], alarm["MetricName"], alarm["Statistic"], alarm["Period"]) == (METRIC_NAMESPACE, metric_name, "Sum", 300)
        assert (alarm["Threshold"], alarm["ComparisonOperator"]) == (1, "GreaterThanOrEqualToThreshold")
        assert "Dimensions" not in alarm


def test_voice_off_builds_nothing(tmp_path):
    template = synthesize_template_with_overrides(tmp_path / "voice_off", {"voiceEnabled": False})
    assert list_connect_grants(template) == []
    assert list_connect_invoke_permissions(template) == []
    template_text = json.dumps(template)
    assert CONNECT_SERVICE not in template_text and "arn:aws:connect:" not in template_text
    assert [logical_id for logical_id in resources_of_type(template, "AWS::CloudWatch::Alarm")
            if strip_trailing_logical_id_hash(logical_id) in VOICE_ALARM_METRIC_NAMES] == []
    assert "VerdictFunctionArn" not in template["Outputs"]
    _retry_logical_id, retry_function = find_function_by_handler(template, RETRY_HANDLER)
    assert select_voice_environment(retry_function) == {"VOICE_ENABLED": "false", **{
        environment_name: "" for environment_name in RETRY_VOICE_ENVIRONMENT_NAME_BY_CONTEXT_KEY.values()}}
    _verdict_logical_id, verdict_function = find_function_by_handler(template, VERDICT_HANDLER)
    assert select_voice_environment(verdict_function) == {"VOICE_ENABLED": "false", "VOICE_INSTANCE_ARN": ""}


@pytest.mark.parametrize("context_overrides", [{"retrySchedulerEnabled": False}, {"voiceInstanceArn": ""}])
def test_voice_settings_refused(tmp_path, context_overrides):
    refused = run_synth_for_tests(tmp_path, context_overrides)
    assert refused.returncode != 0
    assert VOICE_SETTINGS_REFUSAL in refused.stderr
