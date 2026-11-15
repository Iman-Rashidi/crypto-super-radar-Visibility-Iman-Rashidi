"""Public event-store interface assembled from isolated storage mixins."""
from .event_store_common import (
    IdempotencyViolationError, EventChainIntegrityError, ConcurrentStateChangeError,
)
from .event_store_storage import EventStoreStorage
from .event_store_registration import EventStoreRegistration
from .event_store_transitions import EventStoreTransitions
from .event_store_recovery import EventStoreRecovery


class PersistentEventStore(
    EventStoreRegistration, EventStoreTransitions, EventStoreRecovery, EventStoreStorage
):
    """Durable SQLite event store for immutable trade intents and append-only events.

    This module is exchange-agnostic and performs no external request or order action.
    """


__all__ = [
    "PersistentEventStore", "IdempotencyViolationError", "EventChainIntegrityError",
    "ConcurrentStateChangeError",
]
