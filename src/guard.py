"""Prompt-injection scanner (policy section 9).

Implemented as plain regex rather than a model call: the check has to return
the same verdict every time it sees the same text, and letting a model judge
whether it is being manipulated is exactly the kind of question an injection
attack is built to talk its way around.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


class InjectionPatterns:
    """Namespace holding the pattern table as a class attribute, so it reads
    as a small piece of configuration data rather than a loose module list.
    """

    TABLE: list[tuple[str, str]] = [
        (
            r"\b(ignore|disregard|forget)\b[^.]{0,40}\b(rule|rules|policy|policies|instruction|instructions|guideline|guidelines|check|checks|step|steps)\b",
            "tells the assistant to ignore rules or instructions",
        ),
        (
            r"\b(treat|mark|consider|record|log)\b[^.]{0,40}\b(pre-?approved|cfo-?approved|already approved|as approved|as authoris?ed)\b",
            "tells the assistant to treat the request as already approved",
        ),
        (
            r"\b(approve|authoris?e|sign off on)\b[^.]{0,30}\b(immediately|right away|at once|now|automatically|without review|without approval)\b",
            "tells the assistant to approve without human review",
        ),
        (
            r"\b(auto|automatically)\s*-?\s*approv",
            "asks for automatic approval",
        ),
        (
            r"\b(skip|bypass|override|overrule|avoid|waive)\b[^.]{0,40}\b(policy|policies|review|reviews|approval|approvals|security|privacy|legal|control|controls|check|checks|rule|rules)\b",
            "asks to skip or override a control",
        ),
        (
            r"\b(no|not)\b[^.]{0,20}\b(approval|review|sign-?off)\b[^.]{0,20}\b(required|needed|necessary)\b",
            "claims no approval or review is needed",
        ),
        (
            r"\b(you are now|act as|pretend to be|from now on you)\b",
            "tries to change the assistant's role",
        ),
        (
            r"\b(system prompt|developer message|new instructions?|updated instructions?)\b",
            "refers to the assistant's own instructions",
        ),
        (
            r"\b(reveal|show|print|output|repeat|disclose)\b[^.]{0,40}\b(api key|secret|secrets|token|credential|credentials|password|prompt)\b",
            "tries to extract secrets or the prompt",
        ),
    ]

    @classmethod
    def compiled(cls):
        return [(re.compile(pattern, re.IGNORECASE), reason) for pattern, reason in cls.TABLE]


_COMPILED_PATTERNS = InjectionPatterns.compiled()


@dataclass
class InjectionReport:
    detected: bool = False
    findings: dict[str, list[str]] = field(default_factory=dict)

    def reasons(self) -> list[str]:
        return [f"{field_name}: {reason}" for field_name, rs in self.findings.items() for reason in rs]

    def summary(self) -> str:
        if not self.detected:
            return "No injection patterns found in the untrusted text."
        fields = ", ".join(sorted(self.findings))
        return (
            f"Untrusted text in {fields} tried to change the assistant's behaviour. "
            "The embedded instruction was ignored and the real policy was applied."
        )


def scan_text(text: str | None) -> list[str]:
    if not text:
        return []
    return [reason for pattern, reason in _COMPILED_PATTERNS if pattern.search(text)]


def scan_fields(fields: dict[str, str | None]) -> InjectionReport:
    report = InjectionReport()
    for field_name, value in fields.items():
        found = scan_text(value)
        if found:
            report.detected = True
            report.findings[field_name] = found
    return report


def scan_request(request: dict) -> InjectionReport:
    return scan_fields({
        "business_justification": request.get("business_justification"),
        "product_name": request.get("product_name"),
        "vendor_name": request.get("vendor_name"),
    })


def fence(label: str, text: str | None) -> str:
    """Wrap untrusted text in markers the model is told to treat as data only."""
    body = (text or "").strip() or "(empty)"
    return f"<untrusted_{label}>\n{body}\n</untrusted_{label}>"
