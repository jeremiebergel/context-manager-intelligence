"""Every Hermes Agent symbol CMI imports, in one place.

Several are Hermes internals (leading underscore or undocumented), so a Hermes upgrade can move
them. The core engine guards each import and falls back (fail-open); this list lets a test, CI or
a user check a given Hermes install in one command:

    python -m cmi.hermes_contract        # run with the Python that runs Hermes

Exit status 0 when every required symbol resolves; missing optional symbols only degrade
features (Console accounts, reviewer, pricing) and are reported, not fatal.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from typing import List, Tuple

# (module, attribute, required_by_core)
SYMBOLS: Tuple[Tuple[str, str, bool], ...] = (
    ("agent.context_engine", "ContextEngine", True),
    ("agent.model_metadata", "estimate_messages_tokens_rough", True),
    ("agent.context_compressor", "ContextCompressor", True),
    ("agent.context_compressor", "resolve_model_threshold", False),
    ("agent.context_compressor", "_summarize_tool_result", False),
    ("agent.usage_pricing", "get_pricing_entry", False),
    ("hermes_cli.config", "load_config_readonly", False),
    # cmi_review
    ("agent.auxiliary_client", "call_llm", False),
    # cmi_console
    ("agent.account_usage_cache", "_identity_id_for", False),
    ("agent.account_usage_cache", "has_account_usage", False),
    ("agent.credential_pool", "load_pool", False),
    ("agent.credential_pool", "PooledCredential", False),
    ("agent.credential_pool", "get_pool_strategy", False),
    ("hermes_cli.auth", "read_credential_pool", False),
    ("hermes_cli.inventory", "build_model_options_payload", False),
    ("hermes_cli.inventory", "load_picker_context", False),
    ("hermes_cli.version_info", "get_version_info", False),
    ("hermes_cli.profiles", "get_active_profile_name", False),
)


def check() -> List[Tuple[str, str, bool, str]]:
    """Return the symbols that do not resolve: (module, attr, required, error)."""
    missing = []
    for mod, attr, required in SYMBOLS:
        try:
            if not hasattr(importlib.import_module(mod), attr):
                missing.append((mod, attr, required, "attribute missing"))
        except Exception as e:  # ImportError, or a Hermes module failing at import
            missing.append((mod, attr, required, f"{type(e).__name__}: {e}"))
    return missing


def main() -> int:
    if importlib.util.find_spec("agent") is None:
        print("Hermes Agent is not importable from this Python: run this with the Python that runs Hermes")
        return 2
    missing = check()
    for mod, attr, required, err in missing:
        print(f"{'MISSING (core)' if required else 'missing (optional)'}: {mod}.{attr} — {err}")
    if not missing:
        print(f"OK: all {len(SYMBOLS)} Hermes symbols resolve")
    return 1 if any(r for _, _, r, _ in missing) else 0


if __name__ == "__main__":
    sys.exit(main())
