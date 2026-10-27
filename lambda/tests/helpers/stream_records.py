"""DynamoDB stream records (NEW_AND_OLD_IMAGES, the table's stream view) built from rows the production writers put on moto:
read the row before the write (read_stored_row), run the writer, then stream_record_for_row reads the row as stored now."""

import itertools
from datetime import datetime, timezone

from boto3.dynamodb.types import TypeSerializer

from shared import graph

stream_value_serializer = TypeSerializer()
sequence_numbers = itertools.count(1)


def read_stored_row(key: dict) -> dict | None:
    response = graph.get_table().get_item(Key=key)
    return response["Item"] if "Item" in response else None


def serialize_image(row: dict) -> dict:
    return {name: stream_value_serializer.serialize(value) for name, value in row.items()}


def stream_record_for_row(key: dict, old_row: dict | None, created_at: datetime | None = None) -> dict:
    """The record DynamoDB writes for the row now stored at key: INSERT when old_row is None, else MODIFY with old_row as
    OldImage. Sequence numbers rise across the test run; ApproximateCreationDateTime is created_at, or now."""
    sequence_number = next(sequence_numbers)
    record_time = created_at if created_at is not None else datetime.now(timezone.utc)
    stored_row = read_stored_row(key)
    assert stored_row is not None, "run the production writer before building its record"
    dynamodb = {"Keys": serialize_image(key), "NewImage": serialize_image(stored_row), "SequenceNumber": f"{sequence_number:021d}",
                "ApproximateCreationDateTime": record_time.timestamp(), "StreamViewType": "NEW_AND_OLD_IMAGES"}
    if old_row is not None:
        dynamodb["OldImage"] = serialize_image(old_row)
    return {"eventID": f"event-{sequence_number}", "eventName": "INSERT" if old_row is None else "MODIFY",
            "eventSource": "aws:dynamodb", "dynamodb": dynamodb}


def stream_event(records: list[dict]) -> dict:
    return {"Records": records}
