"""Deterministic match-scoring engine.

Computes a 0-100 fit score for a job based on token overlap between the role
(title, track, domain) and a local `profile.json`.

The scoring is fully deterministic and offline: the same job + profile always
yields the same integer score. This module has no network or DB dependencies so
it is trivially testable and safe to call on every API request.

Public surface:
    default_profile() -> dict
    load_profile(path) -> dict
    match_score(job, profile) -> int              # 0..100
    explain(job, profile) -> dict                 # matched terms + raw weight
    rank(rows, profile, *, by_score) -> list      # rows plus match_score
    with_match(job, profile) -> dict              # job plus score breakdown
    ProfileFile(path).current() -> dict           # mtime-cached profile file
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

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


def default_profile() -> Dict[str, Any]:
    """Return a fresh safe profile for missing or initially unreadable files."""
    return {
        "languages": {"python": 1.0, "java": 1.0},
        "frameworks": {},
        "skills": {"backend": 0.9, "systems": 0.8},
        "keywords": {"software": 0.8, "engineer": 0.6},
        "score_saturation": _DEFAULT_SATURATION,
    }


def load_profile(path: str | Path) -> Dict[str, Any]:
    """Load a profile from disk, falling back to a safe default.

    Never raises for a missing file. Invalid JSON or a non-object document
    raises ValueError so cache callers can retain a last-known-good snapshot.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            profile = json.load(fh)
    except FileNotFoundError:
        # Open directly: the file can disappear between exists() and open().
        return default_profile()
    if not isinstance(profile, dict):
        raise ValueError("Profile must be a JSON object")
    return profile


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


def match_score(job: Dict[str, Any], profile: Dict[str, Any],
                _terms: Optional[Dict[str, float]] = None) -> int:
    """Return a deterministic 0-100 fit score for `job` against `profile`.

    Score = 100 * min(1, matched_weight / saturation), rounded to an int.
    An empty/None job or a profile with no matches yields 0.
    """
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


def explain(job: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    """Return the matched terms and raw weight behind a score (for debugging)."""
    text_norm = role_text(job or {})
    tokens = tokenize(text_norm)
    terms = flatten_terms(profile)
    matched = {t: w for t, w in terms.items() if _term_matches(t, tokens, text_norm)}
    return {
        "score": match_score(job, profile, _terms=terms),
        "matched_terms": dict(sorted(matched.items(), key=lambda kv: (-kv[1], kv[0]))),
        "matched_weight": round(sum(matched.values()), 4),
    }


def rank(rows: List[Dict[str, Any]], profile: Dict[str, Any], *, by_score: bool) -> List[Dict[str, Any]]:
    """Return copies of `rows` with match_score attached, best first when `by_score`.

    The sort is stable, so rows with equal scores keep their input order.
    """
    terms = flatten_terms(profile)
    ranked = [{**row, "match_score": match_score(row, profile, _terms=terms)} for row in rows]
    if by_score:
        ranked.sort(key=lambda r: r["match_score"], reverse=True)
    return ranked


def with_match(job: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    """Return `job` plus its match_score and the matched vs. missing profile terms."""
    info = explain(job, profile)
    matched = info["matched_terms"]
    return {
        **job,
        "match_score": info["score"],
        "match": {
            "matched_terms": matched,
            "missing_terms": {t: w for t, w in flatten_terms(profile).items() if t not in matched},
            "matched_weight": info["matched_weight"],
        },
    }


class ProfileFile:
    """A profile file re-read only when its mtime changes.

    A failed reload keeps serving the last good snapshot so a half-edited file
    never breaks scoring.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = Lock()
        self._snapshot: Optional[tuple[Optional[int], Dict[str, Any]]] = None

    def current(self) -> Dict[str, Any]:
        with self._lock:
            try:
                try:
                    mtime = self.path.stat().st_mtime_ns
                except FileNotFoundError:
                    mtime = None
                if self._snapshot is None or self._snapshot[0] != mtime:
                    self._snapshot = (mtime, load_profile(self.path))
            except (OSError, ValueError) as exc:
                logger.warning("Could not reload profile; retaining last good profile: %s", exc)
                if self._snapshot is None:
                    # Do not record a failed file's mtime: retry when it is repaired.
                    self._snapshot = (None, default_profile())
            return self._snapshot[1]
