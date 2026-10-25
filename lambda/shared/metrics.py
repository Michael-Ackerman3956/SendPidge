"""CloudWatch embedded metric format on stdout: one count, with one optional dimension. No client, no permissions, no layer."""

import json
import time

METRIC_NAMESPACE = "SendPidge"


def emit_count_metric(metric_name: str, dimension_name: str | None = None, dimension_value: str | None = None) -> None:
    record = {
        "_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [
            {"Namespace": METRIC_NAMESPACE, "Dimensions": [[dimension_name] if dimension_name else []],
             "Metrics": [{"Name": metric_name, "Unit": "Count"}]}]},
        metric_name: 1,
    }
    if dimension_name:
        record[dimension_name] = dimension_value
    print(json.dumps(record))
