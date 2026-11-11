"""Model tiers (design §8c, onboarding-interfaces.md §8-§9): which model, fallbacks, thinking budget and effort an
agent run gets, by tier and role. Pure functions; `agents.build_run_plan` applies the result to the agent spec.

* The platform catalogue is the base bundle's `model_tiers` policy (`tiers: {<tier>: {<role>: params | ref}}`).
  A string value is a model ID when it contains `/` (`small`), otherwise a reference to another role of the same tier
  (`implementer`) or to `<tier>.<role>`.
* The project's choice is the derived bundle's `model_tier_selection` policy (`default_tier`, `workflows`,
  `escalation`); the base bundle carries the default below.
* Escalation (implementer and repair only): to `thorough` on repair cycle >= 2, risk >= high, or after a failed
  attempt. The workflow passes the facts (behind `workflow.patched("model-tiers-v1")`); the rule lives here.
* The qa role runs replicant-qa and uses no model.
"""
from __future__ import annotations

import copy
from typing import Any

OPUS, SONNET, HAIKU = "anthropic/claude-opus-5-5", "anthropic/claude-sonnet-5-5", "anthropic/claude-haiku-5-5"
DEFAULT_CATALOG: dict[str, Any] = {"tiers": {  # onboarding-interfaces.md §9, verbatim
    "standard": {
        "planner": {"model": OPUS, "fallbacks": [SONNET], "thinkingTokens": 16000, "reasoningEffort": "high"},
        "implementer": {"model": SONNET, "fallbacks": [OPUS], "thinkingTokens": 8000, "reasoningEffort": "medium"},
        "repair": "implementer",
        "reviewer": {"model": OPUS, "fallbacks": [SONNET], "thinkingTokens": 16000, "reasoningEffort": "high"},
        "scanner": {"model": SONNET, "thinkingTokens": 8000, "reasoningEffort": "medium"},
        "small": HAIKU},
    "thorough": {
        "planner": "standard.planner",
        "implementer": {"model": OPUS, "fallbacks": [SONNET], "thinkingTokens": 32000, "reasoningEffort": "high"},
        "repair": "implementer", "reviewer": "standard.reviewer", "scanner": "standard.scanner",
        "small": HAIKU}}}
DEFAULT_SELECTION: dict[str, Any] = {  # onboarding-interfaces.md §8 (the base bundle's default selection)
    "default_tier": "standard", "workflows": {},
    "escalation": {"repair_cycle_at_least": 2, "risk_at_least": "high", "after_failed_attempt": True}}
ESCALATE_TO = "thorough"
ESCALATING_ROLES = ("implementer", "repair")
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
EFFORTS = ("low", "medium", "high", "xhigh", "max")  # run-plan reasoningEffort (CLI --effort)


def tier_role(role: str, agent_name: str = "") -> str | None:
    """The catalogue role of a run: the run role, except the repo scanner (a planner-shaped agent). qa: none."""
    if role == "qa":
        return None
    if agent_name.startswith("scanner"):
        return "scanner"
    return role if role in ("planner", "implementer", "repair", "reviewer") else None


def _resolve(tiers: dict[str, Any], tier: str, role: str, seen: tuple[str, ...] = ()) -> dict[str, Any] | None:
    key = f"{tier}.{role}"
    if key in seen or len(seen) > 8:
        raise ValueError(f"model_tiers: reference cycle at {key}")
    v = (tiers.get(tier) or {}).get(role)
    if v is None:
        return None
    if isinstance(v, str):
        if "/" in v:
            return {"model": v}
        t, _, r = v.rpartition(".")
        return _resolve(tiers, t or tier, r, seen + (key,))
    return dict(v)


def params_for(catalog: dict[str, Any], tier: str, role: str) -> dict[str, Any] | None:
    """{model, fallbacks?, thinkingTokens?, reasoningEffort?, small?} for a tier and catalogue role, references
    resolved; None when the tier or role is not in the catalogue."""
    tiers = (catalog or {}).get("tiers") or {}
    p = _resolve(tiers, tier, role)
    if not p or not p.get("model"):
        return None
    small = _resolve(tiers, tier, "small")
    if small and small.get("model"):
        p["small"] = small["model"]
    return p


def select_tier(selection: dict[str, Any] | None, catalog: dict[str, Any], *, workflow_type: str, role: str,
                facts: dict[str, Any] | None) -> tuple[str, str | None]:
    """(tier, escalation reason or None). The project's per-workflow tier, else its default tier, else `standard`;
    unknown tier names fall back. Implementer and repair escalate to `thorough` per the selection's rule."""
    sel = {**DEFAULT_SELECTION, **(selection or {})}
    known = set(((catalog or {}).get("tiers") or {}).keys())
    tier = (sel.get("workflows") or {}).get(workflow_type) or sel.get("default_tier") or "standard"
    if tier not in known:
        tier = sel.get("default_tier") if sel.get("default_tier") in known else "standard"
    if role not in ESCALATING_ROLES or tier == ESCALATE_TO or ESCALATE_TO not in known:
        return tier, None
    esc = {**DEFAULT_SELECTION["escalation"], **(sel.get("escalation") or {})}
    f = facts or {}
    if esc.get("repair_cycle_at_least") is not None and int(f.get("repair_cycle") or 0) >= int(esc["repair_cycle_at_least"]):
        return ESCALATE_TO, "repair_cycle"
    if esc.get("risk_at_least") and RISK_ORDER.get(str(f.get("risk") or "low"), 0) >= RISK_ORDER.get(esc["risk_at_least"], 9):
        return ESCALATE_TO, "risk"
    if esc.get("after_failed_attempt") and f.get("failed_attempt"):
        return ESCALATE_TO, "failed_attempt"
    return tier, None


def apply(spec: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """A copy of the agent spec with the tier's model, fallbacks, small model, thinking budget and effort."""
    out = copy.deepcopy(spec)
    m = out.setdefault("model", {})
    m["primary"] = params["model"]
    if "fallbacks" in params:
        m["fallbacks"] = [x for x in params["fallbacks"] if x != params["model"]]
    if params.get("small"):
        m["small"] = params["small"]
    p = m.setdefault("parameters", {})
    if params.get("thinkingTokens"):
        p["thinkingTokens"] = int(params["thinkingTokens"])
    if params.get("reasoningEffort") in EFFORTS:
        p["reasoningEffort"] = params["reasoningEffort"]
    return out


def resolve(policies: dict[str, Any], *, role: str, agent_name: str, workflow_type: str,
            facts: dict[str, Any] | None) -> dict[str, Any] | None:
    """The tier decision for one run: {tier, role, escalated, params} or None (qa, or no catalogue entry)."""
    trole = tier_role(role, agent_name)
    if trole is None:
        return None
    catalog = policies.get("model_tiers") or DEFAULT_CATALOG
    tier, why = select_tier(policies.get("model_tier_selection"), catalog, workflow_type=workflow_type,
                            role=role, facts=facts)
    params = params_for(catalog, tier, trole)
    if params is None:
        return None
    return {"tier": tier, "role": trole, "escalated": why, "params": params}
