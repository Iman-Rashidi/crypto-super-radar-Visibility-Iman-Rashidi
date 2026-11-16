from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from crypto_super_radar.persistent_event_store import (
    IdempotencyViolationError,
    PersistentEventStore,
)
from scripts.verify_evaluation_protocol import (
    ProtocolIntegrityError,
    ProtocolNotReadyError,
    verify_evaluation_protocol,
    verify_protocol_hash,
)


@pytest.fixture
def event_db_path(tmp_path: Path) -> Path:
    return tmp_path / "gate-a-events.sqlite3"


@pytest.fixture
def event_store(event_db_path: Path):
    store = PersistentEventStore(event_db_path)
    store.register_or_fetch_intent(
        intent_id="intent-gate-a",
        client_order_id="client-gate-a",
        payload={"symbol": "BTC-USDT", "side": "BUY", "quantity": "0.01"},
        timestamp_utc="2026-11-15T10:00:00.000Z",
    )
    yield store
    store.close()


def append_transition(
    store: PersistentEventStore,
    *,
    event_id: str,
    to_state: str = "VALIDATED",
    event_type: str = "STATE_TRANSITION",
    timestamp_utc: str = "2026-11-15T10:00:01.000Z",
):
    intent = store.get_intent("intent-gate-a")
    assert intent is not None
    return store.commit_transition_atomic(
        intent_id="intent-gate-a",
        client_order_id="client-gate-a",
        payload_hash=intent["payload_hash"],
        to_state=to_state,
        event_type=event_type,
        event_id=event_id,
        timestamp_utc=timestamp_utc,
    )


def add_three_event_chain(store: PersistentEventStore) -> None:
    append_transition(
        store,
        event_id="evt-validated",
        to_state="VALIDATED",
        timestamp_utc="2026-11-15T10:00:01.000Z",
    )
    append_transition(
        store,
        event_id="evt-submitting",
        to_state="SUBMITTING",
        timestamp_utc="2026-11-15T10:00:02.000Z",
    )


def test_event_id_is_unique_and_conflicting_reuse_is_rejected(
    event_store: PersistentEventStore,
) -> None:
    original = append_transition(
        event_store,
        event_id="evt-idempotent",
        to_state="VALIDATED",
        timestamp_utc="2026-11-15T10:00:01.000Z",
    )
    replay = append_transition(
        event_store,
        event_id="evt-idempotent",
        to_state="VALIDATED",
        timestamp_utc="2026-11-15T10:00:01.000Z",
    )
    assert original["event_id"] == replay["event_id"]
    assert replay["inserted"] is False
    assert len(event_store.list_events("intent-gate-a")) == 2

    with pytest.raises(IdempotencyViolationError):
        append_transition(
            event_store,
            event_id="evt-idempotent",
            to_state="REJECTED",
            event_type="CONFLICTING_REPLAY",
            timestamp_utc="2026-11-15T10:00:01.000Z",
        )


def test_previous_event_hash_tampering_is_detected(
    event_store: PersistentEventStore,
    event_db_path: Path,
) -> None:
    add_three_event_chain(event_store)
    with sqlite3.connect(event_db_path) as connection:
        connection.execute("DROP TRIGGER trade_events_no_update")
        connection.execute(
            "UPDATE trade_events SET previous_event_hash=? WHERE event_id=?",
            ("f" * 64, "evt-submitting"),
        )

    result = event_store.verify_event_chain("intent-gate-a")
    assert result["status"] == "FAIL"
    assert any("previous event hash mismatch" in error for error in result["errors"])


def test_payload_tampering_is_detected(
    event_store: PersistentEventStore,
    event_db_path: Path,
) -> None:
    with sqlite3.connect(event_db_path) as connection:
        connection.execute("DROP TRIGGER trade_intents_identity_immutable")
        connection.execute(
            "UPDATE trade_intents SET payload_json=? WHERE intent_id=?",
            ('{"quantity":"0.01","side":"SELL","symbol":"BTC-USDT"}', "intent-gate-a"),
        )

    result = event_store.verify_event_chain("intent-gate-a")
    assert result["status"] == "FAIL"
    assert any("intent payload hash does not match stored payload" in error for error in result["errors"])


def test_deleted_event_is_detected(
    event_store: PersistentEventStore,
    event_db_path: Path,
) -> None:
    add_three_event_chain(event_store)
    with sqlite3.connect(event_db_path) as connection:
        connection.execute("DROP TRIGGER trade_events_no_delete")
        connection.execute(
            "DELETE FROM trade_events WHERE intent_id=? AND seq_num=2",
            ("intent-gate-a",),
        )

    result = event_store.verify_event_chain("intent-gate-a")
    assert result["status"] == "FAIL"
    assert any("sequence gap" in error for error in result["errors"])


def test_event_reordering_is_detected(
    event_store: PersistentEventStore,
    event_db_path: Path,
) -> None:
    add_three_event_chain(event_store)
    with sqlite3.connect(event_db_path) as connection:
        connection.execute("DROP TRIGGER trade_events_no_update")
        connection.execute(
            "UPDATE trade_events SET seq_num=seq_num+100 "
            "WHERE intent_id=? AND seq_num>=2",
            ("intent-gate-a",),
        )
        connection.execute(
            "UPDATE trade_events SET seq_num=2 WHERE event_id='evt-submitting'"
        )
        connection.execute(
            "UPDATE trade_events SET seq_num=3 WHERE event_id='evt-validated'"
        )

    result = event_store.verify_event_chain("intent-gate-a")
    assert result["status"] == "FAIL"
    assert any(
        "from_state does not match previous to_state" in error
        or "event hash mismatch" in error
        or "previous event hash mismatch" in error
        for error in result["errors"]
    )


def test_event_parent_intent_foreign_key_rejects_orphans(
    event_store: PersistentEventStore,
    event_db_path: Path,
) -> None:
    with sqlite3.connect(event_db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        foreign_keys = connection.execute("PRAGMA foreign_key_list(trade_events)").fetchall()
        assert any(
            row[2] == "trade_intents"
            and row[3] == "intent_id"
            and row[4] == "intent_id"
            for row in foreign_keys
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO trade_events (
                    event_id, intent_id, seq_num, client_order_id, payload_hash,
                    from_state, to_state, event_type, exchange_order_id,
                    evidence_json, timestamp_utc, previous_event_hash, event_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "evt-orphan", "missing-parent-intent", 1, "client-orphan",
                    "0" * 64, "NONE", "INTENT_RECORDED", "TEST", None, "{}",
                    "2026-11-15T10:00:00.000Z", None, "0" * 64,
                ),
            )


def test_file_backed_sqlite_recovers_after_close_and_reopen(
    event_store: PersistentEventStore,
    event_db_path: Path,
) -> None:
    add_three_event_chain(event_store)
    expected = [
        (event["event_id"], event["event_hash"])
        for event in event_store.list_events("intent-gate-a")
    ]
    event_store.close()

    restarted = PersistentEventStore(event_db_path)
    try:
        recovered = restarted.get_intent("intent-gate-a")
        events = restarted.list_events("intent-gate-a")
        assert recovered is not None
        assert recovered["current_state"] == "SUBMITTING"
        assert [(event["event_id"], event["event_hash"]) for event in events] == expected
        assert restarted.verify_event_chain("intent-gate-a")["status"] == "PASS"
    finally:
        restarted.close()


def test_full_event_chain_integrity_passes_for_untampered_history(
    event_store: PersistentEventStore,
) -> None:
    add_three_event_chain(event_store)
    events = event_store.list_events("intent-gate-a")
    result = event_store.verify_event_chain("intent-gate-a")

    assert result["status"] == "PASS"
    assert result["event_count"] == 3
    assert result["last_state"] == "SUBMITTING"
    assert events[0]["previous_event_hash"] is None
    for previous, current in zip(events, events[1:]):
        assert current["previous_event_hash"] == previous["event_hash"]


def test_registered_evaluation_protocol_hash_matches_file() -> None:
    digest = verify_protocol_hash()
    assert len(digest) == 64


def test_protocol_tampering_or_unregistered_thresholds_block_evaluation(
    tmp_path: Path,
) -> None:
    # The committed draft is integrity-protected but intentionally not evaluable:
    # numeric thresholds, purge/embargo windows and regime sample sizes are unset.
    with pytest.raises(ProtocolNotReadyError):
        verify_evaluation_protocol(require_ready=True)

    original_protocol = Path(__file__).resolve().parents[1] / "docs" / "evaluation_protocol.yaml"
    original_hash = Path(__file__).resolve().parents[1] / "docs" / "evaluation_protocol.sha256"
    modified_protocol = tmp_path / "evaluation_protocol.yaml"
    copied_hash = tmp_path / "evaluation_protocol.sha256"
    modified_protocol.write_bytes(original_protocol.read_bytes() + b"\n# unauthorized change\n")
    copied_hash.write_bytes(original_hash.read_bytes())

    with pytest.raises(ProtocolIntegrityError):
        verify_evaluation_protocol(modified_protocol, copied_hash)
