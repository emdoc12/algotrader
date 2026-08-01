"""Automated strategy research: desks propose, code judges.

A research loop, not a trading loop. Models are good at generating and
mechanizing hypotheses; they have no edge at discretionary judgement, so every
accept/reject decision here is pure compute.

The whole package is organized around one danger: testing many strategies
manufactures false positives. Four safeguards, each enforced by code rather
than convention —

  * ``hypothesis`` — a structured grammar (the custom-strategy DSL) with a
    canonical content hash, so proposals are machine-testable and identifiable.
  * ``registry``   — pre-registration (criteria fixed before results exist,
    results write-once) and the failure log (a rejected hash is never retested).
  * ``evaluate``   — hard out-of-sample: N contiguous non-overlapping periods,
    each independently scored on a frozen rule.
  * ``gate``       — a significance bar that scales with the cumulative number
    of tests, shared across all desks.

Silence is the expected output.
"""
from daytrader.research.gate import decide, required_alpha
from daytrader.research.hypothesis import (
    HypothesisError,
    canonical_criteria,
    canonical_spec,
    spec_hash,
)
from daytrader.research.registry import ResearchDB, research_db_path

__all__ = [
    "HypothesisError", "canonical_spec", "canonical_criteria", "spec_hash",
    "ResearchDB", "research_db_path", "decide", "required_alpha",
]
