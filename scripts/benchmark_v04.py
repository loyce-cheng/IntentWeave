"""Fixed cooperation cases. Include failures, keep raw recordings outside historical evidence."""
import argparse
import json
from pathlib import Path
from time import perf_counter

from worldmind.cooperation_domain import CooperationConfig, CooperationCommand as Command
from worldmind.runtime import compare, replay


CASES = [
    ("normal", {}, []),
    ("hidden_bridge", {}, [(1, "block", "bridge|home")]),
    ("competition", {"merchant_takes_at": 2}, [(0, "block", "garden|warehouse")]),
    ("short_evidence", {"evidence_ttl": 2}, []),
    ("delayed_messages", {"message_delay": 5}, []),
    ("rejected_order", {"accept_order": False}, []),
    ("exact_deadline", {"order_deadline": 7}, []),
    ("missed_deadline", {"order_deadline": 6}, []),
    ("no_materials", {}, [(0, "remove", "garden"), (0, "remove", "warehouse")]),
    ("cancel_handover", {}, [(6, "order_cancel", "supply-1")]),
    ("interrupt_craft", {}, [(7, "interrupt", "doctor")]),
    ("reserved_material_removed", {}, [(3, "remove", "garden")]),
    ("change_final_goal", {}, [(6, "goal", "medicine_clinic")]),
    ("limited_budget", {"total_budget": 10}, []),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="var/benchmarks/v0.4")
    args = parser.parse_args()
    output = Path(args.output)
    (output / "recordings").mkdir(parents=True, exist_ok=True)
    results = []
    for name, options, schedule in CASES:
        commands = [(tick, Command(kind=kind, target=target)) for tick, kind, target in schedule]
        for policy, result in compare(CooperationConfig(scenario="cooperation", **options), commands).items():
            recording = result.pop("recording")
            started = perf_counter()
            engine, verified = replay(recording)
            replay_wall_ms = (perf_counter() - started) * 1000
            assert verified["complete"] and engine.export() == recording
            assert not engine.claims
            file = f"{name}-{policy}.json"
            (output / "recordings" / file).write_text(json.dumps(recording, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            results.append({"case": name, "policy": policy, "options": options, "schedule": schedule,
                            **result, "order_status": engine.order.status,
                            "fulfilled_tick": engine.order.fulfilled_tick,
                            "replay_wall_ms": replay_wall_ms,
                            "recording": "recordings/" + file})
    (output / "comparison.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"cases": len(CASES), "recordings": len(results), "all_replayed": True,
                      "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
