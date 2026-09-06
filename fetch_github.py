import re
import requests
import sqlite3
import json
from collector_status import failure_status
from db import insert_job
from classifier import is_unwanted_role, classify_track, classify_domain

# Raw Markdown source lists for early-career roles
SOURCES = [
    {
        "url": "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/README.md",
        "default_track": "internship",
        "term_hint": "Summer 2027",
    },
    {
        "url": "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md",
        "default_track": "full_time",
        "term_hint": "New Grad 2027",
    },
]

# [title](url) markdown links
_LINK_PATTERN = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
# <a href="url"> ... </a> HTML anchors (SimplifyJobs apply buttons)
_HREF_PATTERN = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
# HTML tags, images, comments to strip out of visible text
_TAG_PATTERN = re.compile(r"<[^>]+>")
# "Sub-listing" marker: a second role under the same company
_SUBLISTING_MARKER = "↳"
# Closed/expired roles carry a lock; skip them
_CLOSED_MARKERS = ("🔒", ":lock:")


def _strip_emojis(text: str) -> str:
    """Remove emoji / pictographs and sponsorship markers, keep readable text."""
    emoji_pattern = re.compile(
        "["
        "\U0001f300-\U0001faff"  # symbols & pictographs, transport, etc.
        "\U00002600-\U000027bf"  # misc symbols & dingbats
        "\U0001f1e6-\U0001f1ff"  # regional indicator (flags)
        "\U0000fe00-\U0000fe0f"  # variation selectors
        "\U00002190-\U000021ff"  # arrows (incl. ↳)
        "]+",
        flags=re.UNICODE,
    )
    # Also drop GitHub shortcode markers like :lock: :us:
    text = re.sub(r":[a-z_]+:", "", text)
    return emoji_pattern.sub("", text)


def _clean_text(cell: str) -> str:
    """Strip markdown links, HTML tags and emojis down to plain readable text."""
    # Replace markdown links with their label
    cell = _LINK_PATTERN.sub(lambda m: m.group(1), cell)
    cell = _TAG_PATTERN.sub("", cell)
    cell = _strip_emojis(cell)
    # Collapse </br>, pipes and whitespace
    cell = cell.replace("</br>", " ").replace("<br>", " ")
    return re.sub(r"\s+", " ", cell).strip(" *")


def _extract_url(*cells: str) -> str:
    """Find the first real http(s) URL across the given cells."""
    for cell in cells:
        if not cell:
            continue
        href = _HREF_PATTERN.search(cell)
        if href and href.group(1).startswith("http"):
            return href.group(1)
        md = _LINK_PATTERN.search(cell)
        if md and md.group(2).startswith("http"):
            return md.group(2)
        stripped = cell.strip()
        if stripped.startswith("http"):
            return stripped.split()[0]
    return ""


# Matches HTML table cells (SimplifyJobs' current format).
_TD_PATTERN = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)


def _extract_cell_rows(content: str):
    """Yield rows in source order, with None marking table/section boundaries.

    Consume whole HTML rows before looking for Markdown rows, so HTML cell
    content cannot be interpreted a second time as a Markdown table.
    """
    def markdown_rows(text):
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("|"):
                parts = [p.strip() for p in line.split("|")[1:-1]]
                if len(parts) >= 4:
                    yield parts[:4]
                    continue
            yield None

    blocks = re.compile(
        r"<table\b[^>]*>|</table\s*>|"
        r"<h[1-6]\b[^>]*>.*?</h[1-6]\s*>|"
        r"<tr\b[^>]*>.*?</tr\s*>", re.DOTALL | re.IGNORECASE
    )
    end = 0
    for block in blocks.finditer(content):
        gap = content[end:block.start()]
        if gap.strip():
            yield from markdown_rows(gap)
        raw = block.group()
        if re.match(r"<tr\b", raw, re.IGNORECASE):
            cells = _TD_PATTERN.findall(raw)
            yield [c.strip() for c in cells[:4]] if len(cells) >= 4 else None
        else:
            yield None
        end = block.end()
    yield from markdown_rows(content[end:])


def parse_markdown_table(content: str, default_track: str, term_hint: str, stats: dict = None):
    """Parse SimplifyJobs-style Markdown tables into (payloads, skipped).

    Pure function: no network, no DB writes. Handles emojis, sponsorship
    markers, `<a href>` apply buttons, markdown links, and `↳` sub-listings
    that inherit the company name from the row above.

    If a mutable `stats` dict is provided, it is populated with a breakdown of
    total data rows seen and skip reasons, so callers can print an audit.
    """
    payloads = []
    last_company = None
    total_rows = 0
    reasons = {
        "closed_locked": 0,
        "missing_fields": 0,
        "excluded_level_or_discipline": 0,
        "unknown_domain": 0,
    }

    for row in _extract_cell_rows(content):
        if row is None:
            last_company = None
            continue
        company_raw, role_raw, location_raw, link_raw = row
        # Skip header and separator rows (not counted as data rows)
        joined = f"{company_raw}{role_raw}".lower()
        if "company" in joined and "role" in joined:
            last_company = None
            continue
        if company_raw and set(company_raw) <= set("-: "):
            continue

        total_rows += 1

        # Company context belongs to the table, including closed/filtered jobs.
        company = _clean_text(company_raw)
        if not company or _SUBLISTING_MARKER in company_raw:
            company = last_company
        else:
            last_company = company

        if any(m in role_raw or m in link_raw for m in _CLOSED_MARKERS):
            reasons["closed_locked"] += 1
            continue

        title = _clean_text(role_raw)
        location = _clean_text(location_raw) or "Not specified"
        job_url = _extract_url(link_raw, role_raw)

        if not title or not job_url or not company:
            reasons["missing_fields"] += 1
            continue

        # Gate 1: excluded levels / non-SWE disciplines
        if is_unwanted_role(title):
            reasons["excluded_level_or_discipline"] += 1
            continue

        # Gate 2: must map to a known domain (technical roles default to SWE)
        domain = classify_domain(title)
        if not domain:
            reasons["unknown_domain"] += 1
            continue

        # Classification (fall back to source defaults when unclear)
        track, term = classify_track(title, role_raw)
        if track == "unclear":
            track, term = default_track, term_hint
        elif track == default_track and term is None:
            term = term_hint or None

        payloads.append(
            {
                "company": company,
                "title": title,
                "url": job_url,
                "location": location,
                "is_remote": "remote" in location.lower(),
                "track": track,
                "term": term,
                "domain": domain,
                "source": "github_early_career",
                "raw_description": f"Sourced from curated early-career list: {title} at {company} ({location})",
            }
        )

    skipped = sum(reasons.values())
    if stats is not None:
        stats["total_rows"] = total_rows
        stats["kept"] = len(payloads)
        stats["skipped"] = skipped
        stats["reasons"] = reasons

    return payloads, skipped


def run_github_fetch():
    """Fetch all curated sources. Returns a per-source outcome report.

    Each entry is {"source", "ok", "added", "skipped", "error"} so callers can
    distinguish a genuinely empty source (ok=True, added=0) from a broken one
    (ok=False, error set) -- a 404/branch rename must never look like success.
    """
    outcomes = []
    for src in SOURCES:
        outcome = {"source": src["url"], "ok": False, "status": "ok",
                   "fetched": 0, "inserted": 0, "added": 0,
                   "rejected": 0, "skipped": 0, "error": None}
        try:
            resp = requests.get(src["url"], timeout=10)
            if resp.status_code != 200:
                outcome.update(status="blocked" if resp.status_code in (403, 429) else "failed",
                               error=f"http {resp.status_code}")
            else:
                stats = {}
                payloads, skipped = parse_markdown_table(
                    resp.text, src["default_track"], src["term_hint"], stats=stats
                )
                outcome.update(fetched=stats["total_rows"], skipped=skipped)
                if not stats["total_rows"]:
                    outcome.update(status="failed", error="No recognizable source rows")
                for payload in payloads:
                    try:
                        if insert_job(payload):
                            outcome["inserted"] += 1
                    except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
                        outcome["rejected"] += 1
                        outcome.update(status="partial", error=str(exc))
        except Exception as exc:
            status = failure_status(exc)
            if outcome["inserted"] and status != "blocked":
                status = "partial"
            outcome.update(status=status, error=str(exc))
        outcome.update(ok=outcome["status"] == "ok", added=outcome["inserted"])
        outcomes.append(outcome)
        print(json.dumps(outcome, sort_keys=True))
    return outcomes


if __name__ == "__main__":
    run_github_fetch()
