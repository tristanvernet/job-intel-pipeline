import csv
from jobspy import scrape_jobs
from db import insert_job
from classifier import is_unwanted_role, classify_track, classify_domain

SEARCH_QUERIES = [
    # Track 1: Full-Time Entry Level
    {"query": '"Entry Level Software Engineer"', "default_track": "full_time"},
    {"query": '"Junior Software Engineer"', "default_track": "full_time"},
    {"query": '"Associate Software Engineer"', "default_track": "full_time"},
    {"query": '"Junior Systems Engineer"', "default_track": "full_time"},
    # Track 2: Internships
    {"query": '"Software Engineer Intern" 2027', "default_track": "internship"},
    {"query": '"Software Engineering Intern"', "default_track": "internship"},
    {"query": '"Machine Learning Intern"', "default_track": "internship"},
    {"query": '"Systems Intern"', "default_track": "internship"},
]

def run_scout(results_per_query: int = 10):
    total_added = 0
    total_skipped = 0
    total_duplicates = 0
    
    for item in SEARCH_QUERIES:
        query = item["query"]
        print(f"\n[Scout] Searching: {query}...")
        try:
            jobs_df = scrape_jobs(
                site_name=["indeed", "zip_recruiter"],
                search_term=query,
                location="United States",
                results_wanted=results_per_query,
                hours_old=72,
                country_indeed="USA"
            )
            
            if jobs_df is None or jobs_df.empty:
                print(f"  No results found for {query}")
                continue
                
            for _, row in jobs_df.iterrows():
                title = str(row.get("title") or "")
                
                # Gate 1: Check excluded titles (seniors, technicians, non-tech)
                if is_unwanted_role(title):
                    total_skipped += 1
                    continue
                
                # Gate 2: Verify domain match
                domain = classify_domain(title)
                if not domain:
                    total_skipped += 1
                    continue
                
                description = str(row.get("description") or "")
                track, term = classify_track(title, description)
                
                # Gate 3: Skip unverified seniority
                if track == "unclear":
                    total_skipped += 1
                    continue

                company = str(row.get("company") or "Unknown")
                job_url = str(row.get("job_url") or "")
                location = str(row.get("location") or "USA")
                is_remote = bool(row.get("is_remote", False))
                date_posted = str(row.get("date_posted") or "")
                
                payload = {
                    "company": company,
                    "title": title,
                    "url": job_url,
                    "location": location,
                    "is_remote": is_remote,
                    "track": track,
                    "term": term,
                    "domain": domain,
                    "source": str(row.get("site") or "jobspy"),
                    "date_posted": date_posted,
                    "raw_description": description
                }
                
                if insert_job(payload):
                    print(f"  + Added: [{track.upper()}] [{domain}] {company} - {title}")
                    total_added += 1
                else:
                    total_duplicates += 1

        except Exception as e:
            print(f"  Error querying {query}: {e}")
            
    print(f"\n==========================================")
    print(f"Scout Complete: {total_added} added | {total_skipped} filtered out | {total_duplicates} duplicates.")
    print(f"==========================================")

if __name__ == "__main__":
    run_scout(results_per_query=6)
