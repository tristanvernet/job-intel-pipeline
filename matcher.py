"""Deterministic match-scoring engine.

Computes a 0-100 fit score for a job based on token overlap between the role
(title, track, domain) and a local `profile.json`.

The scoring is fully deterministic and offline: the same job + profile always
yields the same integer score. This module has no network or DB dependencies so
it is trivially testable and safe to call on every API request.

Public surface:
    load_profile(path=None) -> dict
    match_score(job, profile=None) -> int        # 0..100
    explain(job, profile=None) -> dict            # matched terms + raw weight
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

PROFILE_PATH = Path(__file__).parent / "profile.json"

# Sections of the profile that contribute weighted terms, in flatten order.
_WEIGHTED_SECTIONS = ("languages", "frameworks", "skills", "keywords")

# Fields of a job that describe the role for matching purposes.
_ROLE_FIELDS = ("title", "track", "domain")

# Tokenizer keeps tech-relevant symbols (c++, c#, node.js) as part of a token.
_TOKEN_RE = re.compile(r"[a-z0-9+#.]+")

# Default saturation: the matched weight at which a job hits 100%. Kept here so
# scoring still works if profile.json is missing or omits the key. 6.0 keeps
# entry-level roles from all saturating at 100% -- a spread preserves signal.
_DEFAULT_SATURATION = 6.0


def _default_profile() -> Dict[str, Any]:
    """Minimal built-in profile used only when profile.json is absent."""
    return {
        "languages": {"python": 1.0, "java": 1.0},
        "frameworks": {},
        "skills": {"backend": 0.9, "systems": 0.8},
        "keywords": {"software": 0.8, "engineer": 0.6},
        "score_saturation": _DEFAULT_SATURATION,
    }


def load_profile(path: Optional[str | Path] = None) -> Dict[str, Any]:
    """Load a profile from disk, falling back to a safe default.

    Never raises for a missing file; a corrupt file raises json.JSONDecodeError
    so the caller sees an explicit, actionable error rather than silent bad data.
    """
    p = Path(path) if path is not None else PROFILE_PATH
    if not p.exists():
        return _default_profile()
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _normalize(text: Any) -> str:
    return str(text or "").lower()


def tokenize(text: Any) -> set[str]:
    """Split text into a set of lowercased tokens (tech symbols preserved).

    Internal symbols are kept (node.js, c++, ci/cd) but a dot-stripped variant is
    also added so trailing sentence punctuation ("spring.") still matches "spring".
    """
    tokens: set[str] = set()
    for tok in _TOKEN_RE.findall(_normalize(text)):
        tokens.add(tok)
        stripped = tok.strip(".")
        if stripped:
            tokens.add(stripped)
    return tokens


def flatten_terms(profile: Dict[str, Any]) -> Dict[str, float]:
    """Flatten all weighted sections into a single {term: weight} map.

    A term appearing in multiple sections keeps its highest weight. Non-numeric
    or blank entries are skipped rather than crashing the scorer.
    """
    terms: Dict[str, float] = {}
    for section in _WEIGHTED_SECTIONS:
        entries = profile.get(section) or {}
        if not isinstance(entries, dict):
            continue
        for term, weight in entries.items():
            key = str(term).strip().lower()
            if not key:
                continue
            try:
                w = float(weight)
            except (TypeError, ValueError):
                continue
            if w <= 0:
                continue
            terms[key] = max(terms.get(key, 0.0), w)
    return terms


def _term_matches(term: str, tokens: set[str], text_norm: str) -> bool:
    """Word-boundary match for single tokens; substring match for phrases/symbols."""
    if " " in term or any(c in term for c in "+#."):
        return term in text_norm
    return term in tokens


def role_text(job: Dict[str, Any]) -> str:
    """Join the role-describing fields of a job into one matchable string."""
    return " ".join(_normalize(job.get(f)) for f in _ROLE_FIELDS)


def match_score(job: Dict[str, Any], profile: Optional[Dict[str, Any]] = None,
                _terms: Optional[Dict[str, float]] = None) -> int:
    """Return a deterministic 0-100 fit score for `job` against `profile`.

    Score = 100 * min(1, matched_weight / saturation), rounded to an int.
    An empty/None job or a profile with no matches yields 0.

    Bulk callers (e.g. /api/jobs scoring hundreds of rows) should pass
    ``_terms=flatten_terms(profile)`` so the weighted-term map is built once
    instead of once per job.
    """
    if profile is None:
        profile = load_profile()

    text_norm = role_text(job or {})
    tokens = tokenize(text_norm)
    terms = _terms if _terms is not None else flatten_terms(profile)

    matched_weight = 0.0
    for term, weight in terms.items():
        if _term_matches(term, tokens, text_norm):
            matched_weight += weight

    try:
        saturation = float(profile.get("score_saturation", _DEFAULT_SATURATION))
    except (TypeError, ValueError):
        saturation = _DEFAULT_SATURATION
    if saturation <= 0:
        saturation = _DEFAULT_SATURATION

    ratio = min(1.0, matched_weight / saturation)
    score = int(round(100 * ratio))
    return max(0, min(100, score))


def explain(job: Dict[str, Any], profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the matched terms and raw weight behind a score (for debugging)."""
    if profile is None:
        profile = load_profile()
    text_norm = role_text(job or {})
    tokens = tokenize(text_norm)
    terms = flatten_terms(profile)
    matched = {t: w for t, w in terms.items() if _term_matches(t, tokens, text_norm)}
    return {
        "score": match_score(job, profile),
        "matched_terms": dict(sorted(matched.items(), key=lambda kv: (-kv[1], kv[0]))),
        "matched_weight": round(sum(matched.values()), 4),
    }
