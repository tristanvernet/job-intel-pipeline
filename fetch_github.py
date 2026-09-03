import re
import requests
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


def parse_markdown_table(content: str, default_track: str, term_hint: str):
    """Parse SimplifyJobs-style Markdown tables into (payloads, skipped).

    Pure function: no network, no DB writes. Handles emojis, sponsorship
    markers, `<a href>` apply buttons, markdown links, and `↳` sub-listings
    that inherit the company name from the row above.
    """
    payloads = []
    skipped = 0
    last_company = ""

    for line in content.splitlines():
        line = line.rstrip()
        if not line.startswith("|"):
            continue

        parts = [p.strip() for p in line.split("|")[1:-1]]
        if len(parts) < 4:
            continue

        company_raw, role_raw, location_raw, link_raw = parts[0], parts[1], parts[2], parts[3]

        # Skip header and separator rows
        joined = f"{company_raw}{role_raw}".lower()
        if "company" in joined and "role" in joined:
            continue
        if set(company_raw) <= set("-: "):
            continue

        # Skip closed/locked roles entirely
        if any(m in role_raw or m in link_raw for m in _CLOSED_MARKERS):
            skipped += 1
            continue

        company = _clean_text(company_raw)
        # Sub-listing: inherit the previous company
        if not company or _SUBLISTING_MARKER in company_raw or company == "↳":
            company = last_company
        if company:
            last_company = company

        title = _clean_text(role_raw)
        location = _clean_text(location_raw) or "Not specified"
        job_url = _extract_url(link_raw, role_raw)

        if not title or not job_url or not company:
            skipped += 1
            continue

        # Gate 1: excluded levels / non-SWE disciplines
        if is_unwanted_role(title):
            skipped += 1
            continue

        # Gate 2: must map to a known domain
        domain = classify_domain(title)
        if not domain:
            skipped += 1
            continue

        # Classification (fall back to source defaults when unclear)
        track, term = classify_track(title, role_raw)
        if track == "unclear":
            track, term = default_track, term_hint

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

    return payloads, skipped


def run_github_fetch():
    print("[GitHub Scout] Fetching curated early-career repositories...")
    total_new = 0
    total_skipped = 0

    for src in SOURCES:
        print(f"  Fetching: {src['url'].split('/')[-3]} ({src['default_track']})...")
        try:
            resp = requests.get(src["url"], timeout=10)
        except requests.RequestException as e:
            print(f"    -> Network error: {e}")
            continue

        if resp.status_code != 200:
            print(f"    -> HTTP {resp.status_code} (repo may use a different branch/path)")
            continue

        payloads, skipped = parse_markdown_table(
            resp.text, src["default_track"], src["term_hint"]
        )
        added = sum(1 for p in payloads if insert_job(p))
        dupes = len(payloads) - added
        print(f"    -> Added {added} jobs | Skipped {skipped} | Duplicate {dupes}")
        total_new += added
        total_skipped += skipped

    print("==========================================")
    print(f"GitHub Scout Complete: {total_new} added to jobs.db")
    print("==========================================")


if __name__ == "__main__":
    run_github_fetch()
