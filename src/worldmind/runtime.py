"""Explicit session/recording dispatch; legacy simulation remains frozen."""
from pydantic import TypeAdapter

from .domain import Config
from .engine import Engine, Command, Recording, replay as river_replay
from .cooperation import CooperationEngine, CooperationRecording, replay as cooperation_replay
from .cooperation_domain import CooperationConfig, CooperationCommand, PROTOCOL

SessionConfig = CooperationConfig | Config
SessionRecording = CooperationRecording | Recording
SessionCommand = CooperationCommand | Command
SessionEngine = CooperationEngine | Engine
recording_adapter = TypeAdapter(SessionRecording)


def make_engine(config: SessionConfig) -> SessionEngine:
    return CooperationEngine(config) if isinstance(config, CooperationConfig) else Engine(config)


def normalize_command(engine, command):
    cls = CooperationCommand if isinstance(engine, CooperationEngine) else Command
    return cls.model_validate(command.model_dump(exclude_none=True))


def replay(data, until=None):
    if data.get("schema_version") == PROTOCOL:
        return cooperation_replay(data, until)
    return river_replay(data, until)


def run_scenario(engine: SessionEngine, commands=None) -> SessionEngine:
    """Run a fixed tick schedule; None uses the demo intervention, [] uses none."""
    command_type = CooperationCommand if isinstance(engine, CooperationEngine) else Command
    schedule = [(1, command_type(kind="block", target="bridge|home"))] if commands is None else commands
    for _ in range(engine.config.max_ticks + 1):
        if engine.status not in ("ready", "running"):
            break
        for tick, command in schedule:
            if tick == engine.tick:
                engine.advance(command)
        engine.advance(command_type(kind="step"))
    return engine


def compare(config: SessionConfig, commands=None):
    results = {}
    for policy in ("Full", "Local"):
        engine = run_scenario(make_engine(config.model_copy(update={"policy": policy})), commands)
        results[policy] = {"status": engine.status, "tick": engine.tick, "metrics": engine.metrics,
                           "cpu_ms": engine.cpu_ms, "recording": engine.export()}
    return results
