"""CMI — Context Manager Intelligence engine.

Implements the ``ContextEngine`` contract (agent.context_engine.ContextEngine)
as a deterministic, provider/model-agnostic context SELECTION layer.
Compaction is fully delegated to the built-in ContextCompressor.
"""
__version__ = "1.11.4"

from .capabilities import ModelCapabilities
from .lifecycle import ToolResultLifecycle
from .working_set import WorkingSet
from .engine import CMIEngine

__all__ = ["ModelCapabilities", "ToolResultLifecycle", "WorkingSet", "CMIEngine"]


def register(ctx) -> None:
    """Canonical context-engine registration contract (see
    hermes_cli.plugins.PluginContext.register_context_engine and
    plugins/plugin_loader.instance_from_module). Without this hook, discovery
    falls back to a fragile dir(mod)-scan + bare attr() wrapped in
    contextlib.suppress(Exception) (plugins/plugin_loader.py), which silently
    swallows any construction error and logs "Context engine 'cmi' loaded but
    no engine instance found" instead of surfacing the real failure.
    """
    engine = CMIEngine()
    ctx.register_context_engine(engine)
    register_command = getattr(ctx, "register_command", None)
    if callable(register_command):
        register_command("cmi", _cmi_command, description="CMI: tokens and cost saved (session, day, 7 d, 30 d)")


def _cmi_command(raw_args: str = "") -> str:
    """/cmi — savings report. Reads the shared ledger, so it works from any surface."""
    from .stats import StatsLedger, report
    try:
        from hermes_constants import get_hermes_home
        ledger = StatsLedger(get_hermes_home() / "cmi_stats.jsonl")
    except Exception:
        return "CMI: statistics unavailable (HERMES_HOME not found)."
    return report(ledger)
