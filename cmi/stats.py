"""Savings ledger: one JSON line per completed turn in ``$HERMES_HOME/cmi_stats.jsonl``.

Append-only (O_APPEND, one short write per turn), so several processes (desktop, gateway, cron)
can write at once without a lock. ``report()`` aggregates it for the ``/cmi`` command.
"""
from __future__ import annotations

from . import _fs

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

RETENTION_DAYS = 180


class StatsLedger:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path

    def set_path(self, path: Path) -> None:
        self.path = path

    def add(self, session_id: str, model: str, raw: int, compiled: int, requests: int, recalls: int = 0,
            lexical: int = 0, provider: str = "", cost_raw: float = 0.0, cost_cmi: float = 0.0) -> None:
        if self.path is None or requests <= 0:
            return
        line = json.dumps({"t": int(time.time()), "s": session_id or "", "m": model or "", "p": provider or "", "raw": int(raw),
                           "out": int(compiled), "n": int(requests), "rc": int(recalls), "lx": int(lexical), "cr": round(cost_raw), "cc": round(cost_cmi)},
                          separators=(",", ":")) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | _fs.O_NOFOLLOW | _fs.O_BINARY, 0o600)
            try:
                os.write(fd, line.encode())
            finally:
                os.close(fd)
        except OSError:
            pass

    def rows(self):
        if self.path is None:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
        except OSError:
            return

    def prune(self, days: int = RETENTION_DAYS) -> None:
        if self.path is None or not self.path.exists():
            return
        cutoff = time.time() - days * 86400
        rows = list(self.rows())
        keep = [r for r in rows if r.get("t", 0) >= cutoff]
        if len(keep) == len(rows):
            return
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_bytes("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in keep).encode("utf-8"))
        _fs.chmod_private(tmp, 0o600)
        os.replace(tmp, self.path)


def _price_per_million(model: str, provider: str = "") -> Optional[float]:
    try:
        from agent.usage_pricing import get_pricing_entry
        e = get_pricing_entry(model, provider=provider or None)
        if e and e.input_cost_per_million is not None:
            return float(e.input_cost_per_million)
    except Exception:
        pass
    return None


def _fmt_tokens(n: float) -> str:
    return f"{n / 1e6:.1f} M" if n >= 1e6 else f"{n / 1e3:.0f} k" if n >= 1e3 else f"{int(n)}"


def report(ledger: StatsLedger, current_session: str = "", live: Optional[Dict[str, Any]] = None) -> str:
    now = time.time()
    windows = [("Cette session", lambda r: r.get("s") == current_session and current_session),
               ("Aujourd'hui", lambda r: r.get("t", 0) >= now - (now % 86400) - time.timezone),
               ("7 derniers jours", lambda r: r.get("t", 0) >= now - 7 * 86400),
               ("30 derniers jours", lambda r: r.get("t", 0) >= now - 30 * 86400)]
    rows = list(ledger.rows())
    prices: Dict[tuple, Optional[float]] = {}
    lines = ["**CMI — économies de contexte**", "",
             "| Période | Requêtes | Tokens sans CMI | Économisés | Coût réel sans → avec CMI* |",
             "|---|---|---|---|---|"]
    for label, keep in windows:
        sel = [r for r in rows if keep(r)]
        if label == "Cette session" and live and live.get("n"):
            sel = sel + [live]
        raw = sum(r.get("raw", 0) for r in sel)
        out = sum(r.get("out", 0) for r in sel)
        n = sum(r.get("n", 0) for r in sel)
        if not n:
            lines.append(f"| {label} | 0 | – | – | – |")
            continue
        pct = (raw - out) / raw * 100 if raw else 0
        c_raw = c_cmi = 0.0
        priced = True
        for r in sel:
            m = (r.get("m", ""), r.get("p", ""))
            if m not in prices:
                prices[m] = _price_per_million(*m)
            if prices[m] is None:
                priced = False
                continue
            c_raw += r.get("cr", 0) * prices[m] / 1e6
            c_cmi += r.get("cc", 0) * prices[m] / 1e6
        if c_raw > 0:
            delta = (c_cmi / c_raw - 1) * 100
            cost = f"{c_raw:,.2f} $ → {c_cmi:,.2f} $ ({delta:+.0f} %)" + ("" if priced else " (partiel)")
        else:
            cost = "prix inconnu"
        lines.append(f"| {label} | {n} | {_fmt_tokens(raw)} | {_fmt_tokens(raw - out)} ({pct:.0f} %) | {cost} |")
    rc = sum(r.get("rc", 0) for r in rows if r.get("t", 0) >= now - 7 * 86400)
    lx = sum(r.get("lx", 0) for r in rows if r.get("t", 0) >= now - 7 * 86400)
    lines += ["", f"Rappels sur 7 jours : {rc} via `cmi_recall`, {lx} anticipés (sans appel d'outil).",
              "", "*Coût d'entrée simulé requête par requête avec le cache du fournisseur (lecture/écriture "
              "au tarif public du modèle), avec et sans CMI. Sur un forfait, l'écart est du quota en plus."]
    return "\n".join(lines)
