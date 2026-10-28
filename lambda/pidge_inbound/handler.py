"""SNS invokes this for each record on the WhatsApp, RCS and SMS inbound topics: typed text and media sent to Pidge's numbers
become inbox rows of the one messenger resident (MESSENGER_RESIDENT_ADDRESS) through the same pipeline as email. Taps,
statuses and a waiting contact's six-digit code are pidge-verdict's and give no row. No complete resident profile, a broken
record or an unknown topic raises before anything is written (retried, then InboundMessageDeadLetterQueue)."""

import json
import logging
import os
import re

from shared import circle, inbound_texts, pipeline
from shared.enums import Channel
from shared.inbound_texts import ReceivedText
from shared.profiles import normalize_receiver_address, require_receiver_profile

logger = logging.getLogger()
logger.setLevel(logging.INFO)

WHATSAPP_TOPIC_ARN = os.environ["WHATSAPP_TOPIC_ARN"]
RCS_TOPIC_ARN = os.environ["RCS_TOPIC_ARN"]
SMS_TOPIC_ARN = os.environ["SMS_TOPIC_ARN"]  # "" while SMS is off: then no record is read as SMS
SMS_ORIGINATION_NUMBER = os.environ["SMS_ORIGINATION_NUMBER"]  # Pidge's SMS number; a record to any other raises
MESSENGER_RESIDENT_ADDRESS = normalize_receiver_address(os.environ["MESSENGER_RESIDENT_ADDRESS"])
VERIFICATION_CODE_PATTERN = re.compile(f"[0-9]{{{circle.VERIFICATION_CODE_DIGITS}}}")  # used with fullmatch


def parse_sms_record(sns_message: dict) -> list[ReceivedText]:
    return inbound_texts.parse_sms_record(sns_message, SMS_ORIGINATION_NUMBER)


# The topic picks the parser, never the content; an empty ARN (SMS off) is no topic at all.
PARSERS_BY_TOPIC_ARN = {topic_arn: parser for topic_arn, parser in (
    (WHATSAPP_TOPIC_ARN, inbound_texts.parse_whatsapp_record),
    (RCS_TOPIC_ARN, inbound_texts.parse_rcs_record),
    (SMS_TOPIC_ARN, parse_sms_record),
) if topic_arn}


def handle(event, context) -> None:
    receiver = require_receiver_profile(MESSENGER_RESIDENT_ADDRESS)  # no complete profile raises UnknownReceiverError (counted)
    for received in parse_sns_record(event):
        if is_code_for_a_waiting_number(received):
            logger.info("WhatsApp message %s is a code a contact's number waits on; left to verification", received.provider_message_id)
            continue
        pipeline.run_pipeline(receiver, pipeline.inbound_message_for_text(received))


def parse_sns_record(event: dict) -> list[ReceivedText]:
    """The one record SNS delivers per invocation, parsed whole before any row is written; a topic not in
    PARSERS_BY_TOPIC_ARN raises ValueError."""
    [record] = event["Records"]
    topic_arn = record["Sns"]["TopicArn"]
    if topic_arn not in PARSERS_BY_TOPIC_ARN:
        raise ValueError(f"SNS record from a topic pidge-inbound does not read: {topic_arn}")
    return PARSERS_BY_TOPIC_ARN[topic_arn](json.loads(record["Sns"]["Message"]))


def is_code_for_a_waiting_number(received: ReceivedText) -> bool:
    """A WhatsApp text that is only a six-digit code, from a number some resident's contact row waits on: pidge-verdict
    checks it (10c) and it never becomes an inbox row."""
    return (received.channel is Channel.WHATSAPP and received.text is not None
            and VERIFICATION_CODE_PATTERN.fullmatch(received.text.strip()) is not None
            and len(circle.list_receivers_waiting_on_number(received.sender_address)) > 0)
