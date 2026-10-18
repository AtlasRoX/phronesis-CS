"""
Phronesis evaluation harness: curated and benchmark cases scored at the plan, retrieval and answer levels.

Run `python -m phronesis.evals --help`; the guide is docs/evals.md.
"""

from .schema import AnswerExpect, CaseResult, Check, EvalCase, PlanExpect, RetrievalExpect

__all__ = ["AnswerExpect", "CaseResult", "Check", "EvalCase", "PlanExpect", "RetrievalExpect"]
