"""One-time EventBridge Scheduler invocations: the release timer, the re-ask and the bank follow-up."""

import json
import logging
import os
from datetime import datetime

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_scheduler_client = None
SCHEDULE_NAME_MAX_LENGTH = 64
RE_ASK_SCHEDULE_FAILED_METRIC_NAME = "ReAskScheduleFailed"  # pipeline.schedule_re_ask counts a first re-ask it could not book


def get_scheduler_client():
    global _scheduler_client
    if _scheduler_client is None:
        _scheduler_client = boto3.client("scheduler")
    return _scheduler_client


def schedule_one_time_invocation(schedule_name: str, fire_at: datetime, target_function_arn: str, payload: dict) -> None:
    """Creates a schedule that runs once at fire_at (UTC) and deletes itself. A name clash (same request twice) is logged, not raised."""
    try:
        get_scheduler_client().create_schedule(
            Name=schedule_name[:SCHEDULE_NAME_MAX_LENGTH],
            GroupName=os.environ["SCHEDULE_GROUP_NAME"],
            ScheduleExpression=f"at({fire_at.strftime('%Y-%m-%dT%H:%M:%S')})",
            ScheduleExpressionTimezone="UTC",
            FlexibleTimeWindow={"Mode": "OFF"},
            ActionAfterCompletion="DELETE",
            Target={"Arn": target_function_arn, "RoleArn": os.environ["SCHEDULER_ROLE_ARN"], "Input": json.dumps(payload)},
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConflictException":
            logger.info("schedule %s already exists", schedule_name)
            return
        raise
