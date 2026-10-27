"""Importing this package registers the node types of this build (core + build-specific modules)."""
import importlib

from replicant_worker import build_config
from replicant_worker.nodes import core  # noqa: F401

for _m in build_config.EXTRA_NODE_MODULES:
    importlib.import_module(_m)
