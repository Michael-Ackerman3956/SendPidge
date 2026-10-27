"""CDK assertions on the synthesised SendPidge template (no AWS credentials, no ../.env)."""
import fnmatch
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
INFRA_DIRECTORY = REPOSITORY_ROOT / "infra"
SYNTH_TEST_ENTRY = INFRA_DIRECTORY / "test-entry" / "synth-for-tests.ts"
TSX_BINARY = INFRA_DIRECTORY / "node_modules" / ".bin" / "tsx"
# The stack reads these as ../<name> from its working directory; the tmp mirror links them to the real repo.
STACK_SOURCE_DIRECTORIES = ["lambda", "agent", "pwa", "landing", "edge"]
SYNTH_TIMEOUT_SECONDS = 300
STACK_ACCOUNT = "358148451054"  # the account synth-for-tests.ts pins
WEBSOCKET_THROTTLE_RATE_LIMIT = 20
WEBSOCKET_THROTTLE_BURST_LIMIT = 40
BASIC_EXECUTION_MANAGED_POLICY_SUFFIX = "service-role/AWSLambdaBasicExecutionRole"

# Wildcard actions an Allow statement may carry; every other action containing "*" fails.
ALLOWED_WILDCARD_ACTIONS = {
    "s3:GetObject*": "CDK grantRead/grantReadWrite on the email, site and CDK asset buckets",
    "s3:GetBucket*": "CDK grantRead on the same buckets (bucket metadata for the read grant)",
    "s3:List*": "CDK grantRead on the same buckets (prefix listing for the read grant)",
    "s3:DeleteObject*": "BucketDeployment and autoDeleteObjects on the PWA and landing buckets",
    "s3:Abort*": "CDK grantPut: Mail Manager's write role and BucketDeployment abort multipart uploads",
}

TABLE_READ_WRITE_ACTIONS = frozenset({
    "dynamodb:BatchGetItem", "dynamodb:BatchWriteItem", "dynamodb:ConditionCheckItem", "dynamodb:DeleteItem", "dynamodb:DescribeTable",
    "dynamodb:GetItem", "dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:PutItem", "dynamodb:Query", "dynamodb:Scan",
    "dynamodb:UpdateItem",
})
BUCKET_READ_ACTIONS = frozenset({"s3:GetBucket*", "s3:GetObject*", "s3:List*"})
BUCKET_PUT_ACTIONS = frozenset({
    "s3:Abort*", "s3:PutObject", "s3:PutObjectLegalHold", "s3:PutObjectRetention", "s3:PutObjectTagging", "s3:PutObjectVersionTagging",
})
SITE_DEPLOY_ACTIONS = BUCKET_READ_ACTIONS | BUCKET_PUT_ACTIONS | {"s3:DeleteObject*"}
AUTO_DELETE_ACTIONS = frozenset({"s3:PutBucketPolicy", "s3:GetBucket*", "s3:List*", "s3:DeleteObject*"})
MODEL_INVOKE_ACTIONS = frozenset({"bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"})
SCHEDULE_WRITE_ACTIONS = frozenset({"scheduler:CreateSchedule", "scheduler:UpdateSchedule"})

# Owners: an identity policy's role (logical id, hash stripped) or a resource policy's service principal.
BUCKET_DEPLOYMENT_ROLE = "CustomCDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756CServiceRole"
AUTO_DELETE_ROLE = "CustomS3AutoDeleteObjectsCustomResourceProviderRole"
CASE_AGENT_RUNTIME_ROLE = "CaseAgentRuntimeExecutionRole"
MAIL_MANAGER_WRITE_ROLE = "MailManagerWriteRole"
CLOUDFRONT_SERVICE = "cloudfront.amazonaws.com"
TABLE_ROLES = (
    "WebSocketConnectFunctionServiceRole", "RetryFunctionServiceRole", "CaseFollowUpFunctionServiceRole", "CaseInboundFunctionServiceRole",
    "QuestionInboundFunctionServiceRole", "ReaderFunctionServiceRole", "ReleaseFunctionServiceRole", "VerdictFunctionServiceRole",
    "AskLinkFunctionServiceRole", "DispatchFunctionServiceRole",
)
SOCKET_PUSH_ROLES = tuple(role for role in TABLE_ROLES if role not in ("WebSocketConnectFunctionServiceRole", "QuestionInboundFunctionServiceRole"))
SCHEDULE_WRITING_ROLES = ("RetryFunctionServiceRole", "ReaderFunctionServiceRole", "VerdictFunctionServiceRole")
MODEL_CALLING_ROLES = ("ReaderFunctionServiceRole", CASE_AGENT_RUNTIME_ROLE, "DispatchFunctionServiceRole")
MODEL_RESOURCES = (
    "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5*",  # region *: the US and global profiles route anywhere
    "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-5-5*",
    "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-4-5*",
    "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-5-5*",
    f"arn:aws:bedrock:us-east-1:{STACK_ACCOUNT}:inference-profile/us.anthropic.claude-haiku-4-5*",
    f"arn:aws:bedrock:us-east-1:{STACK_ACCOUNT}:inference-profile/global.anthropic.claude-haiku-5-5*",
    f"arn:aws:bedrock:us-east-1:{STACK_ACCOUNT}:inference-profile/global.anthropic.claude-opus-4-5*",
    f"arn:aws:bedrock:us-east-1:{STACK_ACCOUNT}:inference-profile/global.anthropic.claude-opus-5-5*",
)
AGENTCORE_LOG_GROUPS = f"arn:${{AWS::Partition}}:logs:us-east-1:{STACK_ACCOUNT}:log-group:/aws/bedrock-agentcore/runtimes/*"


def grant_to_each(roles: tuple[str, ...], actions: frozenset[str]) -> dict[str, frozenset[str]]:
    return {role: actions for role in roles}


# Every Allow on a wildcard resource (logical-id hashes stripped): {owner: the actions that owner may hold there}.
ALLOWED_WILDCARD_GRANTS: dict[str, dict[str, frozenset[str]]] = {
    "${EmailBucket.Arn}/*": {"ReaderFunctionServiceRole": BUCKET_READ_ACTIONS},  # the Reader reads every stored email
    "${EmailBucket.Arn}/inbox/*": {MAIL_MANAGER_WRITE_ROLE: BUCKET_PUT_ACTIONS,
                                   "VerdictFunctionServiceRole": frozenset({"s3:PutObjectTagging"})},  # quarantine tag
    "${EmailBucket.Arn}/cases/*": {MAIL_MANAGER_WRITE_ROLE: BUCKET_PUT_ACTIONS, "CaseInboundFunctionServiceRole": BUCKET_READ_ACTIONS},
    "${EmailBucket.Arn}/pidge/*": {MAIL_MANAGER_WRITE_ROLE: BUCKET_PUT_ACTIONS, "QuestionInboundFunctionServiceRole": BUCKET_READ_ACTIONS},
    "${EmailBucket.Arn}/asks/*": {MAIL_MANAGER_WRITE_ROLE: BUCKET_PUT_ACTIONS, "VerdictFunctionServiceRole": BUCKET_READ_ACTIONS},
    "${PwaBucket.Arn}/*": {BUCKET_DEPLOYMENT_ROLE: SITE_DEPLOY_ACTIONS, AUTO_DELETE_ROLE: AUTO_DELETE_ACTIONS,
                           CLOUDFRONT_SERVICE: frozenset({"s3:GetObject"})},
    "${LandingBucket.Arn}/*": {BUCKET_DEPLOYMENT_ROLE: SITE_DEPLOY_ACTIONS, AUTO_DELETE_ROLE: AUTO_DELETE_ACTIONS,
                               CLOUDFRONT_SERVICE: frozenset({"s3:GetObject"})},
    f"arn:${{AWS::Partition}}:s3:::cdk-hnb659fds-assets-{STACK_ACCOUNT}-us-east-1/*": {
        BUCKET_DEPLOYMENT_ROLE: BUCKET_READ_ACTIONS, CASE_AGENT_RUNTIME_ROLE: BUCKET_READ_ACTIONS},  # read at deploy
    "${Table.Arn}/index/*": grant_to_each(TABLE_ROLES, TABLE_READ_WRITE_ACTIONS),  # the table grant covers the UserIndex GSI
    "${RetryFunction.Arn}:*": {"SchedulerRole": frozenset({"lambda:InvokeFunction"})},  # any version
    "${CaseFollowUpFunction.Arn}:*": {"SchedulerRole": frozenset({"lambda:InvokeFunction"})},
    "${ReleaseFunction.Arn}:*": {"SchedulerRole": frozenset({"lambda:InvokeFunction"})},
    "${CaseAgentRuntime.AgentRuntimeArn}/*": {"DispatchFunctionServiceRole": frozenset({
        "bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:InvokeAgentRuntimeForUser"})},
    f"arn:${{AWS::Partition}}:execute-api:us-east-1:{STACK_ACCOUNT}:${{WebSocketApi}}/live/*/@connections/*":
        grant_to_each(SOCKET_PUSH_ROLES, frozenset({"execute-api:ManageConnections"})),
    f"arn:${{AWS::Partition}}:scheduler:us-east-1:{STACK_ACCOUNT}:schedule/SendPidge-ScheduleGroup-4E8C1034/*":
        grant_to_each(SCHEDULE_WRITING_ROLES, SCHEDULE_WRITE_ACTIONS),  # one-time schedules inside the one group
    f"arn:${{AWS::Partition}}:logs:us-east-1:{STACK_ACCOUNT}:log-group:*": {CASE_AGENT_RUNTIME_ROLE: frozenset({"logs:DescribeLogGroups"})},
    AGENTCORE_LOG_GROUPS: {CASE_AGENT_RUNTIME_ROLE: frozenset({"logs:CreateLogGroup", "logs:DescribeLogStreams"})},
    AGENTCORE_LOG_GROUPS + ":log-stream:*": {CASE_AGENT_RUNTIME_ROLE: frozenset({"logs:CreateLogStream", "logs:PutLogEvents"})},
    f"arn:${{AWS::Partition}}:bedrock-agentcore:us-east-1:{STACK_ACCOUNT}:workload-identity-directory/default/workload-identity/*": {
        CASE_AGENT_RUNTIME_ROLE: frozenset({"bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                                            "bedrock-agentcore:GetWorkloadAccessTokenForUserId"})},
    **{model_resource: grant_to_each(MODEL_CALLING_ROLES, MODEL_INVOKE_ACTIONS) for model_resource in MODEL_RESOURCES},
    "*": {  # services with no resource-level scoping for these actions
        BUCKET_DEPLOYMENT_ROLE: frozenset({"cloudfront:CreateInvalidation", "cloudfront:GetInvalidation"}),
        CASE_AGENT_RUNTIME_ROLE: frozenset({"xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules",
                                            "xray:GetSamplingTargets", "cloudwatch:PutMetricData"}),
        "DispatchFunctionServiceRole": frozenset({"dynamodb:ListStreams"}),  # its stream event source
    },
}

# Every Mail Manager rule that stores mail: {rule name: (its one recipient, its S3 prefix)}.
EXPECTED_STORE_RULES = {
    "store-case-reply": ("cases@sendpidge.com", "cases/"),
    "store-ask-reply": ("pidge@sendpidge.com", "asks/"),
    "store-pidge-question": ("pidge@sendpidge.com", "pidge/"),
    "store-raw-email": ("grandma@sendpidge.com", "inbox/"),
}

IDENTITY_POLICY_RESOURCE_TYPES = {"AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"}
POLICY_DOCUMENT_RESOURCE_TYPES = {"AWS::S3::BucketPolicy", "AWS::SNS::TopicPolicy", "AWS::SQS::QueuePolicy"}
LOGICAL_ID_HASH_PATTERN = re.compile(r"\$\{([A-Za-z0-9]+?)[0-9A-F]{8}(\.[A-Za-z]+)?\}")
TRAILING_LOGICAL_ID_HASH_PATTERN = re.compile(r"[0-9A-F]{8}$")
# Points every AWS SDK lookup at nothing, so synth cannot reach real credentials even by accident.
CREDENTIAL_FREE_ENVIRONMENT = {"AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull, "AWS_EC2_METADATA_DISABLED": "true"}


def build_synth_working_directory(mirror_root: Path) -> Path:
    """Tmp mirror: ../<source> links to the repo, build/ holds stand-ins; never infra/build/."""
    for directory_name in STACK_SOURCE_DIRECTORIES:
        (mirror_root / directory_name).symlink_to(REPOSITORY_ROOT / directory_name, target_is_directory=True)
    working_directory = mirror_root / "infra"
    stand_in_layer = working_directory / "build" / "layer" / "python"
    stand_in_layer.mkdir(parents=True)
    (stand_in_layer / "stand_in.txt").write_text("test stand-in layer; never deployed\n")
    stand_in_case_agent_shared = working_directory / "build" / "case_agent" / "shared"
    stand_in_case_agent_shared.mkdir(parents=True)
    shutil.copy(REPOSITORY_ROOT / "agent" / "pidge_case" / "main.py", working_directory / "build" / "case_agent" / "main.py")
    for shared_source_file in (REPOSITORY_ROOT / "lambda" / "shared").glob("*.py"):
        shutil.copy(shared_source_file, stand_in_case_agent_shared / shared_source_file.name)
    return working_directory


@pytest.fixture(scope="session")
def synthesized_template(tmp_path_factory) -> dict:
    mirror_root = tmp_path_factory.mktemp("synth_mirror")
    working_directory = build_synth_working_directory(mirror_root)
    output_directory = mirror_root / "cdk.out"
    environment_without_credentials = {
        name: value for name, value in os.environ.items() if not name.startswith(("AWS_", "CDK_"))
    } | CREDENTIAL_FREE_ENVIRONMENT
    completed = subprocess.run(
        [str(TSX_BINARY), str(SYNTH_TEST_ENTRY), str(output_directory)],
        cwd=working_directory, env=environment_without_credentials,
        capture_output=True, text=True, timeout=SYNTH_TIMEOUT_SECONDS, check=False,
    )
    assert completed.returncode == 0, f"synth failed:\n{completed.stderr[-4000:]}"
    return json.loads((output_directory / "SendPidge.template.json").read_text())


def render_intrinsic_as_text(value) -> str:
    if isinstance(value, str):
        return value
    if "Fn::Join" in value:
        return "".join(render_intrinsic_as_text(part) for part in value["Fn::Join"][1])
    if "Fn::Sub" in value and isinstance(value["Fn::Sub"], str):
        return value["Fn::Sub"]
    if "Ref" in value:
        return "${%s}" % value["Ref"]
    if "Fn::GetAtt" in value:
        return "${%s}" % ".".join(value["Fn::GetAtt"])
    return json.dumps(value, sort_keys=True)


def strip_logical_id_hashes(rendered_text: str) -> str:
    return LOGICAL_ID_HASH_PATTERN.sub(lambda match: "${" + match.group(1) + (match.group(2) or "") + "}", rendered_text)


def strip_trailing_logical_id_hash(logical_id: str) -> str:
    return TRAILING_LOGICAL_ID_HASH_PATTERN.sub("", logical_id)


def as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def resources_of_type(template: dict, resource_type: str) -> dict:
    return {logical_id: resource for logical_id, resource in template["Resources"].items() if resource["Type"] == resource_type}


def describe_resource_policy_owner(principal) -> str:
    """A resource-policy statement's owner: a role (hash stripped), one service principal, or "*" for anyone."""
    if principal in ("*", {"AWS": "*"}):
        return "*"
    if "Service" in principal:
        assert isinstance(principal["Service"], str), principal
        return principal["Service"]
    return strip_trailing_logical_id_hash(principal["AWS"]["Fn::GetAtt"][0])


def iterate_owned_policy_statements(template: dict):
    """Yields (owner, statement) for every policy document: identity policies by the role they attach to, resource policies by principal."""
    for logical_id, resource in template["Resources"].items():
        properties = resource.get("Properties", {})
        if resource["Type"] in IDENTITY_POLICY_RESOURCE_TYPES:
            assert not properties.get("Users") and not properties.get("Groups"), logical_id
            for role_reference in properties.get("Roles", []):
                for statement in properties["PolicyDocument"]["Statement"]:
                    yield strip_trailing_logical_id_hash(role_reference["Ref"]), statement
        elif resource["Type"] == "AWS::IAM::Role":
            for inline_policy in properties.get("Policies", []):
                for statement in inline_policy["PolicyDocument"]["Statement"]:
                    yield strip_trailing_logical_id_hash(logical_id), statement
        elif resource["Type"] in POLICY_DOCUMENT_RESOURCE_TYPES:
            for statement in properties["PolicyDocument"]["Statement"]:
                yield describe_resource_policy_owner(statement["Principal"]), statement


def find_function_by_handler(template: dict, handler: str) -> tuple[str, dict]:
    matches = [(logical_id, function) for logical_id, function in resources_of_type(template, "AWS::Lambda::Function").items()
               if function["Properties"].get("Handler") == handler]
    assert len(matches) == 1, f"expected one function with handler {handler}, found {len(matches)}"
    return matches[0]


def collect_role_allow_statements(template: dict, role_logical_id: str) -> list[dict]:
    """Every Allow statement the role carries: its inline policies and the identity policies attached to it."""
    role_owner = strip_trailing_logical_id_hash(role_logical_id)
    return [statement for owner, statement in iterate_owned_policy_statements(template)
            if owner == role_owner and statement["Effect"] == "Allow" and "Principal" not in statement]


def statement_grants(statement: dict, action: str) -> bool:
    return any(fnmatch.fnmatchcase(action.lower(), action_pattern.lower()) for action_pattern in as_list(statement["Action"]))


def has_source_account_condition(statement: dict) -> bool:
    return statement.get("Condition", {}).get("StringEquals", {}).get("aws:SourceAccount") == STACK_ACCOUNT


def test_no_wildcard_action_or_resource_outside_the_allow_list(synthesized_template):
    violations = []
    for owner, statement in iterate_owned_policy_statements(synthesized_template):
        if {"NotAction", "NotResource", "NotPrincipal"} & statement.keys():
            violations.append(f"{owner}: NotAction/NotResource/NotPrincipal")
            continue
        if statement["Effect"] != "Allow":
            continue
        actions = as_list(statement["Action"])
        if owner == "*":
            violations.append(f"an Allow to any principal: {actions}")
        violations += [f"{owner}: action {action}" for action in actions if "*" in action and action not in ALLOWED_WILDCARD_ACTIONS]
        for resource in as_list(statement["Resource"]):
            rendered_resource = strip_logical_id_hashes(render_intrinsic_as_text(resource))
            if "*" not in rendered_resource:
                continue
            allowed_actions = ALLOWED_WILDCARD_GRANTS.get(rendered_resource, {}).get(owner, frozenset())
            violations += [f"{owner}: {action} on {rendered_resource}" for action in actions if action not in allowed_actions]
    assert violations == []


def test_put_metric_data_is_limited_to_the_agentcore_namespace(synthesized_template):
    statements = [statement for _owner, statement in iterate_owned_policy_statements(synthesized_template)
                  if statement["Effect"] == "Allow" and statement_grants(statement, "cloudwatch:PutMetricData")]
    assert statements
    assert all(statement.get("Condition") == {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}} for statement in statements)


def test_every_role_carries_only_the_basic_execution_managed_policy(synthesized_template):
    for role_logical_id, role in resources_of_type(synthesized_template, "AWS::IAM::Role").items():
        managed_policy_names = [render_intrinsic_as_text(arn) for arn in role["Properties"].get("ManagedPolicyArns", [])]
        assert all(name.endswith(BASIC_EXECUTION_MANAGED_POLICY_SUFFIX) for name in managed_policy_names), (role_logical_id, managed_policy_names)


def test_only_the_ask_link_function_url_is_open_to_anyone(synthesized_template):
    public_permissions = [permission["Properties"] for permission in resources_of_type(synthesized_template, "AWS::Lambda::Permission").values()
                          if permission["Properties"]["Principal"] == "*"]
    assert {strip_trailing_logical_id_hash(permission["FunctionName"]["Fn::GetAtt"][0]) for permission in public_permissions} == {"AskLinkFunction"}
    assert all(permission.get("FunctionUrlAuthType") == "NONE" or permission.get("InvokedViaFunctionUrl") is True
               for permission in public_permissions)


def test_every_function_on_the_lambda_asset_has_its_handler_module(synthesized_template):
    _reader_logical_id, reader_function = find_function_by_handler(synthesized_template, "pidge_reader.handler.handle")
    lambda_asset_code = reader_function["Properties"]["Code"]
    handlers = [function["Properties"]["Handler"] for function in resources_of_type(synthesized_template, "AWS::Lambda::Function").values()
                if function["Properties"]["Code"] == lambda_asset_code]
    assert len(handlers) > 1
    missing_modules = [handler for handler in handlers
                       if not (REPOSITORY_ROOT / "lambda" / (handler.rsplit(".", 1)[0].replace(".", "/") + ".py")).is_file()]
    assert missing_modules == []


def test_every_bucket_blocks_public_access_and_denies_non_tls(synthesized_template):
    buckets = resources_of_type(synthesized_template, "AWS::S3::Bucket")
    bucket_policies = resources_of_type(synthesized_template, "AWS::S3::BucketPolicy").values()
    assert buckets
    for bucket_logical_id, bucket in buckets.items():
        assert bucket["Properties"].get("PublicAccessBlockConfiguration") == {
            "BlockPublicAcls": True, "BlockPublicPolicy": True, "IgnorePublicAcls": True, "RestrictPublicBuckets": True,
        }, bucket_logical_id
        statements = [statement for policy in bucket_policies if policy["Properties"]["Bucket"] == {"Ref": bucket_logical_id}
                      for statement in policy["Properties"]["PolicyDocument"]["Statement"]]
        bucket_name = bucket_logical_id[:-8]
        assert any(
            statement["Effect"] == "Deny" and "s3:*" in as_list(statement["Action"])
            and statement.get("Principal") == {"AWS": "*"}
            and statement.get("Condition") == {"Bool": {"aws:SecureTransport": "false"}}
            and {strip_logical_id_hashes(render_intrinsic_as_text(resource)) for resource in as_list(statement["Resource"])}
            == {"${%s.Arn}" % bucket_name, "${%s.Arn}/*" % bucket_name}
            for statement in statements
        ), f"{bucket_logical_id} has no non-TLS deny on the bucket and its objects"


def test_websocket_stage_is_throttled(synthesized_template):
    stages = resources_of_type(synthesized_template, "AWS::ApiGatewayV2::Stage")
    assert len(stages) == 1
    route_settings = next(iter(stages.values()))["Properties"]["DefaultRouteSettings"]
    assert (route_settings["ThrottlingRateLimit"], route_settings["ThrottlingBurstLimit"]) == (
        WEBSOCKET_THROTTLE_RATE_LIMIT, WEBSOCKET_THROTTLE_BURST_LIMIT)


def test_each_inbound_topic_takes_publishes_only_from_this_account(synthesized_template):
    """WhatsApp publishes through the topic policy; RCS assumes RcsTwoWayRole, so that role's trust policy carries the guard."""
    topics = resources_of_type(synthesized_template, "AWS::SNS::Topic")
    topic_policies = resources_of_type(synthesized_template, "AWS::SNS::TopicPolicy").values()
    roles = resources_of_type(synthesized_template, "AWS::IAM::Role")
    assert len(topics) == 2
    for topic_logical_id in topics:
        topic_policy_statements = [statement for policy in topic_policies if {"Ref": topic_logical_id} in policy["Properties"]["Topics"]
                                   for statement in policy["Properties"]["PolicyDocument"]["Statement"] if statement["Effect"] == "Allow"]
        publishing_role_trust_statements = [
            trust_statement for role_logical_id, role in roles.items()
            if any(statement_grants(statement, "sns:Publish") and {"Ref": topic_logical_id} in as_list(statement["Resource"])
                   for statement in collect_role_allow_statements(synthesized_template, role_logical_id))
            for trust_statement in role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        ]
        publisher_statements = topic_policy_statements + publishing_role_trust_statements
        assert publisher_statements, f"{topic_logical_id} has no publisher"
        assert all(has_source_account_condition(statement) for statement in publisher_statements), topic_logical_id


def test_verdict_role_reads_only_ask_replies_and_has_no_bedrock(synthesized_template):
    """pidge-verdict reads ask-by-email replies under asks/, never inbox/ or any other object, and never calls a model."""
    _logical_id, verdict_function = find_function_by_handler(synthesized_template, "pidge_verdict.handler.handle")
    role_logical_id = verdict_function["Properties"]["Role"]["Fn::GetAtt"][0]
    statements = collect_role_allow_statements(synthesized_template, role_logical_id)
    assert statements
    object_read_resources = {strip_logical_id_hashes(render_intrinsic_as_text(resource))
                             for statement in statements if statement_grants(statement, "s3:GetObject")
                             for resource in as_list(statement["Resource"])}
    assert object_read_resources == {"${EmailBucket.Arn}", "${EmailBucket.Arn}/asks/*"}
    assert [statement for statement in statements if statement_grants(statement, "bedrock:InvokeModel")
            or any(action.lower().startswith("bedrock:") for action in as_list(statement["Action"]))] == []


def test_reader_has_a_dead_letter_queue_and_reserved_concurrency(synthesized_template):
    reader_logical_id, reader_function = find_function_by_handler(synthesized_template, "pidge_reader.handler.handle")
    assert reader_function["Properties"].get("ReservedConcurrentExecutions", 0) > 0
    queues = resources_of_type(synthesized_template, "AWS::SQS::Queue")
    failure_destinations = [
        config["Properties"]["DestinationConfig"]["OnFailure"]["Destination"]
        for config in resources_of_type(synthesized_template, "AWS::Lambda::EventInvokeConfig").values()
        if config["Properties"]["FunctionName"] == {"Ref": reader_logical_id}
    ]
    dead_letter_target = reader_function["Properties"].get("DeadLetterConfig", {}).get("TargetArn")
    queue_targets = [target for target in failure_destinations + [dead_letter_target]
                     if isinstance(target, dict) and target.get("Fn::GetAtt", [None])[0] in queues]
    assert queue_targets, "pidge-reader has no SQS failure destination or dead-letter queue"


def test_store_rules_match_only_their_own_recipient(synthesized_template):
    """Every rule that writes to S3, whatever its name: one RECIPIENT EQUALS condition and the prefix its readers expect."""
    rule_sets = resources_of_type(synthesized_template, "AWS::SES::MailManagerRuleSet")
    assert len(rule_sets) == 1
    stored_by_rule = {}
    for rule in next(iter(rule_sets.values()))["Properties"]["Rules"]:
        write_actions = [action["WriteToS3"] for action in rule["Actions"] if "WriteToS3" in action]
        if not write_actions:
            continue
        assert len(write_actions) == 1, rule["Name"]
        recipient_conditions = [condition["StringExpression"] for condition in rule.get("Conditions", [])
                                if condition.get("StringExpression", {}).get("Evaluate") == {"Attribute": "RECIPIENT"}]
        assert len(recipient_conditions) == 1 and recipient_conditions[0]["Operator"] == "EQUALS", rule["Name"]
        assert len(recipient_conditions[0]["Values"]) == 1, rule["Name"]
        stored_by_rule[rule["Name"]] = (recipient_conditions[0]["Values"][0], write_actions[0]["S3Prefix"])
    assert stored_by_rule == EXPECTED_STORE_RULES
