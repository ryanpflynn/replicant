"""Repository paths. REPLICANT_REPO_ROOT overrides; otherwise the checkout this package lives in."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def repo_root() -> Path:
    env = os.environ.get("REPLICANT_REPO_ROOT")
    if env and Path(env, "dev", "compose", "compose.yaml").is_file():
        return Path(env).resolve()
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "dev" / "compose" / "compose.yaml").is_file():
            return p
    cwd = Path.cwd().resolve()
    for p in (cwd, *cwd.parents):
        if (p / "dev" / "compose" / "compose.yaml").is_file():
            return p
    raise SystemExit("cannot find the Replicant repo (dev/compose/compose.yaml); set REPLICANT_REPO_ROOT")


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def compose_dir(self) -> Path:
        return self.root / "dev" / "compose"

    @property
    def compose_file(self) -> Path:
        return self.compose_dir / "compose.yaml"

    @property
    def v0_file(self) -> Path:
        return self.compose_dir / "compose.v0.yaml"

    @property
    def env_file(self) -> Path:
        return self.compose_dir / ".env"

    @property
    def dev_home(self) -> Path:
        """$REPLICANT_DEV_HOME: replicant/dev/.secrets (compose sees it as ../.secrets)."""
        return self.root / "dev" / ".secrets"

    @property
    def secrets(self) -> Path:
        return self.dev_home / "secrets"

    @property
    def generated(self) -> Path:
        return self.dev_home / "generated"

    def secret(self, name: str) -> str:
        return (self.secrets / name).read_text().strip()


def paths() -> Paths:
    return Paths(repo_root())
