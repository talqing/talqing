"""Multi-provider text messaging: inbound, outbound, Kafka turns, SSE contracts.

Import from the package or submodules::

    from services.messaging import TextTurnJob, publish_turn, TurnPayload
    from services.messaging import inbound, delivery, textq, typing_indicator
"""

from __future__ import annotations

from services.messaging.models import TurnPayload, TurnStatus
from services.messaging.textq import TextTurnJob, TextTurnResult, close, publish_turn
from services.messaging.turn_state import record_turn_status, turn_error_text

__all__ = [
    "TextTurnJob",
    "TextTurnResult",
    "TurnPayload",
    "TurnStatus",
    "close",
    "publish_turn",
    "record_turn_status",
    "turn_error_text",
]
