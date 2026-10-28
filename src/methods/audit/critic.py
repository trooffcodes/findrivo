"""
methods/audit/critic.py
Evidence-based website redesign critic using LLMGateway.
Enforces deterministic qualification and prevents false-positive hallucinations.
"""

import json
import re
from typing import Any, Dict, List, Optional

from core.llm_client import LLMGateway
from core.logger import app_logger, track_performance
from prompts.critic_prompt import CRITIC_SYSTEM_PROMPT

_llm_gateway: Optional[LLMGateway] = None
_VALID_VERDICTS = {"modern", "passable", "outdated", "broken"}


def _get_gateway() -> LLMGateway:
    global _llm_gateway
    if _llm_gateway is None:
        _llm_gateway = LLMGateway()
    return _llm_gateway


def _parse_json(raw_response: str) -> Dict[str, Any]:
    clean = raw_response.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", clean)
    if match:
        clean = match.group(1).strip()

    try:
        result = json.loads(clean)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        start = clean.find("{")
        if start < 0:
            raise ValueError("LLM critic response contained no JSON object.")
        result, _ = decoder.raw_decode(clean[start:])

    if not isinstance(result, dict):
        raise ValueError("Critic response must be a JSON object.")
    return result


def _score(value: Any, default: int = 0) -> int:
    try:
        return max(0, min(100, int(float(value))))
    except (TypeError, ValueError, OverflowError):
        return default


def _normalize_defects(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    defects = []
    for item in value[:3]:
        if isinstance(item, str) and item.strip():
            defects.append(item.strip()[:300])
    return defects


def apply_qualification_rules(result: Dict[str, Any]) -> Dict[str, Any]:
    viability = _score(result.get("viability_score"))
    confidence = _score(result.get("confidence_score"))
    defects = _normalize_defects(result.get("primary_defects"))

    qualified = viability >= 65 and confidence >= 70 and bool(defects)
    verdict = result.get("design_verdict", "passable")
    if verdict not in _VALID_VERDICTS:
        verdict = "passable"

    result.update({
        "viability_score": viability,
        "confidence_score": confidence,
        "design_verdict": verdict,
        "primary_defects": defects,
        "qualified": qualified,
        "needs_redesign": qualified,
    })
    return result


def _failure_result(error: Exception) -> Dict[str, Any]:
    return {
        "viability_score": 30,
        "confidence_score": 50,
        "design_verdict": "passable",
        "primary_defects": [],
        "qualified": False,
        "needs_redesign": False,
        "rejection_reason": f"Critic evaluation fallback: {str(error)[:100]}",
    }


@track_performance("llm_critic_redesign_audit")
def qualify_redesign(skeleton_context: str, issues: List[str]) -> Dict[str, Any]:
    issues_summary = "\n".join(f"- {i}" for i in issues[:6]) if issues else "None"
    prompt = (
        "Evaluate the website redesign viability using the evidence below.\n\n"
        f"SITE EVIDENCE:\n{skeleton_context[:8000]}\n\n"
        f"HEURISTIC FLAGS:\n{issues_summary}"
    )

    try:
        gateway = _get_gateway()
        raw_response = gateway.query(prompt=prompt, system_instruction=CRITIC_SYSTEM_PROMPT)
        result = _parse_json(raw_response)
        return apply_qualification_rules(result)
    except Exception as err:
        app_logger.debug("Critic qualification fallback triggered: %s", err)
        return _failure_result(err)