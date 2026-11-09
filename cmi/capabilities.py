"""ModelCapabilities — thin view-model over existing Hermes metadata sources.

No new source of truth: every field is derived at construction time from
functions that already exist. Provider/model-agnostic by construction.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class ModelCapabilities:
    model: str = ""
    context_window: int = 0
    prompt_caching: bool = False
    tool_calling: bool = True
    vision: bool = False
    tokenizer: Optional[str] = None
    reserved_output: int = 0
    safety_margin_pct: float = 0.05

    @property
    def input_budget(self) -> int:
        eff = self.context_window - self.reserved_output
        if eff <= 0:
            eff = self.context_window
        return max(1, int(eff * (1.0 - self.safety_margin_pct)))

    @classmethod
    def resolve(
        cls,
        model: str,
        context_length: int,
        *,
        base_url: str = "",
        api_key: str = "",
        provider: str = "",
        max_tokens: Optional[int] = None,
        agent: Optional[Any] = None,
    ) -> "ModelCapabilities":
        caching = False
        if agent is not None:
            pc = getattr(agent, "prompt_caching", None)
            if isinstance(pc, dict):
                caching = bool(pc.get("enabled", False))
            elif isinstance(pc, bool):
                caching = pc
        vision = False
        if agent is not None:
            vision = bool(getattr(agent, "supports_vision", False))
        return cls(
            model=model or "",
            context_window=int(context_length or 0),
            prompt_caching=caching,
            tool_calling=True,
            vision=vision,
            tokenizer=None,
            reserved_output=int(max_tokens or 0),
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "context_window": self.context_window,
            "prompt_caching": self.prompt_caching,
            "tool_calling": self.tool_calling,
            "vision": self.vision,
            "input_budget": self.input_budget,
        }
