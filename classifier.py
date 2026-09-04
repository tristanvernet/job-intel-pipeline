import re
from typing import Tuple, Optional

# Unambiguous senior / experienced keywords
EXCLUDED_LEVELS = [
    r"\bsr\b", r"\bsenior\b", r"\bprincipal\b", r"\blead\b", r"\bstaff\b",
    r"\bmanager\b", r"\bdirector\b", r"\bvp\b", r"\bhead of\b",
    r"\btier\s*(?:ii|iii|iv|2|3|4)\b", r"\b(?:ii|iii|iv)\b", r"\bexperienced\b"
]

# Unwanted non-tech or non-SWE disciplines
EXCLUDED_DOMAINS = [
    r"\belectrical\b", r"\bmechanical\b", r"\baerospace\b", r"\bcivil\b",
    r"\bprocess engineer\b", r"\bgraphics?\s+designer\b", r"\btechnician\b",
    r"\bscientist\b", r"\btherapist\b", r"\bnurse\b", r"\bhr\b", r"\btalent acquisition\b",
    r"\bbusiness systems\b", r"\bmarketing\b", r"\bsales\b"
]

def is_unwanted_role(title: str) -> bool:
    t = title.lower()
    for pattern in EXCLUDED_LEVELS + EXCLUDED_DOMAINS:
        if re.search(pattern, t):
            return True
    return False

def classify_track(title: str, description: str = "") -> Tuple[str, Optional[str]]:
    title_lower = title.lower()
    desc_lower = description.lower()[:1200]
    full_text = f"{title_lower} {desc_lower}"
    
    # 1. Internship Detection
    if any(k in title_lower for k in ["intern", "internship", "co-op", "coop"]):
        term = "Summer 2026 / General"
        if "summer 2027" in full_text or "summer '27" in full_text:
            term = "Summer 2027"
        elif "spring 2027" in full_text or "spring '27" in full_text:
            term = "Spring 2027"
        elif "fall 2026" in full_text or "fall '26" in full_text:
            term = "Fall 2026"
        elif "summer" in full_text:
            term = "Summer"
        return "internship", term

    # 2. Full-Time Entry Level Detection
    entry_signals = [
        "junior", "jr", "entry level", "associate", "new grad",
        "university graduate", "swe i", "software engineer i", "level 1", "rotational",
        # Broadened early-career / generalist titles
        "technology analyst", "sde", "software development engineer", "developer",
        "technical specialist", "graduate", "apprentice", "early career", "campus"
    ]
    if any(k in title_lower for k in entry_signals):
        return "full_time", "New Grad / Entry Level"

    if re.search(r"\b(entry[-\s]level|0[-\s]2 years|new grad|class of 202[67])\b", desc_lower):
        return "full_time", "Entry Level"

    return "unclear", None

# Any of these tokens marks a role as "technical" and therefore SWE-eligible
# when it doesn't strictly land in AI/ML or Systems. Kept broad on purpose so
# we stop silently dropping legitimate engineering roles.
_TECHNICAL_SWE_TOKENS = [
    "software", "backend", "back end", "full stack", "fullstack", "frontend",
    "front end", "developer", "swe", "sde", "web engineer", "application",
    "programmer", "engineer", "engineering", "technology analyst",
    "technical specialist", "technical analyst", "coding", "computer",
    "it analyst", "solutions engineer", "technical program",
]


def classify_domain(title: str) -> Optional[str]:
    t = title.lower()
    # Word-boundary match for short acronyms so "Retail", "Detail" or "Html"
    # never false-positive into AI/ML. Multi-word phrases are safe as substrings.
    if re.search(r"\b(ai|ml|nlp|llm)\b", t) or any(
        k in t for k in ["machine learning", "computer vision", "deep learning"]
    ):
        return "AI/ML"
    if any(k in t for k in ["embedded", "systems engineer", "infrastructure", "platform engineer", "kernel", "operating system", "devops", "sre"]):
        return "Systems"
    # Default any remaining technical role to SWE rather than dropping it.
    if any(k in t for k in _TECHNICAL_SWE_TOKENS):
        return "SWE"
    return None
