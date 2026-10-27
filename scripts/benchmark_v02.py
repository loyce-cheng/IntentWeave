"""Replay-verified development comparison; not an independent/statistical benchmark.

Retain the v0.1 seven cases, add two declared stress cases, and run both protocol
versions through the same authoritative engine and fixed intervention schedule.
No model API, random intervention, success-only filtering, or timing threshold.
"""
from __future__ import annotations

import argparse
import json
import platform
from pathlib import Path

from worldmind.domain import Config, ROADS
from worldmind.engine import Command, Engine, replay
from worldmind.version import LEGACY_RECORDING_VERSION, V02_RECORDING_VERSION

CASES = {
    "normal": ({}, []),
    "hidden_bridge": ({}, [(1, "block", "bridge|home")]),
    "missing_materials": ({}, [(0, "remove", p) for p in ("garden", "warehouse")]),
    "no_roads": ({}, [(0, "block", r) for r in ROADS]),
    "goal_change": ({}, [(2, "goal", "herb_clinic")]),
    "budget_exhaustion": ({"decision_budget": 1}, []),
    "merchant_competition": ({"goal": "herb_clinic", "merchant_takes_at": 2}, []),
    "short_evidence_ttl": ({"evidence_ttl": 3, "max_ticks": 40}, []),
    "both_home_roads_lost": ({}, [(1, "block", "bridge|home"), (5, "block", "ferry|home")]),
}


def run(output: Path) -> dict:
    recordings = output / "recordings"
    recordings.mkdir(parents=True, exist_ok=True)
    results = []
    for version in (LEGACY_RECORDING_VERSION, V02_RECORDING_VERSION):
        for case, (overrides, schedule) in CASES.items():
            for policy in ("Full", "Local"):
                engine = Engine(Config(policy=policy, **overrides), version=version)
                for _ in range(engine.config.max_ticks + 1):
                    for tick, kind, target in schedule:
                        if engine.tick == tick:
                            engine.advance(Command(kind=kind, target=target))
                    engine.advance(Command(kind="step"))
                    if engine.status not in ("ready", "running"):
                        break
                recording = engine.export()
                _, verified = replay(recording)
                filename = f"{version.split('/')[0]}-{case}-{policy}.json"
                (recordings / filename).write_text(
                    json.dumps(recording, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                result = {"version": version, "case": case, "policy": policy,
                          "status": engine.status, "tick": engine.tick,
                          "metrics": engine.metrics, "planning_cpu_ms": engine.cpu_ms,
                          "replay_verified_steps": verified["verified"], "recording": filename}
                results.append(result)
                print(f"{version:28} {case:23} {policy:5} {engine.status:18} "
                      f"tick={engine.tick:3} budget={engine.metrics['budget_used']:5}")
    report = {
        "scope": "9 author-written development cases x 2 versions x 2 policies; no statistical inference",
        "budget_scope": "node expansions + action validation, including suffix-boundary compilation",
        "cpu_scope": "planning and sequence validation only; individual timings are not a speed claim",
        "environment": {"python": platform.python_version(), "system": platform.system(),
                        "architecture": platform.machine()},
        "cases": {name: {"config": config, "interventions": schedule}
                  for name, (config, schedule) in CASES.items()},
        "results": results,
    }
    (output / "comparison.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("var/benchmarks/v0.2"))
    run(parser.parse_args().output)
