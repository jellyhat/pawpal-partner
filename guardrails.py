"""Guardrails for PawPal+ AI features.

Three kinds of checks, all deterministic so they can be unit-tested:

1. Input guardrails on sitter questions (length, emergencies, unsafe
   medication requests) -- these run *before* anything is sent to a model.
2. Data hygiene for owner-written text that will be placed in a prompt
   (strip control characters, flag prompt-injection phrasing).
3. Output guardrails on model answers (every claim must cite a retrieved
   source; no dosage numbers that are not in the sources).

Every time a guardrail fires it is logged to ``logs/pawpal.log``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Sequence

logger = logging.getLogger("pawpal.guardrails")

MAX_QUESTION_CHARS = 500

EMERGENCY_PATTERNS = [
    r"\bseiz", r"\bnot breathing\b", r"\bcan'?t breathe\b", r"\bchok", r"\bcollaps",
    r"\bunconscious\b", r"\bbleeding\b", r"\bpoison",
    r"\b(?:ate|eaten|swallowed|chewed|licked|got into)\b.{0,40}"
    r"\b(?:chocolate|grapes?|raisins?|xylitol|sugar.?free gum|onions?|garlic|lily|lilies|"
    r"rat bait|antifreeze|ibuprofen|tylenol|advil)\b",
    r"\bhit by (?:a )?car\b", r"\bbloat", r"\bswollen (?:belly|stomach)\b",
    r"\bhasn'?t peed\b", r"\bcan'?t pee\b", r"\bstraining to (?:pee|urinate)\b",
]

HUMAN_MEDICATIONS = [
    "ibuprofen", "advil", "motrin", "acetaminophen", "tylenol", "paracetamol",
    "naproxen", "aleve", "aspirin",
]

DOSE_CHANGE_PATTERNS = [
    r"\b(?:double|extra|another|more|increase|bigger|higher)\b.{0,25}\b(?:dose|pill|tablet|medication|meds?)\b",
    r"\b(?:dose|pill|tablet|medication|meds?)\b.{0,25}\b(?:double|extra|increase|more)\b",
    r"\bskip(?:ping)?\b.{0,25}\b(?:dose|pill|tablet|medication|meds?)\b",
    r"\bmake up for\b.{0,25}\b(?:missed|dose)\b",
]

INJECTION_PATTERNS = [
    r"ignore (?:all |any )?(?:previous|prior|above) instructions",
    r"disregard (?:the )?(?:system|previous) (?:prompt|instructions)",
    r"you are now\b",
    r"\bsystem prompt\b",
]


@dataclass
class GuardrailResult:
    allowed: bool
    category: str  # "ok", "empty", "too_long", "emergency", "medication", "human_medication"
    message: str = ""
    urgent: bool = False


def check_question(question: str, vet_contacts: Sequence[str] = ()) -> GuardrailResult:
    """Screen a sitter question before retrieval or any model call."""
    text = (question or "").strip()
    if not text:
        return GuardrailResult(False, "empty", "Type a question about the pets' care.")
    if len(text) > MAX_QUESTION_CHARS:
        logger.warning("Guardrail: question too long (%d chars)", len(text))
        return GuardrailResult(
            False,
            "too_long",
            f"Please keep questions under {MAX_QUESTION_CHARS} characters.",
        )

    lowered = text.casefold()
    contacts = "; ".join(c for c in vet_contacts if c) or "the vet listed by the owner"

    if any(re.search(p, lowered) for p in EMERGENCY_PATTERNS):
        logger.warning("Guardrail: emergency language detected: %r", text[:120])
        return GuardrailResult(
            True,
            "emergency",
            "This may be an emergency. Call the vet or the nearest emergency "
            f"animal hospital now ({contacts}), then contact the owner. "
            "Don't wait on this app.",
            urgent=True,
        )

    if any(name in lowered for name in HUMAN_MEDICATIONS):
        logger.warning("Guardrail: human medication question blocked: %r", text[:120])
        return GuardrailResult(
            False,
            "human_medication",
            "Do not give pets human pain relievers. Many (ibuprofen, "
            "acetaminophen, naproxen, aspirin) are toxic to dogs and cats. "
            f"Call the vet ({contacts}) for advice.",
        )

    if any(re.search(p, lowered) for p in DOSE_CHANGE_PATTERNS):
        logger.warning("Guardrail: dose-change question blocked: %r", text[:120])
        return GuardrailResult(
            False,
            "medication",
            "I can't advise changing, skipping, or doubling medication. Follow "
            "the owner's written instructions exactly. If a dose was missed or "
            f"something seems wrong, call the vet ({contacts}) and the owner.",
        )

    return GuardrailResult(True, "ok")


def sanitize_owner_text(text: str, max_chars: int = 1000) -> str:
    """Clean owner-provided text before it is placed in a model prompt.

    The text is still treated strictly as data (it is wrapped in tags in the
    prompt); this just removes control characters and logs injection attempts.
    """
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text or "")
    cleaned = cleaned[:max_chars]
    if any(re.search(p, cleaned.casefold()) for p in INJECTION_PATTERNS):
        logger.warning("Guardrail: possible prompt injection in owner text: %r", cleaned[:120])
    return cleaned


def cited_source_numbers(answer: str) -> List[int]:
    return [int(n) for n in re.findall(r"\[(\d+)\]", answer)]


def check_answer(answer: str, sources: Sequence[str]) -> GuardrailResult:
    """Verify a model answer is grounded in the retrieved sources.

    * It must cite at least one source as ``[n]``.
    * Every cited number must refer to a real source.
    * Any dose-like amount (``2 mg``, ``1/2 tablet``) must appear in a source.
    """
    citations = cited_source_numbers(answer)
    if not citations:
        logger.warning("Guardrail: answer had no citations")
        return GuardrailResult(False, "no_citation", "Answer did not cite its sources.")
    bad = [n for n in citations if not 1 <= n <= len(sources)]
    if bad:
        logger.warning("Guardrail: answer cited missing sources %s", bad)
        return GuardrailResult(False, "bad_citation", f"Answer cited unknown sources {bad}.")

    joined = " ".join(sources).casefold()
    amounts = re.findall(
        r"\b\d+(?:[./]\d+)?\s*(?:mg|ml|mL|cc|tablets?|pills?|cups?|tbsp|tsp|units?)\b",
        answer,
    )
    for amount in amounts:
        compact = re.sub(r"\s+", " ", amount.casefold())
        if compact not in joined and compact.replace(" ", "") not in joined.replace(" ", ""):
            logger.warning("Guardrail: ungrounded amount %r in answer", amount)
            return GuardrailResult(
                False,
                "ungrounded_amount",
                f"Answer mentioned '{amount}', which is not in the owner's notes.",
            )
    return GuardrailResult(True, "ok")
