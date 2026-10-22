"""Author-written small deterministic examples, not an independent research benchmark."""
import json
import platform
from pathlib import Path

from worldmind.domain import Config, ROADS
from worldmind.engine import Command, Engine, replay
from worldmind.version import LEGACY_RECORDING_VERSION as VERSION

CASES = {
    'normal': [],
    'hidden_bridge': [(1, Command(kind='block', target='bridge|home'))],
    'missing_materials': [(0, Command(kind='remove', target=p)) for p in ('garden', 'warehouse')],
    'no_roads': [(0, Command(kind='block', target=r)) for r in ROADS],
    'goal_change': [(2, Command(kind='goal', target='herb_clinic'))],
    'budget_exhaustion': [],
    'merchant_competition': [],
}
results = []
recordings = Path('docs/evidence/recordings')
recordings.mkdir(parents=True, exist_ok=True)
for name, inputs in CASES.items():
    for policy in ('Full', 'Local'):
        cfg = Config(policy=policy, goal='herb_clinic' if name == 'merchant_competition' else 'medicine_home', **({'decision_budget': 1} if name == 'budget_exhaustion' else {}),
                     merchant_takes_at=2 if name == 'merchant_competition' else 0)
        e = Engine(cfg, version=VERSION)
        for _ in range(cfg.max_ticks + 1):
            for tick, command in inputs:
                if e.tick == tick:
                    e.apply(command)
            e.apply(Command(kind='step'))
            if e.status not in ('ready', 'running'):
                break
        data = e.export()
        _, verification = replay(data)
        filename = f'{name}-{policy}.json'
        (recordings / filename).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        results.append({'case': name, 'policy': policy, 'status': e.status, 'tick': e.tick,
                        'metrics': e.metrics, 'planning_cpu_ms': e.cpu_ms,
                        'replay_verified_steps': verification['verified'], 'recording': filename})
report = {'version': VERSION, 'environment': {'python': platform.python_version(),
          'system': platform.system(), 'architecture': platform.machine()},
          'scope': '7 author-written cases x 2 policies; deterministic, not statistical confirmation',
          'cpu_scope': 'planning and sequence validation only, not perception/HTTP/render/network',
          'results': results}
Path('docs/evidence/evaluation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
for r in results:
    print(f'{r["case"]:22} {r["policy"]:5} {r["status"]:18} ticks={r["tick"]:3} budget={r["metrics"]["budget_used"]:5}')
