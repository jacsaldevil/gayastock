"""Agent package bootstrap."""

from . import tools as _tools
from strategy.runtime import install as _install_strategy_runtime

_install_strategy_runtime(_tools)
