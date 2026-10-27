"""Hash verification shared by independently versioned simulation protocols."""


def verify_replay(engine, recording, until=None):
    """Replay a validated recording on its protocol's freshly constructed engine."""
    actual_hash = engine.state_hash()
    if recording.initial_hash != actual_hash:
        raise ValueError("initial hash mismatch")
    count = len(recording.steps) if until is None else until
    if type(count) is not int or not 0 <= count <= len(recording.steps):
        raise ValueError("invalid replay cursor")
    for i, step in enumerate(recording.steps[:count]):
        engine.advance(step.input)
        actual = engine.trace[-1]  # Computed from execution, never imported hashes.
        actual_hash = actual["state_hash"]
        if step.state_hash != actual_hash or step.event_hash != actual["event_hash"]:
            raise ValueError(f"replay divergence at step {i + 1}")
    if count == len(recording.steps) and actual_hash != recording.final_hash:
        raise ValueError("final hash mismatch")
    return engine, {"verified": count, "total": len(recording.steps),
                    "complete": count == len(recording.steps), "mode": "verified_replay"}
