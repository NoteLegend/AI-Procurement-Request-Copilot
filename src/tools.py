"""8 tools the agent can call, registered through a small @tool decorator
instead of a hand-maintained dict, so a new tool only has to be written once.

7 of the 8 are plain functions over local data. Only vendor_risk_tool makes a
network call. Every call is logged in Toolbox, so evidence can always be
traced back to the tool that produced it.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

import pandas as pd

from src import data_access, guard, policy_index
from src.config import SECURITY_REVIEW_VALID_DAYS, SNAPSHOT_DATE
from src.vendor_client import fetch_vendor_risk

_REGISTRY: dict[str, Callable[..., dict]] = {}


def tool(name: str):
    """Register a function under `name` so Toolbox.run(name, ...) can find it.

    The registry is keyed on the same string the model sees in TOOL_SCHEMAS,
    so the schema list below stays the single source of truth for what the
    model is told about each tool; this decorator only wires the name to an
    implementation.
    """
    def decorator(fn: Callable[..., dict]) -> Callable[..., dict]:
        _REGISTRY[name] = fn
        return fn
    return decorator


def _clean(value):
    """Make a pandas/NumPy value JSON-safe."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, str):
        return value.strip()
    return value


def _row(series: pd.Series) -> dict:
    return {key: _clean(value) for key, value in series.to_dict().items()}


def _norm(value) -> str:
    return str(value or "").strip().lower()


# --------------------------------------------------------------- tool bodies

@tool("lookup_requester")
def lookup_requester(employee_id: str) -> dict:
    """Who asked, which department, and who their manager is."""
    employees = data_access.load_employees()
    match = employees[employees["employee_id"] == str(employee_id).strip()]
    if match.empty:
        return {"found": False, "employee_id": employee_id}
    record = _row(match.iloc[0])
    manager = employees[employees["employee_id"] == record.get("manager_id")]
    record["manager_name"] = _clean(manager.iloc[0]["name"]) if not manager.empty else None
    record["found"] = True
    return record


@tool("check_budget")
def check_budget(department: str, annual_cost_usd: float | None = None) -> dict:
    """Compare a cost against the department's available software budget (policy 2)."""
    budgets = data_access.load_budgets()
    match = budgets[budgets["department"].str.strip().str.lower() == _norm(department)]
    if match.empty:
        return {"found": False, "department": department}

    record = _row(match.iloc[0])
    available = float(record["available_usd"])
    result = {
        "found": True,
        "department": record["department"],
        "annual_software_budget_usd": float(record["annual_software_budget_usd"]),
        "committed_usd": float(record["committed_usd"]),
        "available_usd": available,
    }
    if annual_cost_usd is None:
        result["within_budget"] = None
        result["note"] = "No annual cost was given, so the budget could not be compared."
        return result

    cost = float(annual_cost_usd)
    result["annual_cost_usd"] = cost
    result["within_budget"] = cost <= available
    result["shortfall_usd"] = round(max(0.0, cost - available), 2)
    result["remaining_after_purchase_usd"] = round(available - cost, 2)
    return result


_MATCH_PRIORITY = {"same_product_and_vendor": 0, "same_product_name": 1, "same_vendor": 2, "same_category": 3}


@tool("search_software_catalog")
def search_software_catalog(
    category: str | None = None,
    vendor_name: str | None = None,
    product_name: str | None = None,
) -> dict:
    """Find approved software that overlaps with the request (policy 3)."""
    catalog = data_access.load_software_catalog()
    wanted_category, wanted_vendor, wanted_product = _norm(category), _norm(vendor_name), _norm(product_name)

    matches = []
    for _, row in catalog.iterrows():
        record = _row(row)
        row_vendor, row_category, row_product = _norm(record["vendor_name"]), _norm(record["category"]), _norm(record["product_name"])
        vendor_hit = bool(wanted_vendor) and row_vendor == wanted_vendor
        category_hit = bool(wanted_category) and row_category == wanted_category
        # "TaskFlow Pro" should match catalogue entry "TaskFlow" and vice versa
        product_hit = bool(wanted_product) and (row_product in wanted_product or wanted_product in row_product)
        if not (vendor_hit or category_hit or product_hit):
            continue
        if product_hit and vendor_hit:
            record["match_type"] = "same_product_and_vendor"
        elif vendor_hit:
            record["match_type"] = "same_vendor"
        elif product_hit:
            record["match_type"] = "same_product_name"
        else:
            record["match_type"] = "same_category"
        matches.append(record)

    matches.sort(key=lambda item: _MATCH_PRIORITY[item["match_type"]])
    return {
        "query": {"category": category, "vendor_name": vendor_name, "product_name": product_name},
        "match_count": len(matches),
        "matches": matches,
        "strongest_match_type": matches[0]["match_type"] if matches else None,
    }


@tool("lookup_vendor_registry")
def lookup_vendor_registry(vendor_name: str) -> dict:
    """Internal vendor registry, plus how old the security review is (policy 5).
    Age is measured against the policy snapshot date, never today's date.
    """
    vendors = data_access.load_vendors()
    match = vendors[vendors["vendor_name"].str.strip().str.lower() == _norm(vendor_name)]
    if match.empty:
        return {"found": False, "vendor_name": vendor_name, "note": "Vendor is not in the internal registry."}

    record = _row(match.iloc[0])
    record["found"] = True
    record["reference_date"] = SNAPSHOT_DATE.isoformat()

    review_date = record.get("security_review_date")
    if not review_date:
        record["security_review_age_days"] = None
        record["security_review_current"] = False
        record["security_review_state"] = "missing"
    else:
        age = (SNAPSHOT_DATE - date.fromisoformat(str(review_date))).days
        record["security_review_age_days"] = age
        record["security_review_current"] = age <= SECURITY_REVIEW_VALID_DAYS
        record["security_review_state"] = "current" if age <= SECURITY_REVIEW_VALID_DAYS else "expired"

    record["is_new_vendor"] = _norm(record.get("procurement_status")) == "new"
    return record


@tool("vendor_risk_tool")
def vendor_risk_tool(vendor_name: str) -> dict:
    """External vendor-risk service. The only tool that makes a network call."""
    data = fetch_vendor_risk(vendor_name)
    if data.get("available") and data.get("last_review_date"):
        data["last_review_age_days"] = (SNAPSHOT_DATE - date.fromisoformat(str(data["last_review_date"]))).days
    return data


@tool("get_purchase_history")
def get_purchase_history(vendor_name: str | None = None, department: str | None = None) -> dict:
    """Past purchases, used to spot renewals and existing spend."""
    rows = [_row(row) for _, row in data_access.load_purchase_history().iterrows()]
    if vendor_name:
        rows = [r for r in rows if _norm(r["vendor_name"]) == _norm(vendor_name)]
    if department:
        rows = [r for r in rows if _norm(r["department"]) == _norm(department)]
    return {"query": {"vendor_name": vendor_name, "department": department}, "match_count": len(rows), "purchases": rows}


@tool("scan_request_text")
def scan_request_text(request_id: str) -> dict:
    """Check a request's free text for instructions aimed at this assistant (policy 9)."""
    request = data_access.get_request(request_id)
    report = guard.scan_request(request)
    return {
        "request_id": request_id,
        "detected": report.detected,
        "findings": {name: list(reasons) for name, reasons in report.findings.items()},
        "reasons": report.reasons(),
        "summary": report.summary(),
    }


@tool("lookup_policy")
def lookup_policy(topic: str) -> dict:
    """Read a section out of the procurement policy (the source of truth)."""
    section = policy_index.get_topic(topic)
    if section is None:
        hits = policy_index.search(topic)
        if not hits:
            return {"found": False, "topic": topic, "available_topics": sorted(policy_index.TOPICS)}
        section = hits[0]
    return {
        "found": True,
        "topic": topic,
        "section": section["number"],
        "title": section["title"],
        "reference": f"Policy section {section['number']}",
        "text": section["body"],
    }


# ------------------------------------------------------- schemas for the model

TOOL_SCHEMAS: list[dict] = [
    {"type": "function", "function": {
        "name": "lookup_requester",
        "description": "Look up the person who raised the request: their department, level, country and manager.",
        "parameters": {"type": "object", "properties": {
            "employee_id": {"type": "string", "description": "Employee id, for example E004"}},
            "required": ["employee_id"]},
    }},
    {"type": "function", "function": {
        "name": "check_budget",
        "description": "Compare an annual cost against a department's available software budget. Deterministic.",
        "parameters": {"type": "object", "properties": {
            "department": {"type": "string"},
            "annual_cost_usd": {"type": ["number", "null"], "description": "Annual cost, or null if unknown"}},
            "required": ["department"]},
    }},
    {"type": "function", "function": {
        "name": "search_software_catalog",
        "description": "Search approved internal software for an overlapping product, vendor or category.",
        "parameters": {"type": "object", "properties": {
            "category": {"type": "string"}, "vendor_name": {"type": "string"}, "product_name": {"type": "string"}}},
    }},
    {"type": "function", "function": {
        "name": "lookup_vendor_registry",
        "description": "Internal vendor registry record: procurement status, security status, legal terms, and whether the security review is still current at the policy reference date.",
        "parameters": {"type": "object", "properties": {"vendor_name": {"type": "string"}}, "required": ["vendor_name"]},
    }},
    {"type": "function", "function": {
        "name": "vendor_risk_tool",
        "description": "External vendor-risk service: risk level, security review status, whether personal data is processed, and whether data is stored outside the region. May be unavailable.",
        "parameters": {"type": "object", "properties": {"vendor_name": {"type": "string"}}, "required": ["vendor_name"]},
    }},
    {"type": "function", "function": {
        "name": "get_purchase_history",
        "description": "Past purchases, filtered by vendor and/or department.",
        "parameters": {"type": "object", "properties": {"vendor_name": {"type": "string"}, "department": {"type": "string"}}},
    }},
    {"type": "function", "function": {
        "name": "scan_request_text",
        "description": "Check the free text of a request for instructions aimed at this assistant. Deterministic.",
        "parameters": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]},
    }},
    {"type": "function", "function": {
        "name": "lookup_policy",
        "description": (
            "Read a section of the procurement policy. Topics: required_information, budget, "
            "existing_software, approval_thresholds, security, privacy, legal, ai_tools, "
            "untrusted_content, tool_failures, human_authority."
        ),
        "parameters": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]},
    }},
]

# Only one registered tool leaves the process; the rest are pure code over local data.
DETERMINISTIC_TOOLS = {name for name in _REGISTRY if name != "vendor_risk_tool"}


@dataclass
class ToolCallRecord:
    name: str
    arguments: dict
    result: dict
    ok: bool
    duration_ms: float


@dataclass
class Toolbox:
    """Runs registered tools and keeps a record of everything that happened.

    The record is what makes the grounding check possible: an evidence item is
    only allowed to name a tool that appears in this log.
    """

    calls: list[ToolCallRecord] = field(default_factory=list)
    latest: dict[str, dict] = field(default_factory=dict)

    def run(self, name: str, arguments: dict | None = None) -> dict:
        arguments = arguments or {}
        fn = _REGISTRY.get(name)
        started = time.perf_counter()
        if fn is None:
            result, ok = {"error": f"Unknown tool: {name}"}, False
        else:
            try:
                result, ok = fn(**arguments), True
            except TypeError as exc:
                result, ok = {"error": f"Bad arguments for {name}: {exc}"}, False
            except Exception as exc:
                result, ok = {"error": f"{name} failed: {type(exc).__name__}: {exc}"}, False

        duration_ms = (time.perf_counter() - started) * 1000
        self.calls.append(ToolCallRecord(name, arguments, result, ok, duration_ms))
        if ok:
            self.latest[name] = result
        return result

    @property
    def tool_names(self) -> list[str]:
        return [call.name for call in self.calls]

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def was_called(self, name: str) -> bool:
        return name in {call.name for call in self.calls}
