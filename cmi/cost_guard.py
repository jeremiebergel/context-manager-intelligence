"""Shadow cost accounting: what each request costs WITH CMI vs what the raw request would have
cost, under the provider's prompt-cache pricing.

Both streams are simulated the same way: the part of a request that repeats the previous request
of the same stream (message-level prefix) is a cache read, the rest is a cache write (or plain
input on providers without a write premium). Costs are in input-token equivalents, so they can
be priced later with any per-million input price.

It is a MEASUREMENT, recorded per turn in the savings ledger and shown by /cmi; the watchdog
alerts when CMI ended up costlier over a period. It is deliberately not a brake: a cache tier is
an investment that pays back over 100+ requests, so a session is costlier than raw for a while
right after one. Braking on the running total blocked exactly the tiers that make sessions end
cheaper (measured on real sessions: 21% quota saved without a brake, 4% with one).
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

# Anthropic-like defaults when the provider's pricing is unknown: the conservative assumption,
# since a write premium is what makes rewriting the prefix expensive.
DEFAULT_READ_RATIO = 0.1
DEFAULT_WRITE_RATIO = 1.25
def cache_ratios(model: str, provider: str = "") -> Tuple[float, float, Optional[float]]:
    """(read, write, input price per million) relative to the uncached input price."""
    try:
        from agent.usage_pricing import get_pricing_entry
        e = get_pricing_entry(model, provider=provider or None)
    except Exception:
        e = None
    if e is None or not e.input_cost_per_million:
        return DEFAULT_READ_RATIO, DEFAULT_WRITE_RATIO, None
    inp = float(e.input_cost_per_million)
    if e.cache_read_cost_per_million is None:
        return 1.0, 1.0, inp  # no prompt cache: every input token costs the same
    read = float(e.cache_read_cost_per_million) / inp
    write = float(e.cache_write_cost_per_million) / inp if e.cache_write_cost_per_million else 1.0
    return read, write, inp


class _Stream:
    __slots__ = ("prev", "cost", "tokens")

    def __init__(self) -> None:
        self.prev: List[int] = []
        self.cost = 0.0
        self.tokens = 0

    def observe(self, fps: Sequence[int], toks: Sequence[int], read: float, write: float,
                cold: bool = False) -> float:
        hit_msgs = 0
        for a, b in zip([] if cold else self.prev, fps):
            if a != b:
                break
            hit_msgs += 1
        hit = sum(toks[:hit_msgs])
        total = sum(toks)
        c = hit * read + (total - hit) * write
        self.prev = list(fps)
        self.cost += c
        self.tokens += total
        return c


class CostShadow:
    def __init__(self) -> None:
        self.read, self.write, self.price = DEFAULT_READ_RATIO, DEFAULT_WRITE_RATIO, None
        self.reset()

    def reset(self) -> None:
        self.raw = _Stream()
        self.cmi = _Stream()
        self.requests = 0

    def set_pricing(self, model: str, provider: str = "") -> None:
        self.read, self.write, self.price = cache_ratios(model, provider)

    def observe(self, raw_fps, raw_toks, out_fps, out_toks, cold: bool = False) -> None:
        # cold: the provider cache expired since the last request (TTL), for both streams.
        self.raw.observe(raw_fps, raw_toks, self.read, self.write, cold)
        self.cmi.observe(out_fps, out_toks, self.read, self.write, cold)
        self.requests += 1

    def saving_ratio(self) -> Optional[float]:
        return (1 - self.cmi.cost / self.raw.cost) if self.raw.cost else None
