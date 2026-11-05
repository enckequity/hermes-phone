"""Hermes entry point. Hermes imports this file by path and calls ``register(ctx)``.

All logic lives in the ``hermes_phone`` package next to this file; the relative import works
because Hermes loads the plugin directory as a package. The absolute fallback only matters when
a tool (for example pytest collecting the repo root) imports this file as a bare module.
"""
try:
    from .hermes_phone import register
except ImportError:  # pragma: no cover - bare-module import outside Hermes
    from hermes_phone import register

__all__ = ["register"]
