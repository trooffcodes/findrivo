
import json
import re
from typing import Any, Dict, List

from core.llm_client import LLMGateway
from core.logger import app_logger, track_performance
from prompts.critic_prompt import CRITIC_SYSTEM_PROMPT

_llm_gateway = None

_VALID_VERDICTS = {"modern", "passable", "outdated", "broken"}


def _get_gateway() -> LLMGateway:
    global _llm_gateway
    if _llm_gateway is None:
        _llm_gateway = LLMGateway()
    return _llm_gateway


def _parse_json(raw_response: str) -> Dict[str, Any]:
    """Parse a JSON object, including one wrapped in Markdown fences."""
    clean = raw_response.strip()

    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.I)
        clean = re.sub(r"\s*```$", "", clean)

    try:
        result = json.loads(clean)
    except json.JSONDecodeError:
        # Recover a JSON object if the provider added surrounding text.
        decoder = json.JSONDecoder()
        start = clean.find("{")
        if start < 0:
            raise ValueError("LLM response contains no JSON object")

        result, _ = decoder.raw_decode(clean[start:])

    if not isinstance(result, dict):
        raise ValueError("LLM response must be a JSON object")

    return result


def _score(value: Any, default: int = 0) -> int:
    """Normalize an integer-like score to the 0-100 range."""
    try:
        return max(0, min(100, int(float(value))))
    except (TypeError, ValueError, OverflowError):
        return default


def _normalize_defects(value: Any) -> List[str]:
    """Accept only a short list of non-empty defect descriptions."""
    if not isinstance(value, list):
        return []

    defects = []
    for item in value[:3]:
        if isinstance(item, str) and item.strip():
            defects.append(item.strip()[:500])

    return defects


def apply_qualification_rules(result: Dict[str, Any]) -> Dict[str, Any]:
    """Apply deterministic qualification and selective human-review rules."""
    viability = _score(result.get("viability_score"))
    confidence = _score(result.get("confidence_score"))
    defects = _normalize_defects(result.get("primary_defects"))

    # An LLM finding is not enough by itself to prove a material defect.
    qualified = (
        viability >= 70
        and confidence >= 80
        and bool(defects)
    )

    # Review only potentially valuable leads with uncertain evidence.
    review_required = (
        not qualified
        and viability >= 65
        and confidence < 80
        and bool(defects)
    )

    verdict = result.get("design_verdict", "passable")
    if verdict not in _VALID_VERDICTS:
        verdict = "passable"

    rejection_reason = result.get("rejection_reason")
    if not qualified and (
        not isinstance(rejection_reason, str) or not rejection_reason.strip()
    ):
        rejection_reason = (
            "The available evidence does not establish a material "
            "redesign opportunity."
        )

    if qualified:
        rejection_reason = None

    result.update({
        "viability_score": viability,
        "confidence_score": confidence,
        "design_verdict": verdict,
        "primary_defects": defects,
        "qualified": qualified,
        "needs_redesign": qualified,
        "review_required": review_required,
        "review_reason": (
            "Potentially material issue needs evidence verification."
            if review_required else None
        ),
        "rejection_reason": rejection_reason,
    })

    return result


def _failure_result(error: Exception) -> Dict[str, Any]:
    """Fail closed: a model failure must never manufacture a lead."""
    return {
        "viability_score": 0,
        "confidence_score": 0,
        "design_verdict": "passable",
        "primary_defects": [],
        "qualified": False,
        "needs_redesign": False,
        "review_required": False,
        "review_reason": None,
        "rejection_reason": "Audit assessment failed; no reliable decision.",
        "audit_error": True,
        "audit_error_type": type(error).__name__,
    }


@track_performance("llm_critic_redesign_audit")
def qualify_redesign(
    skeleton_context: str,
    issues: List[str],
) -> Dict[str, Any]:
    """Assess redesign evidence, then enforce qualification in Python."""
    issues_summary = (
        "\n".join(f"- {issue}" for issue in issues[:6])
        if issues else "None"
    )

    prompt = (
        "Evaluate the website using the evidence below. Audit flags are "
        "unverified signals, not established facts. Do not infer visual "
        "appearance from CSS metrics. Return the required JSON.\n\n"
        f"SITE EVIDENCE:\n{skeleton_context[:9000]}\n\n"
        f"HEURISTIC FLAGS:\n{issues_summary}"
    )

    try:
        gateway = _get_gateway()
        raw_response = gateway.call_groq_with_fallback(
            prompt=prompt,
            system_prompt=CRITIC_SYSTEM_PROMPT,
            temperature=0.2,
        )

        result = _parse_json(raw_response)

        # Require both scores and a supported verdict.
        if "viability_score" not in result or "confidence_score" not in result:
            raise ValueError("Critic response is missing required scores")

        if result.get("design_verdict") not in _VALID_VERDICTS:
            raise ValueError("Critic response has an invalid design_verdict")

        result["primary_defects"] = _normalize_defects(
            result.get("primary_defects")
        )

        return apply_qualification_rules(result)

    except Exception as err:
        app_logger.warning(
            "Critic redesign qualification failed | err=%s",
            err,
        )
        return _failure_result(err)
