"""Explicit session/recording dispatch; legacy simulation remains frozen."""
from pydantic import TypeAdapter

from .domain import Config
from .engine import Engine, Command, Recording, replay as river_replay, compare as river_compare
from .cooperation import CooperationEngine, CooperationRecording, replay as cooperation_replay
from .cooperation_domain import CooperationConfig, CooperationCommand, PROTOCOL

SessionConfig = CooperationConfig | Config
SessionRecording = CooperationRecording | Recording
SessionCommand = CooperationCommand | Command
recording_adapter = TypeAdapter(SessionRecording)


def make_engine(config):
    return CooperationEngine(config) if isinstance(config, CooperationConfig) else Engine(config)


def normalize_command(engine, command):
    cls = CooperationCommand if isinstance(engine, CooperationEngine) else Command
    return cls.model_validate(command.model_dump(exclude_none=True))


def replay(data, until=None):
    if data.get("schema_version") == PROTOCOL:
        return cooperation_replay(data, until)
    return river_replay(data, until)


def compare(config, commands=None):
    if not isinstance(config, CooperationConfig):
        return river_compare(config, commands)
    schedule = [(1, CooperationCommand(kind="block", target="bridge|home"))] if commands is None else commands
    results = {}
    for policy in ("Full", "Local"):
        engine = CooperationEngine(config.model_copy(update={"policy": policy}))
        for _ in range(config.max_ticks + 1):
            if engine.status not in ("ready", "running"):
                break
            for tick, command in schedule:
                if tick == engine.tick:
                    engine.advance(command)
            engine.advance(CooperationCommand(kind="step"))
        results[policy] = {"status": engine.status, "tick": engine.tick, "metrics": engine.metrics,
                           "cpu_ms": engine.cpu_ms, "recording": engine.export()}
    return results
