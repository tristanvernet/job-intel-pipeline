from jobspy import scrape_jobs
from db import insert_job
from classifier import is_unwanted_role, classify_track, classify_domain

# Boards to scrape via jobspy.
SITES = ["indeed", "glassdoor", "zip_recruiter"]

# Expanded query variations across tracks and domains.
SEARCH_QUERIES = [
    # Track 1: Full-Time Entry Level — SWE
    {"query": '"Entry Level Software Engineer"', "default_track": "full_time"},
    {"query": '"Junior Software Engineer"', "default_track": "full_time"},
    {"query": '"Associate Software Engineer"', "default_track": "full_time"},
    {"query": '"New Grad Software Engineer" 2027', "default_track": "full_time"},
    {"query": '"Software Engineer I"', "default_track": "full_time"},
    {"query": '"Graduate Software Developer"', "default_track": "full_time"},
    # Track 1: Full-Time Entry Level — Systems / AI-ML
    {"query": '"Junior Systems Engineer"', "default_track": "full_time"},
    {"query": '"Entry Level Platform Engineer"', "default_track": "full_time"},
    {"query": '"Associate Machine Learning Engineer"', "default_track": "full_time"},
    {"query": '"New Grad Data Engineer"', "default_track": "full_time"},
    # Track 2: Internships — SWE
    {"query": '"Software Engineer Intern" 2027', "default_track": "internship"},
    {"query": '"Software Engineering Intern"', "default_track": "internship"},
    {"query": '"Backend Engineering Intern"', "default_track": "internship"},
    {"query": '"Full Stack Intern"', "default_track": "internship"},
    # Track 2: Internships — Systems / AI-ML
    {"query": '"Machine Learning Intern"', "default_track": "internship"},
    {"query": '"AI Research Intern"', "default_track": "internship"},
    {"query": '"Systems Intern"', "default_track": "internship"},
    {"query": '"Infrastructure Engineering Intern"', "default_track": "internship"},
]


def row_to_payload(row: dict, default_track: str = "unclear"):
    """Convert one jobspy result row into a job payload, or None if filtered.

    Pure function: no network, no DB. `row` is a plain dict (e.g. a DataFrame
    row converted via `.to_dict()`), so it is trivially testable with fixtures.
    """
    title = str(row.get("title") or "").strip()
    if not title:
        return None

    # Gate 1: excluded seniority / non-tech disciplines
    if is_unwanted_role(title):
        return None

    # Gate 2: must match a known domain
    domain = classify_domain(title)
    if not domain:
        return None

    description = str(row.get("description") or "")
    track, term = classify_track(title, description)
    if track == "unclear":
        # Trust the query's intended track before discarding.
        if default_track in ("full_time", "internship"):
            track = default_track
        else:
            return None

    return {
        "company": str(row.get("company") or "Unknown").strip() or "Unknown",
        "title": title,
        "url": str(row.get("job_url") or "").strip(),
        "location": str(row.get("location") or "USA").strip(),
        "is_remote": bool(row.get("is_remote", False)),
        "track": track,
        "term": term,
        "domain": domain,
        "source": str(row.get("site") or "jobspy"),
        "date_posted": str(row.get("date_posted") or ""),
        "raw_description": description,
    }


def run_scout(results_per_query: int = 10):
    total_added = 0
    total_skipped = 0
    total_duplicates = 0

    for item in SEARCH_QUERIES:
        query = item["query"]
        print(f"\n[Scout] Searching: {query}...")
        try:
            jobs_df = scrape_jobs(
                site_name=SITES,
                search_term=query,
                location="United States",
                results_wanted=results_per_query,
                hours_old=72,
                country_indeed="USA",
            )
        except Exception as e:
            print(f"  Error querying {query}: {e}")
            continue

        if jobs_df is None or jobs_df.empty:
            print(f"  No results found for {query}")
            continue

        for _, row in jobs_df.iterrows():
            payload = row_to_payload(row.to_dict(), item["default_track"])
            if payload is None:
                total_skipped += 1
                continue
            if insert_job(payload):
                print(f"  + Added: [{payload['track'].upper()}] [{payload['domain']}] "
                      f"{payload['company']} - {payload['title']}")
                total_added += 1
            else:
                total_duplicates += 1

    print("\n==========================================")
    print(f"Scout Complete: {total_added} added | {total_skipped} filtered out | "
          f"{total_duplicates} duplicates.")
    print("==========================================")


if __name__ == "__main__":
    run_scout(results_per_query=6)
