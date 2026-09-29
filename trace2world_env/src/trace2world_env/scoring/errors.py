"""Errors raised while validating or reducing declared evaluation contracts."""

from __future__ import annotations


class EvidenceContractError(ValueError):
    """A state-kernel evidence contract or bounded expression is malformed."""


class StateKernelError(RuntimeError):
    """The packaged state service or its evaluator plan violated the host contract."""
