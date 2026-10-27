"""Importing this package registers the node types of this build (core + build-specific modules)."""
import importlib

from .. import build_config
from . import core  # noqa: F401

for _m in build_config.EXTRA_NODE_MODULES:
    importlib.import_module(_m)
