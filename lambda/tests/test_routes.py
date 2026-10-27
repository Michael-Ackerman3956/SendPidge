"""Every ClientAction the PWA may send has a route in pidge-verdict: a new member without one fails here, not as a
bad_message on the live socket."""

from shared.enums import ClientAction
from tests.test_verdict import aws  # noqa: F401
from tests.test_verify_contact import verdict_handler_without_profiles  # noqa: F401


def test_every_client_action_has_a_route(verdict_handler_without_profiles):  # noqa: F811
    handler, _pushed_messages = verdict_handler_without_profiles
    assert set(ClientAction) == handler.ROUTED_ACTIONS
