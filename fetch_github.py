import re
import requests
from db import insert_job
from classifier import is_unwanted_role, classify_track, classify_domain

# Raw Markdown source lists for early-career roles
SOURCES = [
    {
        "url": "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/README.md",
        "default_track": "internship",
        "term_hint": "Summer 2027"
    },
    {
        "url": "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md",
        "default_track": "full_time",
        "term_hint": "New Grad 2027"
    }
]

def parse_markdown_table(content: str, default_track: str, term_hint: str):
    """
    Parses typical GitHub internship/new-grad Markdown tables.
    Format usually: | Company | Role | Location | Application/Link | Date Posted |
    """
    rows_added = 0
    rows_skipped = 0
    
    # Regex to capture markdown links: [Title](URL)
    link_pattern = re.compile(r'\[(.*?)\]\((.*?)\)')
    
    for line in content.splitlines():
        if not line.startswith("|"):
            continue
        
        parts = [p.strip() for p in line.split("|")[1:-1]]
        if len(parts) < 4:
            continue
            
        company_raw = parts[0]
        role_raw = parts[1]
        location_raw = parts[2]
        link_raw = parts[3]
        
        # Skip header rows
        if "Company" in company_raw or "---" in company_raw:
            continue
            
        # Extract clean company name (strip markdown link if present)
        comp_match = link_pattern.search(company_raw)
        company = comp_match.group(1) if comp_match else company_raw
        
        # Extract clean role title
        role_match = link_pattern.search(role_raw)
        title = role_match.group(1) if role_match else role_raw
        
        # Extract URL
        url_match = link_pattern.search(link_raw)
        if url_match:
            job_url = url_match.group(2)
        elif link_raw.startswith("http"):
            job_url = link_raw
        else:
            # Check if role itself contained the application link
            if role_match and role_match.group(2).startswith("http"):
                job_url = role_match.group(2)
            else:
                continue
        
        # Filtering Gate 1: Check excluded titles (seniors, managers, civil/mech)
        if is_unwanted_role(title):
            rows_skipped += 1
            continue
            
        # Filtering Gate 2: Categorize domain (SWE, Systems, AI/ML)
        domain = classify_domain(title)
        if not domain:
            rows_skipped += 1
            continue
            
        # Classification
        track, term = classify_track(title, role_raw)
        if track == "unclear":
            track = default_track
            term = term_hint
            
        is_remote = "remote" in location_raw.lower()
        
        payload = {
            "company": company,
            "title": title,
            "url": job_url,
            "location": location_raw,
            "is_remote": is_remote,
            "track": track,
            "term": term,
            "domain": domain,
            "source": "github_early_career",
            "raw_description": f"Sourced from curated early-career list: {title} at {company} ({location_raw})"
        }
        
        if insert_job(payload):
            rows_added += 1
        else:
            rows_skipped += 1
            
    return rows_added, rows_skipped

def run_github_fetch():
    print("[GitHub Scout] Fetching curated early-career repositories...")
    total_new = 0
    total_skipped = 0
    
    for src in SOURCES:
        print(f"  Fetching: {src['url'].split('/')[-3]} ({src['default_track']})...")
        try:
            resp = requests.get(src["url"], timeout=10)
            if resp.status_code == 200:
                added, skipped = parse_markdown_table(
                    resp.text, 
                    src["default_track"], 
                    src["term_hint"]
                )
                print(f"    -> Added {added} jobs | Skipped/Duplicate {skipped}")
                total_new += added
                total_skipped += skipped
            else:
                print(f"    -> HTTP {resp.status_code} (repo may use a different branch/path)")
        except Exception as e:
            print(f"    -> Error fetching: {e}")
            
    print("==========================================")
    print(f"GitHub Scout Complete: {total_new} added to jobs.db")
    print("==========================================")

if __name__ == "__main__":
    run_github_fetch()
