"""Compare replay CPU and retained receipt memory across source checkouts.

Run with PYTHONPATH pointing to the desired checkout's src. This script only
reads the frozen recordings and prints JSON; it never rewrites their hashes.
"""
import gc
import json
import statistics
import time
import tracemalloc
from pathlib import Path

from worldmind.engine import Command, Engine, replay
from worldmind.sessions import Session


def replay_timings():
    paths = sorted((Path(__file__).resolve().parents[1] / "docs/evidence/v0.2.1/recordings").glob("*.json"))
    records = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    samples = []
    for _ in range(5):
        start = time.process_time_ns()
        for record in records:
            replay(record)
        samples.append((time.process_time_ns() - start) / 1e6)
    return {"recordings": len(records), "cpu_ms_samples": samples,
            "cpu_ms_median": statistics.median(samples)}


def receipt_memory():
    session = Session(Engine())
    for _ in range(8):
        session.engine.apply(Command(kind="step"))
    gc.collect()
    tracemalloc.start()
    for i in range(1000):
        session.revision = i
        if hasattr(session, "remember"):
            session.remember(str(i), str(i))
        else:  # baseline kept full snapshots; preserve its actual representation
            session.receipts[str(i)] = (str(i), session.view())
    gc.collect()
    retained, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {"receipts": len(session.receipts), "retained_bytes": retained, "peak_bytes": peak}


def main():
    print(json.dumps({"replay": replay_timings(), "receipt_memory": receipt_memory()}, indent=2))


if __name__ == "__main__":
    main()
