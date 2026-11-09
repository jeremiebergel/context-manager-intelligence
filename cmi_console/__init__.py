"""CMI Console agent half: one observer hook feeding the account ledger (ledger.py).

The hook reads Hermes' persisted per-account request counters (read-only) and appends an
opaque-key line per request to $HERMES_HOME/cmi_console_ledger.jsonl. It never blocks or alters a request.
"""


def register(ctx) -> None:
    try:
        from .ledger import on_post_api_request

        ctx.register_hook("post_api_request", on_post_api_request)
    except Exception:  # plugin must load even if the hook API differs
        return None
