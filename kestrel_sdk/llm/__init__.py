"""LLM-related types and protocols.

Exports lightweight, stable types that feature packages need without
pulling in the full framework. Heavy LLM service implementation stays
in kestrel-sovereign.
"""

from .types import BackendType

__all__ = ["BackendType"]
