"""Deterministic policy engine - the "code" layer, organised as a small class
instead of loose functions passing an outcome object around by hand.

Every rule here comes from data/procurement_policy.md, in plain Python, so it
gives the same answer every run. Approvals come only from this module; the
model can make the action stricter and add a flag from a fixed list, nothing
more.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.config import SNAPSHOT_DATE
from src.contracts import EvidenceItem
from src.tools import Toolbox

SENSITIVE_DATA_LEVELS = {
    "source_code", "confidential_documents", "employee_pii",
    "customer_pii", "credentials", "secrets",
}
PII_DATA_LEVELS = {"employee_pii", "customer_pii"}

INTEGRATION_SECURITY_KEYWORDS = ("production", "cloud account", "git repositor", "source code", "codebase")

APPROVAL_ORDER = ["Manager", "Department Head", "Finance", "CFO", "Procurement", "Security", "Privacy", "Legal"]

# weakest first - the model can only move up this list, never down
ACTIONS = [
    "proceed_to_approval",
    "review_existing_tool_first",
    "route_for_reviews",
    "request_clarification",
    "hold_do_not_proceed",
]

# closed vocabulary - a free-text flag field let the model invent noise
EXTRA_RISK_FLAGS = [
    "unclear_business_justification",
    "unused_existing_capacity",
    "duplicate_purchase_risk",
    "urgency_pressure",
    "scope_creep_risk",
    "renewal_timing_risk",
    "vendor_concentration_risk",
]

ACTION_LABELS = {
    "proceed_to_approval": "Proceed to the standard approval chain",
    "review_existing_tool_first": "Review the existing tool before buying anything new",
    "route_for_reviews": "Route for the required reviews before any approval",
    "request_clarification": "Request clarification from the requester before review",
    "hold_do_not_proceed": "Hold the request and do not proceed",
}


def action_rank(action: str) -> int:
    try:
        return ACTIONS.index(action)
    except ValueError:
        return 0


def _norm(value) -> str:
    return str(value or "").strip().lower()


def _security_claim(status: str) -> str:
    """Turn a security status string into approved / not_approved / unknown."""
    if status == "approved":
        return "approved"
    if status in {"pending", "expired", "not_completed", "rejected", "denied"}:
        return "not_approved"
    return "unknown"


@dataclass
class RuleOutcome:
    required_approvals: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    minimum_action: str = "proceed_to_approval"
    human_review_required: bool = True
    strongest_overlap: str | None = None

    def add_flag(self, flag: str) -> None:
        if flag not in self.risk_flags:
            self.risk_flags.append(flag)

    def add_approval(self, approval: str) -> None:
        if approval not in self.required_approvals:
            self.required_approvals.append(approval)

    def raise_action(self, action: str) -> None:
        if action_rank(action) > action_rank(self.minimum_action):
            self.minimum_action = action

    def sorted_approvals(self) -> list[str]:
        known = [a for a in APPROVAL_ORDER if a in self.required_approvals]
        extra = [a for a in self.required_approvals if a not in APPROVAL_ORDER]
        return known + extra


def approvals_for_amount(annual_cost_usd: float) -> list[str]:
    """Policy 4 threshold table."""
    if annual_cost_usd <= 1000:
        return ["Manager"]
    if annual_cost_usd <= 10000:
        return ["Department Head", "Procurement"]
    if annual_cost_usd <= 25000:
        return ["Department Head", "Finance", "Procurement"]
    return ["Department Head", "Finance", "CFO", "Procurement"]


class PolicyEngine:
    """Runs every deterministic policy check for one request.

    Each ``_check_*`` method reads whatever tool results it needs from
    ``self.results`` and writes its findings onto ``self.outcome``. Grouping
    them as methods on one object - instead of threading the outcome through a
    chain of module-level functions - keeps the per-request state (the
    request dict, the tool results, the toolbox) in one place for the whole
    evaluation.
    """

    def __init__(self, request: dict, toolbox: Toolbox):
        self.request = request
        self.toolbox = toolbox
        self.results = toolbox.latest
        self.outcome = RuleOutcome()

    def evaluate(self) -> RuleOutcome:
        injection = self.results.get("scan_request_text") or {}
        requester = self.results.get("lookup_requester")

        self._check_injection(injection)
        self._check_required_fields(requester, injection)

        if requester and requester.get("found"):
            self._describe_requester(requester)

        self._check_budget(self.results.get("check_budget"))
        self._check_overlap(self.results.get("search_software_catalog"))
        assessment_ok, is_new = self._check_vendor(
            self.results.get("lookup_vendor_registry"), self.results.get("vendor_risk_tool")
        )
        self._check_security_privacy_legal(
            self.results.get("lookup_vendor_registry"), self.results.get("vendor_risk_tool"), assessment_ok, is_new
        )
        self._check_thresholds()

        self.outcome.human_review_required = True  # policy 11 - no path here ever approves anything
        return self.outcome

    def _policy_evidence(self, topic: str, finding: str) -> EvidenceItem:
        section = self.toolbox.run("lookup_policy", {"topic": topic})
        reference = section.get("reference") if section.get("found") else f"Policy topic {topic}"
        return EvidenceItem(source="lookup_policy", finding=finding, reference=reference)

    def _check_injection(self, injection: dict) -> None:
        """Policy 9."""
        if not injection.get("detected"):
            return
        self.outcome.add_flag("prompt_injection_detected")
        self.outcome.raise_action("request_clarification")
        self.outcome.evidence.append(EvidenceItem(
            source="scan_request_text",
            finding=injection.get("summary", "") + " Detected: " + "; ".join(injection.get("reasons", [])) + ".",
            reference="Policy section 9",
        ))
        self.outcome.facts.append(
            "The request text tried to instruct this assistant. It was ignored. "
            "Nothing in the request text can grant an approval."
        )

    def _check_required_fields(self, requester: dict | None, injection: dict) -> None:
        """Policy 1."""
        out, req = self.outcome, self.request
        if requester is None or not requester.get("found"):
            out.missing_information.append("A valid requester record for this employee id")
        if not _norm(req.get("product_name")):
            out.missing_information.append("The product name")
        if not _norm(req.get("vendor_name")):
            out.missing_information.append("The vendor name")
        if req.get("annual_cost_usd") is None:
            out.missing_information.append("The annual cost in USD, or a reasonable annual estimate")
        if req.get("user_count") is None:
            out.missing_information.append("The number of users or licences needed")

        data_level = _norm(req.get("data_access_level"))
        if not data_level or data_level in {"unknown", "tbd", "not sure"}:
            out.missing_information.append("The intended data access level for this tool")

        justification = _norm(req.get("business_justification"))
        if not justification:
            out.missing_information.append("A business purpose for the request")
        elif "business_justification" in (injection.get("findings") or {}):
            out.missing_information.append(
                "A business purpose that can be trusted. The justification text contained "
                "instructions aimed at this assistant, so it was not used as a reason to buy."
            )

        if req.get("requested_integrations", []) is None:
            out.missing_information.append("The integrations this tool would need")

        if out.missing_information:
            out.add_flag("missing_information")
            out.raise_action("request_clarification")

    def _describe_requester(self, requester: dict) -> None:
        self.outcome.evidence.append(EvidenceItem(
            source="lookup_requester",
            finding=(
                f"{requester['name']} ({requester['level']}, {requester['country']}) raised this request "
                f"for the {requester['department']} department. Manager: {requester.get('manager_name') or 'unknown'}."
            ),
            reference=requester["employee_id"],
        ))
        self.outcome.facts.append(f"Requester is in the {requester['department']} department.")

    def _check_budget(self, budget: dict | None) -> None:
        """Policy 2."""
        out, cost = self.outcome, self.request.get("annual_cost_usd")
        if budget is None or not budget.get("found"):
            out.facts.append("Budget could not be checked: no budget record for this department.")
            out.add_flag("budget_unverified")
            out.raise_action("route_for_reviews")
            return

        available, dept = budget["available_usd"], budget["department"]
        if cost is None:
            out.evidence.append(EvidenceItem(
                source="check_budget",
                finding=f"{dept} has ${available:,.0f} of software budget still available, "
                        "but the request has no annual cost so the comparison could not be made.",
                reference="department_budgets.csv",
            ))
            out.facts.append(f"{dept} available software budget is ${available:,.0f}. Request cost is unknown.")
            return

        if budget["within_budget"]:
            left = budget["remaining_after_purchase_usd"]
            out.evidence.append(EvidenceItem(
                source="check_budget",
                finding=f"Annual cost ${cost:,.0f} fits inside {dept}'s available software budget of "
                        f"${available:,.0f}. ${left:,.0f} would be left.",
                reference="department_budgets.csv",
            ))
            out.facts.append(
                f"Budget check passed: ${cost:,.0f} against ${available:,.0f} available. "
                "A pass here is not by itself a reason to approve."
            )
        else:
            gap = budget["shortfall_usd"]
            out.evidence.append(EvidenceItem(
                source="check_budget",
                finding=f"Annual cost ${cost:,.0f} is more than {dept}'s available software budget of "
                        f"${available:,.0f}. The shortfall is ${gap:,.0f}.",
                reference="department_budgets.csv",
            ))
            out.facts.append(f"Budget shortfall of ${gap:,.0f}.")
            out.add_flag("budget_insufficient")
            out.add_approval("Finance")
            out.raise_action("route_for_reviews")

    def _check_overlap(self, catalog: dict | None) -> None:
        """Policy 3."""
        out, req = self.outcome, self.request
        if catalog is None:
            return
        matches = catalog.get("matches", [])
        if not matches:
            out.evidence.append(EvidenceItem(
                source="search_software_catalog",
                finding="No approved tool in the catalogue covers this product, vendor or category.",
                reference="software_catalog.csv",
            ))
            out.facts.append("No existing approved tool overlaps with this request.")
            return

        out.strongest_overlap = catalog.get("strongest_match_type")
        top = matches[0]
        rest = ", ".join(m["product_name"] for m in matches[1:4])
        finding = (
            f"{top['product_name']} ({top['vendor_name']}, {top['category']}) is already approved "
            f"with {top['licensed_seats']} seats at ${top['annual_cost_usd']:,.0f} a year, "
            f"scope {top['scope']}. Match type: {top['match_type'].replace('_', ' ')}."
        )
        if rest:
            finding += f" Other tools in the same space: {rest}."
        out.evidence.append(EvidenceItem(source="search_software_catalog", finding=finding, reference=top["software_id"]))
        out.facts.append(
            f"Existing approved tool found: {top['product_name']} ({top['match_type']}), "
            f"{top['licensed_seats']} seats, scope {top['scope']}, status {top['status']}."
        )
        out.add_flag("existing_tool_overlap")
        out.raise_action("review_existing_tool_first")

        # policy 8 - a tool approved for limited use isn't cleared for a more sensitive data class
        data_level = _norm(req.get("data_access_level"))
        if "limited" in _norm(top.get("status")) and data_level in SENSITIVE_DATA_LEVELS:
            out.evidence.append(self._policy_evidence(
                "ai_tools",
                f"{top['product_name']} is approved for limited use only. A previous purchase does not "
                f"authorise a new data class, and this request asks for {data_level.replace('_', ' ')}.",
            ))
            out.facts.append("The existing tool is approved for limited use only, so this data class is not covered.")
            out.add_flag("security_review_required")
            out.add_approval("Security")
            out.raise_action("route_for_reviews")

    def _check_vendor(self, registry: dict | None, risk: dict | None) -> tuple[bool, bool]:
        """Policy 5, 6, 7, 10 for the vendor side. Returns (assessment_ok, is_new)."""
        out, req = self.outcome, self.request
        ok, is_new = True, False

        if registry is not None and registry.get("found"):
            is_new = bool(registry.get("is_new_vendor"))
            state = registry.get("security_review_state")
            status = _norm(registry.get("security_status"))
            finding = (
                f"Internal registry: procurement status {registry.get('procurement_status')}, "
                f"security status {registry.get('security_status') or 'unknown'}, "
                f"legal terms {registry.get('legal_terms_status') or 'unknown'}."
            )
            if state == "current":
                finding += f" Security review dated {registry['security_review_date']} is still current ({registry['security_review_age_days']} days old at {SNAPSHOT_DATE})."
            elif state == "expired":
                finding += f" Security review dated {registry['security_review_date']} is {registry['security_review_age_days']} days old, past the 365-day limit at {SNAPSHOT_DATE}."
            else:
                finding += " No security review date is recorded."
            out.evidence.append(EvidenceItem(source="lookup_vendor_registry", finding=finding, reference=registry.get("vendor_id")))
            out.facts.append(f"Registry security review state: {state}; status {status or 'unknown'}.")

            if state == "expired":
                ok = False
                out.add_flag("vendor_review_expired")
            if state == "missing" or status != "approved":
                ok = False
        else:
            out.evidence.append(EvidenceItem(
                source="lookup_vendor_registry",
                finding=f"{req.get('vendor_name')} is not in the internal vendor registry.",
                reference="vendors.csv",
            ))
            out.facts.append("Vendor is not in the internal registry.")
            ok, is_new = False, True

        if risk is None:
            # no result at all still counts as missing evidence (policy 10)
            out.evidence.append(EvidenceItem(
                source="lookup_vendor_registry",
                finding=f"No result was returned by the external vendor-risk check for {req.get('vendor_name')}, "
                        "so the vendor's risk level is unverified.",
                reference="vendors.csv",
            ))
            out.facts.append("External vendor-risk check produced no result; risk level unverified.")
            out.add_flag("vendor_risk_unavailable")
            out.raise_action("route_for_reviews")
            return False, is_new

        if not risk.get("available"):
            out.evidence.append(EvidenceItem(
                source="vendor_risk_tool",
                finding=f"The external vendor-risk service could not be reached for {req.get('vendor_name')} "
                        f"({risk.get('reason')}). {risk.get('detail', '')} Vendor risk is therefore unverified.",
                reference="GET /vendor-risk",
            ))
            out.facts.append("External vendor-risk check failed, so the vendor's risk level is unverified. Do not assume it is low.")
            out.add_flag("vendor_risk_unavailable")
            ok = False
            out.raise_action("route_for_reviews")
            return ok, is_new

        api_status = _norm(risk.get("security_review_status"))
        out.evidence.append(EvidenceItem(
            source="vendor_risk_tool",
            finding=(
                f"External risk service: risk level {risk.get('risk_level')}, security review "
                f"{risk.get('security_review_status')}, processes personal data "
                f"{bool(risk.get('processes_personal_data'))}, stores data outside region "
                f"{bool(risk.get('stores_data_outside_region'))}. {risk.get('notes', '')}"
            ).strip(),
            reference="GET /vendor-risk",
        ))
        out.facts.append(
            f"External risk level {risk.get('risk_level')}, review status {api_status}, "
            f"stores data outside region {bool(risk.get('stores_data_outside_region'))}."
        )

        if api_status in {"expired", "not_completed", "unknown", ""}:
            ok = False
        if api_status == "expired":
            out.add_flag("vendor_review_expired")
        if _norm(risk.get("risk_level")) == "high":
            ok = False
            out.facts.append("Vendor is rated high risk by the external service.")

        # policy 5: compare what each source *claims*, not what we worked out -
        # a registry that still says Approved while the API says expired is the disagreement itself
        if registry is not None and registry.get("found"):
            registry_claim = _security_claim(_norm(registry.get("security_status")))
            api_claim = _security_claim(api_status)
            dates_disagree = bool(
                registry.get("security_review_date") and risk.get("last_review_date")
                and str(registry["security_review_date"]) != str(risk["last_review_date"])
            )
            claims_disagree = registry_claim != "unknown" and api_claim != "unknown" and registry_claim != api_claim
            if claims_disagree or dates_disagree:
                out.evidence.append(EvidenceItem(
                    source="lookup_vendor_registry",
                    finding=(
                        f"The internal registry and the external risk service disagree about {req.get('vendor_name')}. "
                        f"Registry: {registry.get('security_status')} / {registry.get('security_review_state')}. "
                        f"External service: {risk.get('security_review_status')}. "
                        "Policy section 5 says this must go to a human rather than picking one source."
                    ),
                    reference="Policy section 5",
                ))
                out.facts.append("Internal registry and external risk service disagree on vendor security.")
                out.add_flag("conflicting_vendor_evidence")
                ok = False
                out.raise_action("route_for_reviews")

        return ok, is_new

    def _check_security_privacy_legal(
        self, registry: dict | None, risk: dict | None, assessment_ok: bool, is_new: bool
    ) -> None:
        """Policy 5, 6, 7."""
        out, req = self.outcome, self.request
        data_level = _norm(req.get("data_access_level"))
        integrations = " ".join(str(i) for i in (req.get("requested_integrations") or [])).lower()

        sec_reasons = []
        if data_level in SENSITIVE_DATA_LEVELS or data_level.startswith("production"):
            sec_reasons.append(f"the tool would access {data_level.replace('_', ' ')}")
        hit = next((k for k in INTEGRATION_SECURITY_KEYWORDS if k in integrations), None)
        if hit:
            sec_reasons.append(f"the requested integration touches {hit}")
        if not assessment_ok:
            sec_reasons.append("the vendor security assessment is missing, expired, unverified or disputed")
        if sec_reasons:
            out.add_flag("security_review_required")
            out.add_approval("Security")
            out.raise_action("route_for_reviews")
            out.evidence.append(self._policy_evidence("security", "Security review is required because " + "; ".join(sec_reasons) + "."))

        stores_outside = bool(risk.get("stores_data_outside_region")) if (risk and risk.get("available")) else False
        priv_reasons = []
        if data_level in PII_DATA_LEVELS:
            priv_reasons.append(f"the tool would process {data_level.replace('_', ' ')}")
        if stores_outside and data_level not in {"none", ""}:
            priv_reasons.append("the vendor stores data outside the operating region")
        if priv_reasons:
            out.add_flag("privacy_review_required")
            out.add_approval("Privacy")
            out.raise_action("route_for_reviews")
            out.evidence.append(self._policy_evidence("privacy", "Privacy review is required because " + "; ".join(priv_reasons) + "."))

        cost = req.get("annual_cost_usd")
        legal_terms = _norm(registry.get("legal_terms_status")) if registry and registry.get("found") else "unknown"
        legal_reasons = []
        if is_new and cost is not None and float(cost) >= 10000:
            legal_reasons.append(f"the vendor is new and annual spend is ${float(cost):,.0f}")
        if legal_terms != "approved":
            legal_reasons.append(f"legal terms are not approved yet (status: {legal_terms or 'unknown'})")
        if stores_outside and data_level in PII_DATA_LEVELS:
            legal_reasons.append("personal data would be stored outside the operating region")
        if legal_reasons:
            out.add_flag("legal_review_required")
            out.add_approval("Legal")
            out.raise_action("route_for_reviews")
            out.evidence.append(self._policy_evidence("legal", "Legal review is required because " + "; ".join(legal_reasons) + "."))

    def _check_thresholds(self) -> None:
        """Policy 4."""
        out, cost = self.outcome, self.request.get("annual_cost_usd")
        if cost is None:
            # never invent a tier - send it back through the requester's manager
            for a in ("Manager", "Procurement"):
                out.add_approval(a)
            out.evidence.append(self._policy_evidence(
                "approval_thresholds",
                "The approval tier could not be set because the request has no annual cost. The "
                "threshold table needs an amount, and the policy says to ask rather than invent one.",
            ))
            out.facts.append("Approval tier is undetermined: no annual cost was given.")
            return

        cost = float(cost)
        tier = approvals_for_amount(cost)
        for a in tier:
            out.add_approval(a)
        out.evidence.append(self._policy_evidence(
            "approval_thresholds",
            f"At ${cost:,.0f} a year, the policy threshold table requires: {', '.join(tier)}. "
            "These are minimums and other reviews can be added on top.",
        ))
        out.facts.append(f"Threshold approvals for ${cost:,.0f}: {', '.join(tier)}.")


def apply_rules(request: dict, toolbox: Toolbox) -> RuleOutcome:
    """Module-level entry point kept for the agents and tests - builds a
    PolicyEngine for this one request and runs it.
    """
    return PolicyEngine(request, toolbox).evaluate()
