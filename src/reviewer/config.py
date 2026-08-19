"""Run configuration.

One config object per run, built from CLI flags plus a small number of
environment variables. Nothing else may read the environment: run identity is
the config, not the machine, so two developers get comparable results.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import constants


@dataclass
class Config:
    # Model / provider
    api_key: str = ""
    base_url: str = constants.DEFAULT_BASE_URL
    model: str = constants.DEFAULT_MODEL
    temperature: float = 0.0
    # v4-pro thinks by default. Review work does not need it, and thinking mode
    # rejects a forced tool_choice outright — which is how this pipeline gets a
    # grammar-enforced schema. It also requires echoing reasoning_content back
    # on every tool round-trip, which the agent loop does not do.
    thinking: bool = False

    # Repository under review
    repo_path: Path = field(default_factory=Path.cwd)
    resources_dir: Path = field(default_factory=lambda: _default_resources())

    # Pipeline switches
    ensemble_size: int = constants.ENSEMBLE_SIZE
    enable_validation: bool = True
    max_files: int = 100

    # Output
    post: bool = False
    output_path: Path | None = None
    verbose: bool = False

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        base = os.environ.get("DEEPSEEK_BASE_URL", constants.DEFAULT_BASE_URL)
        cfg = cls(api_key=key, base_url=base)
        for name, value in overrides.items():
            if value is not None:
                setattr(cfg, name, value)
        return cfg

    def require_api_key(self) -> str:
        if not self.api_key:
            raise SystemExit(
                "DEEPSEEK_API_KEY is not set. Export it, or pass --api-key."
            )
        return self.api_key


def _default_resources() -> Path:
    """Locate the bundled prompt library."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "resources"
        if (candidate / "rules" / "general.md").exists():
            return candidate
    return here.parent / "resources"
