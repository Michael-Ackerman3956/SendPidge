"""Every ClientAction the PWA may send has exactly one route: pidge-verdict's $default, pidge-org's own route for an org
action, or pidge-send's send_message route. A new member without one fails here, not as a bad_message on the live socket."""

import importlib
import itertools

from pidge_org import handler as org_actions_handler
from shared.enums import ClientAction
from tests.test_stack import render_intrinsic_as_text, resources_of_type
from tests.test_verdict import aws  # noqa: F401
from tests.test_verify_contact import verdict_handler_without_profiles  # noqa: F401


def route_keys_on_integration(template: dict, integration_name: str) -> set[str]:
    [integration_logical_id] = [logical_id for logical_id in resources_of_type(template, "AWS::ApiGatewayV2::Integration")
                                if integration_name in logical_id]
    return {route["Properties"]["RouteKey"] for route in resources_of_type(template, "AWS::ApiGatewayV2::Route").values()
            if render_intrinsic_as_text(route["Properties"]["Target"]) == "integrations/${%s}" % integration_logical_id}


def test_every_client_action_has_a_route(verdict_handler_without_profiles, synthesized_template):  # noqa: F811
    handler, _pushed_messages = verdict_handler_without_profiles
    send_actions_handler = importlib.import_module("pidge_send.handler")  # after the fixture's env, like the verdict handler
    send_actions = frozenset(send_actions_handler.SEND_ACTION_HANDLERS)
    routed_action_sets = (handler.ROUTED_ACTIONS, frozenset(org_actions_handler.ORG_ACTION_HANDLERS), send_actions)
    assert set(ClientAction) == frozenset().union(*routed_action_sets)
    assert [pair for pair in itertools.combinations(routed_action_sets, 2) if not pair[0].isdisjoint(pair[1])] == []
    assert route_keys_on_integration(synthesized_template, "SendMessageIntegration") == set(send_actions)
