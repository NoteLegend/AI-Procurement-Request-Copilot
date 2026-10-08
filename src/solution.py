from __future__ import annotations

from src.config import get_api_key
from src.contracts import Architecture, ProcurementDecision

_ARCHITECTURE_MODULES = {
    "single": "src.agents.single",
    "staged": "src.agents.staged",
}


def handle_request(request_id: str, architecture: Architecture = "single") -> ProcurementDecision:
    """Assessment adapter.

    Both architectures share the same tools and the same deterministic policy
    engine. Only the orchestration differs:

      single - one agent gathers evidence and writes the judgement itself.
      staged - an analyst agent gathers evidence into a structured pack, and a
               separate reviewer agent decides from that pack alone.
    """
    # fail fast if no key - otherwise a run quietly degrades and looks like a pass
    get_api_key()

    module_path = _ARCHITECTURE_MODULES.get(architecture)
    if module_path is None:
        raise ValueError(f"Unknown architecture: {architecture!r}. Use 'single' or 'staged'.")

    import importlib
    agent_module = importlib.import_module(module_path)
    return agent_module.run(request_id)
