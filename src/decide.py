"""Merges the rule outcome and the model's judgement into the final contract
object. This is where the "code wins" rule is enforced.
"""
from __future__ import annotations

import re

from src.contracts import EvidenceItem, ProcurementDecision, RunTelemetry
from src.llm import LlmCounter
from src.rules import ACTION_LABELS, ACTIONS, EXTRA_RISK_FLAGS, RuleOutcome, action_rank
from src.tools import Toolbox

MAX_EXTRA_FLAGS = 3
MAX_EXTRA_MISSING = 3

# raised by this module, not the model - must survive the flag filter below
SYSTEM_RISK_FLAGS = {"llm_unavailable"}


class JudgementSanitiser:
    """Filters the model's raw judgement dict down to the parts the rest of
    the pipeline is allowed to trust, grouped here instead of as loose
    module-level functions.
    """

    @staticmethod
    def flag_name(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")[:60]

    @staticmethod
    def clean_list(values, limit: int) -> list[str]:
        out: list[str] = []
        for value in values or []:
            text = str(value).strip()
            if text and text.lower() not in {"none", "n/a", "not applicable"}:
                out.append(text)
            if len(out) >= limit:
                break
        return out

    @classmethod
    def extra_flags(cls, judgement: dict, already_present: list[str]) -> list[str]:
        allowed = set(EXTRA_RISK_FLAGS) | SYSTEM_RISK_FLAGS
        accepted = []
        for raw in cls.clean_list(judgement.get("additional_risk_flags"), MAX_EXTRA_FLAGS):
            name = cls.flag_name(raw)
            if name in allowed and name not in already_present and name not in accepted:
                accepted.append(name)
        return accepted


def resolve_action(outcome: RuleOutcome, judgement: dict) -> str:
    """The model may make the action stricter. It can never make it weaker."""
    wanted = str(judgement.get("recommended_action", "")).strip().lower()
    if wanted not in ACTIONS:
        return outcome.minimum_action
    return wanted if action_rank(wanted) > action_rank(outcome.minimum_action) else outcome.minimum_action


def build_decision(
    request: dict,
    outcome: RuleOutcome,
    judgement: dict,
    toolbox: Toolbox,
    counter: LlmCounter,
    extra_notes: list[str] | None = None,
) -> ProcurementDecision:
    action = resolve_action(outcome, judgement)

    approvals = outcome.sorted_approvals()
    flags = list(outcome.risk_flags) + JudgementSanitiser.extra_flags(judgement, outcome.risk_flags)

    missing = list(outcome.missing_information)
    if outcome.missing_information:  # only policy-1 can mark a request incomplete
        for item in JudgementSanitiser.clean_list(judgement.get("additional_missing_information"), MAX_EXTRA_MISSING):
            if item not in missing:
                missing.append(item)
    if missing and "missing_information" not in flags:
        flags.append("missing_information")

    evidence: list[EvidenceItem] = list(outcome.evidence)
    for note in extra_notes or []:
        evidence.append(EvidenceItem(source="orchestrator", finding=note, reference=None))

    model_text = str(judgement.get("recommendation", "")).strip()
    overlap = str(judgement.get("overlap_assessment", "")).strip()
    overlap_reason = str(judgement.get("overlap_reason", "")).strip()
    if overlap in {"duplicate", "credible_expansion", "different_need"} and overlap_reason:
        model_text = f"{model_text} Overlap view: {overlap.replace('_', ' ')} - {overlap_reason}".strip()

    recommendation = ACTION_LABELS[action]
    if model_text:
        recommendation = f"{recommendation} - {model_text}"

    next_step = str(judgement.get("next_step", "")).strip() or (
        "Send this evidence package to " + ", ".join(approvals) + " for human review."
    )

    return ProcurementDecision(
        request_id=request["request_id"],
        recommendation=recommendation[:1200],
        evidence=evidence,
        required_approvals=approvals,
        missing_information=missing,
        risk_flags=flags,
        next_step=next_step[:600],
        human_review_required=True,  # policy 11 - a human always decides
        telemetry=RunTelemetry(llm_calls=counter.calls, tool_calls=toolbox.call_count, tool_names=toolbox.tool_names),
    )


def fallback_judgement(outcome: RuleOutcome, reason: str) -> dict:
    """Used when the model cannot be reached or keeps returning unusable output.
    The deterministic result is still a safe answer, so the run degrades
    instead of failing, and the reason is surfaced as a risk flag, not hidden.
    """
    return {
        "recommended_action": outcome.minimum_action,
        "recommendation": (
            "This recommendation came from the deterministic policy checks only, because "
            f"the language model step could not complete ({reason}). A human should read the "
            "evidence below in full."
        ),
        "next_step": (
            "Route to " + ", ".join(outcome.sorted_approvals() or ["Procurement"])
            + " for human review, and re-run the copilot once the model is reachable."
        ),
        "additional_risk_flags": ["llm_unavailable"],
        "additional_missing_information": [],
        "overlap_assessment": "unclear",
        "overlap_reason": "",
    }
