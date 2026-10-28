"""Compatibility shim.

Discovery settings live in the central ``config`` module. This file previously
contained a second Config class whose DEBUG default disagreed with the rest of
the application.
"""

from config import Config

__all__ = ["Config"]
