"""$connect checks the per-user access token; $disconnect forgets the connection."""

import logging
from enum import StrEnum

from shared import graph

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class WebSocketRoute(StrEnum):
    CONNECT = "$connect"
    DISCONNECT = "$disconnect"


ACCEPTED = {"statusCode": 200}
REJECTED = {"statusCode": 401}


def handle(event, context):
    request_context = event["requestContext"]
    route = WebSocketRoute(request_context["routeKey"])
    connection_id = request_context["connectionId"]
    if route is WebSocketRoute.DISCONNECT:
        graph.remove_connection(connection_id)
        return ACCEPTED
    access_token = (event.get("queryStringParameters") or {}).get("token")
    user_id = graph.find_user_id_for_access_token(access_token) if access_token else None
    if user_id is None:
        logger.warning("rejected connection %s: missing or unknown token", connection_id)
        return REJECTED
    graph.register_connection(connection_id, user_id)  # userId comes from the token row, never the client
    logger.info("connected %s as %s", connection_id, user_id)
    return ACCEPTED
