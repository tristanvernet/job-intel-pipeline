"""Populate or update profile.json from a resume file or a GitHub username.

Usage:
    python sync_profile.py --resume path/to/resume.md
    python sync_profile.py --resume path/to/resume.pdf
    python sync_profile.py --resume path/to/resume.docx
    python sync_profile.py --github octocat
    python sync_profile.py --resume resume.txt --github octocat   # merge both

The scanner is a deterministic keyword-extraction pass over a known technology
vocabulary, so the same input always produces the same profile. GitHub language
weights are derived from real byte counts across the user's public repos.

Design notes:
  * Text/Markdown resumes are read directly. PDF support is optional and only
    used if `pypdf` (or `PyPDF2`) is installed; otherwise a clear error is shown.
  * .docx resumes are extracted with `python-docx` when installed; otherwise a
    clear install hint is shown.
  * Network calls are isolated and fail loudly-but-gracefully (no silent except).
  * Existing hand-tuned weights in profile.json are preserved unless a newly
    detected weight is higher, so re-syncing never clobbers manual tuning.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Optional

from matcher import PROFILE_PATH, _WEIGHTED_SECTIONS, tokenize

# --------------------------------------------------------------------------- #
# Known technology vocabulary. Multi-word / symbol terms are matched as phrases.
# --------------------------------------------------------------------------- #
KNOWN_LANGUAGES = {
    "python", "java", "c++", "c#", "javascript", "typescript", "go", "golang",
    "rust", "ruby", "kotlin", "swift", "scala", "php", "sql", "bash", "r",
}
KNOWN_FRAMEWORKS = {
    "fastapi", "flask", "django", "spring", "react", "angular", "vue", "node",
    "express", "pytorch", "tensorflow", "keras", "pandas", "numpy", "spark",
    "kafka", "docker", "kubernetes", "terraform", "graphql",
}
KNOWN_SKILLS = {
    "backend", "frontend", "fullstack", "systems", "distributed",
    "infrastructure", "api", "database", "testing", "cloud", "devops",
    "machine learning", "deep learning", "nlp", "computer vision",
    "microservices", "ci/cd", "observability", "security",
}
KNOWN_KEYWORDS = {
    "software", "engineer", "developer", "swe", "sde", "ai", "ml", "platform",
    "embedded", "kernel", "compiler", "data", "analytics",
}

_SECTION_VOCAB = {
    "languages": KNOWN_LANGUAGES,
    "frameworks": KNOWN_FRAMEWORKS,
    "skills": KNOWN_SKILLS,
    "keywords": KNOWN_KEYWORDS,
}

# GitHub language name -> our canonical language key.
_GH_LANG_ALIASES = {"go": "go", "golang": "go", "c++": "c++", "c#": "c#"}

_DEFAULT_SATURATION = 4.0


# --------------------------------------------------------------------------- #
# Resume text extraction
# --------------------------------------------------------------------------- #
def extract_text_from_resume(path: str | Path) -> str:
    """Return the plain text of a resume.

    .pdf is parsed via pypdf, .docx via python-docx, and everything else
    (.txt/.md/.markdown/no extension) is read directly as UTF-8 text.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Resume not found: {p}")

    suffix = p.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf_text(p)
    if suffix == ".docx":
        return _extract_docx_text(p)
    # Treat everything else (.txt, .md, .markdown, no extension) as text.
    return p.read_text(encoding="utf-8", errors="replace")


def _extract_pdf_text(path: Path) -> str:
    try:
        try:
            from pypdf import PdfReader  # type: ignore
        except ImportError:
            from PyPDF2 import PdfReader  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "PDF support requires the 'pypdf' package. Install it with "
            "`pip install pypdf`, or convert the resume to .txt/.md first."
        ) from exc

    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _extract_docx_text(path: Path) -> str:
    try:
        from docx import Document  # type: ignore  # provided by python-docx
    except ImportError as exc:
        raise RuntimeError(
            "DOCX support requires the 'python-docx' package. Install it with "
            "`pip install python-docx`, or convert the resume to .txt/.md first."
        ) from exc

    document = Document(str(path))
    parts = [para.text for para in document.paragraphs]
    # Include text inside tables, which python-docx keeps separate from paragraphs.
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Deterministic keyword scan
# --------------------------------------------------------------------------- #
def scan_text_for_tech(text: str) -> Dict[str, Dict[str, float]]:
    """Extract weighted terms per section from free text.

    Weights are frequency-normalized within each section to a 0..1 range, so the
    most-mentioned technology in a section anchors at 1.0 and the rest scale down.
    Deterministic: identical text always yields identical weights.
    """
    normalized = text.lower()
    tokens = tokenize(normalized)

    sections: Dict[str, Dict[str, float]] = {s: {} for s in _WEIGHTED_SECTIONS}
    for section, vocab in _SECTION_VOCAB.items():
        counts: Counter[str] = Counter()
        for term in vocab:
            if " " in term or any(c in term for c in "+#./"):
                occurrences = normalized.count(term)
            else:
                occurrences = 1 if term in tokens else 0
            if occurrences:
                counts[term] = occurrences
        if not counts:
            continue
        peak = max(counts.values())
        for term, n in counts.items():
            # Scale 0.4..1.0 by frequency so any mention is meaningful.
            sections[section][term] = round(0.4 + 0.6 * (n / peak), 3)
    return sections


# --------------------------------------------------------------------------- #
# GitHub extraction
# --------------------------------------------------------------------------- #
def fetch_github_profile(username: str, timeout: float = 10.0) -> Dict[str, Dict[str, float]]:
    """Derive weighted languages/keywords from a GitHub user's public repos.

    Language weights come from aggregate byte counts across repos (normalized to
    the top language = 1.0). Repo topics feed the keywords section. Network
    errors raise RuntimeError with a clear message (no silent failure).
    """
    import requests  # local import so the scan path has no network dependency

    headers = {"Accept": "application/vnd.github+json"}
    try:
        resp = requests.get(
            f"https://api.github.com/users/{username}/repos",
            params={"per_page": 100, "sort": "updated"},
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"GitHub request failed for '{username}': {exc}") from exc

    if resp.status_code == 404:
        raise RuntimeError(f"GitHub user '{username}' not found.")
    if resp.status_code == 403:
        raise RuntimeError("GitHub rate limit hit. Try again later or set a token.")
    if resp.status_code != 200:
        raise RuntimeError(f"GitHub returned HTTP {resp.status_code} for '{username}'.")

    repos = resp.json()
    lang_bytes: Counter[str] = Counter()    # precise: raw bytes per language
    lang_coarse: Counter[str] = Counter()   # fallback: primary language per repo
    topics: Counter[str] = Counter()

    # Cap precise fetching at the 20 most recently updated repos (the list is
    # already sorted by `updated`) so a large account never triggers a burst
    # of ~100 sequential calls and secondary rate limits.
    precise_budget = 20

    for repo in repos:
        if repo.get("fork"):
            continue
        primary = repo.get("language")
        if primary:
            lang_coarse[primary.lower()] += 1
        for topic in repo.get("topics") or []:
            topics[topic.lower()] += 1

        # Best-effort precise byte counts per language, politely throttled.
        lang_url = repo.get("languages_url")
        if lang_url and precise_budget > 0:
            precise_budget -= 1
            time.sleep(0.1)  # avoid secondary rate limits
            try:
                lr = requests.get(lang_url, headers=headers, timeout=timeout)
                if lr.status_code == 200:
                    for lang, byte_count in lr.json().items():
                        lang_bytes[lang.lower()] += int(byte_count)
            except (requests.RequestException, ValueError):
                # Non-fatal: this repo just won't contribute byte counts.
                continue

    # Never mix units: prefer raw byte counts when any were fetched; fall back
    # to coarse per-repo primary-language counts only when none succeeded.
    lang_counts = lang_bytes if lang_bytes else lang_coarse

    return {
        "languages": _normalize_weights(
            {_GH_LANG_ALIASES.get(k, k): v for k, v in lang_counts.items()}
        ),
        "keywords": _normalize_weights(dict(topics)),
    }


def _normalize_weights(counts: Dict[str, float]) -> Dict[str, float]:
    if not counts:
        return {}
    peak = max(counts.values())
    if peak <= 0:
        return {}
    return {k: round(0.4 + 0.6 * (v / peak), 3) for k, v in counts.items()}


# --------------------------------------------------------------------------- #
# Merge + persist
# --------------------------------------------------------------------------- #
def merge_sections(
    base: Dict[str, Any], incoming: Dict[str, Dict[str, float]]
) -> Dict[str, Any]:
    """Merge detected weights into a profile dict, keeping the higher weight."""
    merged = json.loads(json.dumps(base))  # deep copy, JSON-safe
    for section in _WEIGHTED_SECTIONS:
        merged.setdefault(section, {})
        for term, weight in (incoming.get(section) or {}).items():
            existing = merged[section].get(term)
            merged[section][term] = (
                max(float(existing), float(weight)) if existing is not None else float(weight)
            )
    merged.setdefault("score_saturation", _DEFAULT_SATURATION)
    return merged


def load_existing_profile(path: str | Path) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {
            "name": "Candidate Profile",
            **{s: {} for s in _WEIGHTED_SECTIONS},
            "score_saturation": _DEFAULT_SATURATION,
        }
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_profile(profile: Dict[str, Any], path: str | Path) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(profile, fh, indent=2, sort_keys=False)
        fh.write("\n")


def build_profile(
    resume: Optional[str] = None,
    github: Optional[str] = None,
    profile_path: str | Path = PROFILE_PATH,
) -> Dict[str, Any]:
    """Build an updated profile from a resume and/or GitHub username."""
    if not resume and not github:
        raise ValueError("Provide at least one of --resume or --github.")

    profile = load_existing_profile(profile_path)

    if resume:
        text = extract_text_from_resume(resume)
        profile = merge_sections(profile, scan_text_for_tech(text))

    if github:
        profile = merge_sections(profile, fetch_github_profile(github))

    return profile


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Populate/update profile.json.")
    parser.add_argument("--resume", help="Path to a resume (.txt/.md/.pdf/.docx).")
    parser.add_argument("--github", help="GitHub username to derive tech weights from.")
    parser.add_argument(
        "--out", default=str(PROFILE_PATH), help="Output profile path (default: profile.json)."
    )
    args = parser.parse_args(argv)

    if not args.resume and not args.github:
        parser.error("Provide at least one of --resume or --github.")

    try:
        profile = build_profile(resume=args.resume, github=args.github, profile_path=args.out)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"[sync_profile] Error: {exc}", file=sys.stderr)
        return 1

    save_profile(profile, args.out)
    counts = {s: len(profile.get(s) or {}) for s in _WEIGHTED_SECTIONS}
    print(f"[sync_profile] Wrote {args.out}")
    print(f"[sync_profile] Terms per section: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
