"""Application releases and deterministic recording protocols are explicit contracts."""

__version__ = "0.3.0"
# Application releases can change packaging, APIs and UI without changing the
# deterministic simulation. Keep the protocol until its execution semantics change.
RECORDING_VERSION = "worldmind-0.2.1/river-1"
V02_RECORDING_VERSION = "worldmind-0.2.0/river-1"
LEGACY_RECORDING_VERSION = "worldmind-0.1.0/river-1"
SUPPORTED_RECORDING_VERSIONS = (LEGACY_RECORDING_VERSION, V02_RECORDING_VERSION, RECORDING_VERSION)
