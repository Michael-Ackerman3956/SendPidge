"""CDK assertions on pidge-inbound (slice 15): its topic subscriptions with SourceArn-scoped invoke permissions, its dead-letter
queue and alarm, its pipeline environment, and the SMS topic that exists only with smsEnabled. test_stack.py is at its line limit,
so these live here and reuse its helpers and the session synth (conftest.py)."""
import json

from tests.test_stack import (
    INBOUND_TOPICS, INBOUND_TOPICS_WITHOUT_SMS, assert_each_inbound_topic_takes_publishes_only_from_this_account, find_alarm_properties,
    find_function_by_handler, resources_of_type, run_synth_for_tests, strip_trailing_logical_id_hash,
)

INBOUND_HANDLER = "pidge_inbound.handler.handle"
READER_HANDLER = "pidge_reader.handler.handle"
VERDICT_HANDLER = "pidge_verdict.handler.handle"
MESSENGER_RESIDENT_ADDRESS = "grandma@sendpidge.com"  # cdk.json messengerResidentAddress
# pidge-inbound's keys beyond the pipeline environment it shares with the Reader; only the Reader reads stored email.
INBOUND_ONLY_ENVIRONMENT_KEYS = frozenset({
    "WHATSAPP_TOPIC_ARN", "RCS_TOPIC_ARN", "SMS_TOPIC_ARN", "SMS_ORIGINATION_NUMBER", "MESSENGER_RESIDENT_ADDRESS"})
READER_ONLY_ENVIRONMENT_KEYS = frozenset({"EMAIL_BUCKET"})
SMS_ORIGINATION_NUMBER = "+18885550199"
SMS_ON_OVERRIDES = {
    "smsEnabled": True, "smsOriginationNumber": SMS_ORIGINATION_NUMBER,
    "smsPhoneNumberArn": "arn:aws:sms-voice:us-east-1:358148451054:phone-number/phone-test",
}
SMS_SETTINGS_REFUSAL = "smsEnabled needs smsOriginationNumber (E.164), smsPhoneNumberArn and messengerResidentAddress"


def find_construct_logical_id(template: dict, resource_type: str, construct_id: str) -> str:
    [logical_id] = [logical_id for logical_id in resources_of_type(template, resource_type)
                    if strip_trailing_logical_id_hash(logical_id) == construct_id]
    return logical_id


def list_subscribed_topics(template: dict, function_logical_id: str) -> list[str]:
    """Sorted construct ids of the topics the function subscribes to; each subscription must come with an SNS invoke
    permission scoped to that one topic (SourceArn), and no SNS permission may exist without one."""
    function_arn = {"Fn::GetAtt": [function_logical_id, "Arn"]}
    subscribed_topic_logical_ids = sorted(
        subscription["Properties"]["TopicArn"]["Ref"] for subscription in resources_of_type(template, "AWS::SNS::Subscription").values()
        if subscription["Properties"]["Endpoint"] == function_arn and subscription["Properties"]["Protocol"] == "lambda")
    sns_permissions = [permission["Properties"] for permission in resources_of_type(template, "AWS::Lambda::Permission").values()
                       if permission["Properties"]["FunctionName"] == function_arn
                       and permission["Properties"]["Principal"] == "sns.amazonaws.com"]
    assert all("SourceArn" in permission for permission in sns_permissions), function_logical_id
    assert sorted(permission["SourceArn"]["Ref"] for permission in sns_permissions) == subscribed_topic_logical_ids
    return sorted(strip_trailing_logical_id_hash(logical_id) for logical_id in subscribed_topic_logical_ids)


def synthesize_template_with_overrides(mirror_root, context_overrides: dict) -> dict:
    mirror_root.mkdir()
    completed = run_synth_for_tests(mirror_root, context_overrides)
    assert completed.returncode == 0, f"synth failed:\n{completed.stderr[-4000:]}"
    return json.loads((mirror_root / "cdk.out" / "SendPidge.template.json").read_text())


def test_inbound_message_function_reads_both_topics_with_a_dead_letter_queue(synthesized_template):
    inbound_logical_id, inbound_function = find_function_by_handler(synthesized_template, INBOUND_HANDLER)
    verdict_logical_id, _verdict_function = find_function_by_handler(synthesized_template, VERDICT_HANDLER)
    assert list_subscribed_topics(synthesized_template, inbound_logical_id) == sorted(INBOUND_TOPICS_WITHOUT_SMS)
    assert list_subscribed_topics(synthesized_template, verdict_logical_id) == sorted(INBOUND_TOPICS_WITHOUT_SMS)
    assert inbound_function["Properties"]["ReservedConcurrentExecutions"] == 2
    queue_logical_id = find_construct_logical_id(synthesized_template, "AWS::SQS::Queue", "InboundMessageDeadLetterQueue")
    [invoke_config] = [config["Properties"] for config in resources_of_type(synthesized_template, "AWS::Lambda::EventInvokeConfig").values()
                       if config["Properties"]["FunctionName"] == {"Ref": inbound_logical_id}]
    assert invoke_config["MaximumRetryAttempts"] == 2
    assert invoke_config["DestinationConfig"]["OnFailure"]["Destination"] == {"Fn::GetAtt": [queue_logical_id, "Arn"]}
    alarm = find_alarm_properties(synthesized_template, "InboundMessageDeadLetterAlarm")
    assert alarm["Dimensions"] == [{"Name": "QueueName", "Value": {"Fn::GetAtt": [queue_logical_id, "QueueName"]}}]
    assert (alarm["MetricName"], alarm["Threshold"], alarm["ComparisonOperator"]) == (
        "ApproximateNumberOfMessagesVisible", 1, "GreaterThanOrEqualToThreshold")

    environment = inbound_function["Properties"]["Environment"]["Variables"]
    _reader_logical_id, reader_function = find_function_by_handler(synthesized_template, READER_HANDLER)
    reader_environment = reader_function["Properties"]["Environment"]["Variables"]
    assert {"READER_MODEL", "ESCALATION_MODEL"} <= environment.keys()  # lambda/shared/pipeline.py reads both at import
    assert ({name: value for name, value in environment.items() if name not in INBOUND_ONLY_ENVIRONMENT_KEYS}
            == {name: value for name, value in reader_environment.items() if name not in READER_ONLY_ENVIRONMENT_KEYS})
    assert (environment["WHATSAPP_TOPIC_ARN"], environment["RCS_TOPIC_ARN"], environment["SMS_TOPIC_ARN"]) == (
        {"Ref": find_construct_logical_id(synthesized_template, "AWS::SNS::Topic", "WhatsAppInboundTopic")},
        {"Ref": find_construct_logical_id(synthesized_template, "AWS::SNS::Topic", "RcsInboundTopic")},
        "",  # smsEnabled off: pidge-inbound has no SMS parser
    )
    assert environment["MESSENGER_RESIDENT_ADDRESS"] == MESSENGER_RESIDENT_ADDRESS


def test_sms_topic_only_with_sms_enabled(tmp_path):
    template = synthesize_template_with_overrides(tmp_path / "sms_on", SMS_ON_OVERRIDES)
    assert_each_inbound_topic_takes_publishes_only_from_this_account(template, INBOUND_TOPICS)
    sms_two_way_role = template["Resources"][find_construct_logical_id(template, "AWS::IAM::Role", "SmsTwoWayRole")]
    assert [statement["Principal"] for statement in sms_two_way_role["Properties"]["AssumeRolePolicyDocument"]["Statement"]] == [
        {"Service": "sms-voice.amazonaws.com"}]
    inbound_logical_id, inbound_function = find_function_by_handler(template, INBOUND_HANDLER)
    verdict_logical_id, _verdict_function = find_function_by_handler(template, VERDICT_HANDLER)
    assert list_subscribed_topics(template, inbound_logical_id) == sorted(INBOUND_TOPICS)
    assert list_subscribed_topics(template, verdict_logical_id) == sorted(INBOUND_TOPICS_WITHOUT_SMS)  # SMS is pidge-inbound's only
    environment = inbound_function["Properties"]["Environment"]["Variables"]
    assert (environment["SMS_TOPIC_ARN"], environment["SMS_ORIGINATION_NUMBER"]) == (
        {"Ref": find_construct_logical_id(template, "AWS::SNS::Topic", "SmsInboundTopic")}, SMS_ORIGINATION_NUMBER)

    sms_incomplete_root = tmp_path / "sms_incomplete"
    sms_incomplete_root.mkdir()
    refused = run_synth_for_tests(sms_incomplete_root, {"smsEnabled": True})
    assert refused.returncode != 0
    assert SMS_SETTINGS_REFUSAL in refused.stderr
