import argparse
import json
from pathlib import Path

from .domain import Config, canonical
from .engine import Command
from .runtime import make_engine, compare, replay
from .cooperation_domain import CooperationConfig


def main():
    parser = argparse.ArgumentParser(prog="worldmind")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    run = sub.add_parser("run")
    run.add_argument("--policy", choices=["Full", "Local"], default="Local")
    run.add_argument("--scenario", choices=["river", "cooperation"], default="river")
    run.add_argument("--output", default="var/recording.json")
    rep = sub.add_parser("replay")
    rep.add_argument("file")
    ev = sub.add_parser("eval")
    ev.add_argument("--output", default="var/comparison.json")
    ev.add_argument("--scenario", choices=["river", "cooperation"], default="river")
    init = sub.add_parser("init-ledger")
    init.add_argument("--path", default="var/model-ledger.sqlite3")
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn
        uvicorn.run("worldmind.api:create_app", factory=True, host=args.host, port=args.port)
    elif args.command == "init-ledger":
        from .models import Ledger
        Ledger(args.path)
        print("Ledger initialized; model calls remain disabled unless explicitly enabled.")
    elif args.command == "replay":
        with Path(args.file).open("rb") as source:
            data = source.read(2_000_001)
        if len(data) > 2_000_000:
            raise SystemExit("Recording exceeds 2 MB")
        _, result = replay(json.loads(data))
        print(canonical(result))
    else:
        if args.command == "eval":
            data = compare(CooperationConfig(scenario="cooperation") if args.scenario == "cooperation" else Config())
            print(canonical({k: {n: v for n, v in r.items() if n != "recording"} for k, r in data.items()}))
        else:
            config = (CooperationConfig(scenario="cooperation", policy=args.policy)
                      if args.scenario == "cooperation" else Config(policy=args.policy))
            e = make_engine(config)
            for _ in range(e.config.max_ticks + 1):
                if e.tick == 1:
                    e.advance(Command(kind="block", target="bridge|home"))
                e.advance(Command(kind="step"))
                if e.status not in ("ready", "running"):
                    break
            data = e.export()
            print(canonical({"status": e.status, "tick": e.tick, "metrics": e.metrics}))
        dest = Path(args.output)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(canonical(data), encoding="utf-8")


if __name__ == "__main__":
    main()
