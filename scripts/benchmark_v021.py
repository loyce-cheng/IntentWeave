"""Reproducible v0.2.0 / v0.2.1 development comparison, without statistical claims.

Includes all original nine cases, declared TTL stress cases and a fixed exhaustive
single-intervention sweep. Failed runs remain in reports. No external model calls.
"""
from __future__ import annotations

import argparse
import json
import platform
from pathlib import Path
from statistics import median

from benchmark_v02 import CASES as ORIGINAL_CASES
from worldmind.domain import Config, GOALS, ROADS
from worldmind.engine import Command, Engine, replay
from worldmind.version import V02_RECORDING_VERSION, RECORDING_VERSION

CASES = {
    **ORIGINAL_CASES,
    **{f"evidence_ttl_{ttl}": ({"evidence_ttl": ttl, "max_ticks": 40}, []) for ttl in (2, 4, 5)},
    "ttl2_hidden_bridge": ({"evidence_ttl": 2, "max_ticks": 40}, [(1, "block", "bridge|home")]),
    "ttl2_merchant": ({"evidence_ttl": 2, "max_ticks": 40, "goal": "herb_clinic",
                       "merchant_takes_at": 2}, []),
    "ttl2_no_materials": ({"evidence_ttl": 2, "max_ticks": 40},
                          [(0, "remove", p) for p in ("warehouse", "garden")]),
    "ttl2_both_home_roads": ({"evidence_ttl": 2, "max_ticks": 40},
                             [(1, "block", "bridge|home"), (5, "block", "ferry|home")]),
}


def episode(config, schedule, version=RECORDING_VERSION):
    engine = Engine(config, version=version)
    for _ in range(config.max_ticks + 1):
        for tick, kind, target in schedule:
            if engine.tick == tick:
                engine.advance(Command(kind=kind, target=target))
        engine.advance(Command(kind="step"))
        if engine.status not in ("ready", "running"):
            return engine
    raise AssertionError("unbounded episode")


def summarize(engine):
    return {"status": engine.status, "tick": engine.tick, "metrics": engine.metrics,
            "planning_cpu_ms": engine.cpu_ms, "final_hash": engine.state_hash()}


def run(output, repeats):
    output.mkdir(parents=True, exist_ok=True)
    recordings = output / "recordings"
    recordings.mkdir(exist_ok=True)
    results = []
    for case, (overrides, schedule) in CASES.items():
        for version in (V02_RECORDING_VERSION, RECORDING_VERSION):
            for policy in ("Full", "Local"):
                config = Config(policy=policy, **overrides)
                engine = episode(config, schedule, version)
                recording = engine.export()
                _, verified = replay(recording)
                filename = f"{version.split('/')[0]}-{case}-{policy}.json"
                (recordings / filename).write_text(json.dumps(recording, ensure_ascii=False, indent=2) + "\n",
                                                   encoding="utf-8")
                samples = [engine.cpu_ms]
                for _ in range(repeats - 1):
                    repeated = episode(config, schedule, version)
                    assert repeated.state_hash() == engine.state_hash()
                    samples.append(repeated.cpu_ms)
                result = {"case": case, "version": version, "policy": policy, **summarize(engine),
                          "cpu_samples_ms": samples, "cpu_median_ms": median(samples),
                          "recording": filename, "replay_verified_steps": verified["verified"]}
                results.append(result)
                print(case, version, policy, engine.status, engine.tick, engine.metrics["budget_used"])
    sweep = []
    for goal in GOALS:
        for tick in (0, 1, 3, 5):
            for kind, targets in (("remove", ("warehouse", "garden")), ("block", ROADS)):
                for target in targets:
                    pair = {policy: summarize(episode(Config(policy=policy, goal=goal, max_ticks=40),
                                                       [(tick, kind, target)]))
                            for policy in ("Full", "Local")}
                    sweep.append({"goal": goal, "intervention": [tick, kind, target], "results": pair})
    report = {
        "scope": "16 author-written cases x 2 versions x 2 policies, plus 120 paired v0.2.1 interventions",
        "budget_scope": "BFS/map node expansions + action validations, including suffix compilation and failed probes",
        "cpu_scope": "process CPU inside planning/context construction/validation only; excludes observation, execution and IO",
        "timing_note": "technical repeats are not independent samples; no significance or universal speed claim",
        "repeats": repeats,
        "environment": {"python": platform.python_version(), "system": platform.system()},
        "cases": {name: {"config": config, "interventions": schedule}
                  for name, (config, schedule) in CASES.items()},
        "results": results,
        "intervention_sweep": sweep,
        "sweep_local_budget_regressions": sum(p["results"]["Local"]["metrics"]["budget_used"] >
                                               p["results"]["Full"]["metrics"]["budget_used"] for p in sweep),
        "sweep_status_mismatches": sum(p["results"]["Local"]["status"] !=
                                       p["results"]["Full"]["status"] for p in sweep),
    }
    (output / "comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                             encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("docs/evidence/v0.2.1"))
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 100:
        parser.error("repeats must be between 1 and 100")
    run(args.output, args.repeats)
