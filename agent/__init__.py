"""Agent package bootstrap."""

from . import tools as _tools
from strategy.runtime import install as _install_strategy_runtime
from strategy.recovery_override import install as _install_recovery_override

_install_strategy_runtime(_tools)
_install_recovery_override()

from strategy.llm_reliability import install as _install_llm_reliability

_install_llm_reliability()
