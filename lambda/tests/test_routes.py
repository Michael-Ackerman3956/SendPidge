"""Every ClientAction the PWA may send has exactly one route: pidge-verdict's $default, or pidge-org's own route for an org
action. A new member without one fails here, not as a bad_message on the live socket."""

from pidge_org import handler as org_actions_handler
from shared.enums import ClientAction
from tests.test_verdict import aws  # noqa: F401
from tests.test_verify_contact import verdict_handler_without_profiles  # noqa: F401


def test_every_client_action_has_a_route(verdict_handler_without_profiles):  # noqa: F811
    handler, _pushed_messages = verdict_handler_without_profiles
    org_actions = frozenset(org_actions_handler.ORG_ACTION_HANDLERS)
    assert set(ClientAction) == handler.ROUTED_ACTIONS | org_actions
    assert handler.ROUTED_ACTIONS.isdisjoint(org_actions)
