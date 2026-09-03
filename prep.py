"""Interview / application prep generation.

The public surface is intentionally small and stable so an LLM-backed provider
can be dropped in later WITHOUT touching the API route or the DB schema:

    provider = get_prep_provider()
    result = provider.generate(job_dict)   # -> PrepResult

Today the only implementation is `RuleBasedPrepProvider`, which is fully
deterministic and offline. To swap in an LLM later, implement the `PrepProvider`
protocol and return it from `get_prep_provider()`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Protocol, Any


@dataclass
class PrepResult:
    summary: str
    talking_points: List[str] = field(default_factory=list)
    bullets: List[str] = field(default_factory=list)

    def bullets_text(self) -> str:
        return "\n".join(f"- {b}" for b in self.bullets)

    def talking_points_text(self) -> str:
        return "\n".join(f"- {t}" for t in self.talking_points)


class PrepProvider(Protocol):
    def generate(self, job: Dict[str, Any]) -> PrepResult:  # pragma: no cover - protocol
        ...


# Domain-specific framing used to tailor deterministic output.
_DOMAIN_PROFILE = {
    "SWE": {
        "label": "software engineering",
        "skills": ["clean API design", "testing discipline", "code review", "shipping features end-to-end"],
        "talk": [
            "Walk through a feature you built end-to-end, from design doc to production.",
            "Describe how you keep code maintainable: tests, reviews, and small PRs.",
        ],
    },
    "Systems": {
        "label": "systems and infrastructure",
        "skills": ["reliability", "performance profiling", "distributed systems", "observability"],
        "talk": [
            "Explain a time you debugged a performance or reliability issue under load.",
            "Discuss trade-offs you made around latency, throughput, or resource limits.",
        ],
    },
    "AI/ML": {
        "label": "machine learning",
        "skills": ["data pipelines", "model evaluation", "experiment tracking", "moving models to production"],
        "talk": [
            "Describe an ML project: the metric you optimized and how you validated it.",
            "Explain how you would take a prototype model into a reliable production service.",
        ],
    },
    "General": {
        "label": "engineering",
        "skills": ["problem solving", "collaboration", "ownership", "communication"],
        "talk": [
            "Share a project you are proud of and the impact it had.",
            "Explain how you approach an unfamiliar problem from scratch.",
        ],
    },
}


class RuleBasedPrepProvider:
    """Deterministic, offline prep generator.

    Output depends only on the job fields, so tests can assert exact strings.
    """

    def generate(self, job: Dict[str, Any]) -> PrepResult:
        company = (job.get("company") or "the company").strip()
        title = (job.get("title") or "this role").strip()
        domain = job.get("domain") or "General"
        track = job.get("track") or "unclear"
        location = (job.get("location") or "").strip()

        profile = _DOMAIN_PROFILE.get(domain, _DOMAIN_PROFILE["General"])
        role_kind = "internship" if track == "internship" else "full-time role"

        summary = (
            f"{title} at {company} is a {profile['label']} {role_kind}"
            + (f" based in {location}." if location else ".")
            + f" Focus your prep on {profile['skills'][0]} and {profile['skills'][1]}."
        )

        talking_points: List[str] = [
            f"Why {company}: connect one concrete thing about the company or product to your goals.",
            *profile["talk"],
            f"Ask a sharp question about how the {profile['label']} team measures success.",
        ]

        bullets: List[str] = [
            f"Delivered {profile['skills'][0]} on a project relevant to {profile['label']}, with measurable impact.",
            f"Demonstrated {profile['skills'][2]} while collaborating across a team.",
            f"Applied {profile['skills'][3]} to ship reliable, production-ready work.",
        ]
        if track == "internship":
            bullets.append(
                "Highlighted coursework or side projects that map directly to the internship's stack."
            )

        return PrepResult(summary=summary, talking_points=talking_points, bullets=bullets)


def get_prep_provider() -> PrepProvider:
    """Return the active prep provider.

    Swap this for an LLM-backed provider later; callers never change.
    """
    return RuleBasedPrepProvider()
