"""Webhooks service — event vocabulary, delivery, and tenant config CRUD.

Import what you need from here::

    from services import webhooks
    from services.webhooks import events

    await webhooks.dispatch(tenant, events.SESSION_COMPLETED, agent_id, data)
    await webhooks.create_webhook(body, ctx)

Submodules are package-internal; external callers should not import them.
"""

from __future__ import annotations

from .delivery import (  # noqa: F401
    BOUNDED_DISPATCH_TIMEOUT,
    DELIVERY_TIMEOUT,
    deliver_one,
    dispatch,
    dispatch_bounded,
    load_subscriptions,
    sign,
)
from .payloads import build_session_completed  # noqa: F401
from .service import (  # noqa: F401
    CreateWebhookRequest,
    DeliveryResponse,
    DeliveryResultResponse,
    EventTypesResponse,
    PatchWebhookRequest,
    WebhookResponse,
    create_webhook,
    delete_webhook,
    event_types,
    list_webhooks,
    patch_webhook,
    rotate_webhook_secret,
    test_webhook,
    webhook_deliveries,
)
