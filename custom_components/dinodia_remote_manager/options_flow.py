"""Options flow shim for Dinodia Remote Manager."""

# Architecture: Home Assistant options entry point; the implementation remains
# in config_flow.py so setup and update paths share the same binding contract.

from __future__ import annotations

from .config_flow import DinodiaRemoteManagerOptionsFlow

# End of architecture annotation; flow implementation remains in config_flow.py.
