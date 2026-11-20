"""Scrape every Bellhaven community from the public website.

Steps:
  1. Walk the /communities directory, page by page, until there is no "Next" link.
  2. Also collect community links from the homepage (it can mention ones the directory misses).
  3. Visit each community's detail page and pull out its fields.
  4. Save the results to data/website_locations.json and report any missing fields.

This script only reads the public website. It never touches the CRM.
"""
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

SITE = "https://analyst-assessment-production.up.railway.app"
OUTPUT_FILE = Path("data/website_locations.json")
REQUIRED_FIELDS = ["name", "street", "city", "state", "zip", "care_offerings",
                   "administrator", "phone", "url"]

session = requests.Session()


def get_page(url):
    """Download a page and return it parsed, or None if it failed."""
    resp = session.get(url, timeout=30)
    if resp.status_code != 200:
        print(f"  ! Could not load {url} (HTTP {resp.status_code})")
        return None
    return BeautifulSoup(resp.text, "html.parser")


def clean_url(href):
    """Turn a link into a full URL with no trailing slash, query string or fragment."""
    full = urljoin(SITE, href)
    parsed = urlparse(full)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def is_community_link(href):
    """True for links like /communities/bellhaven-of-x (not the directory itself)."""
    path = urlparse(urljoin(SITE, href)).path.rstrip("/")
    return path.startswith("/communities/") and path.count("/") == 2


def links_from_directory():
    """Follow the directory's pages until there is no Next link."""
    found = []
    url = f"{SITE}/communities"
    page_num = 0
    while url:
        page_num += 1
        soup = get_page(url)
        if soup is None:
            break
        for a in soup.select(".card h3 a[href]"):
            found.append(clean_url(a["href"]))
        next_link = soup.find("a", string=re.compile(r"Next", re.I))
        url = urljoin(SITE, next_link["href"]) if next_link else None
    print(f"Directory: {len(found)} community links across {page_num} pages")
    return found


def links_from_homepage():
    """Grab any community link that appears on the homepage."""
    soup = get_page(SITE + "/")
    if soup is None:
        return []
    found = [clean_url(a["href"]) for a in soup.find_all("a", href=True)
             if is_community_link(a["href"])]
    print(f"Homepage: {len(found)} community links")
    return found


def parse_detail_page(url, soup):
    """Pull the fields out of one community page.

    The page lists fields as label/value pairs (<dt>Label</dt><dd>Value</dd>).
    """
    record = {"name": None, "street": None, "city": None, "state": None, "zip": None,
              "care_offerings": [], "administrator": None, "phone": None, "url": url}

    h1 = soup.find("h1")
    if h1:
        record["name"] = h1.get_text(strip=True)

    for dt in soup.select("dl.detail dt"):
        label = dt.get_text(strip=True).lower()
        dd = dt.find_next_sibling("dd")
        if dd is None:
            continue

        if label == "address":
            # Address is "Street<br>City, ST 12345"
            lines = [line.strip() for line in dd.get_text("\n").split("\n") if line.strip()]
            if lines:
                record["street"] = lines[0]
            if len(lines) > 1:
                match = re.match(r"^(.*?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$", lines[-1])
                if match:
                    record["city"], record["state"], record["zip"] = match.groups()
        elif label == "care offerings":
            record["care_offerings"] = [b.get_text(strip=True) for b in dd.select(".badge")]
        elif label == "administrator":
            record["administrator"] = dd.get_text(strip=True) or None
        elif label == "phone":
            record["phone"] = dd.get_text(strip=True) or None

    return record


def scrape():
    """Collect every community. Returns (locations, problems). Saves nothing."""
    directory_links = links_from_directory()
    homepage_links = links_from_homepage()

    # Combine both lists and drop duplicates, keeping first-seen order.
    all_links = list(dict.fromkeys(directory_links + homepage_links))
    extra = [u for u in all_links if u not in directory_links]
    print(f"Combined unique communities: {len(all_links)}")
    for u in extra:
        print(f"  + Found only on homepage: {u}")

    locations = []
    problems = []
    for url in all_links:
        soup = get_page(url)
        if soup is None:
            problems.append((url, ["page did not load"]))
            continue
        record = parse_detail_page(url, soup)
        record["found_on"] = "directory" if url in directory_links else "homepage only"
        locations.append(record)

        missing = [f for f in REQUIRED_FIELDS if not record.get(f)]
        if missing:
            problems.append((url, missing))

    return locations, problems


def save(locations):
    OUTPUT_FILE.parent.mkdir(exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(locations, indent=2), encoding="utf-8")
    print(f"Saved to {OUTPUT_FILE}")


def report(locations, problems):
    print(f"\nTOTAL LOCATIONS SCRAPED: {len(locations)}")
    if problems:
        print(f"\n{len(problems)} page(s) with missing fields:")
        for url, missing in problems:
            print(f"  ! {url} -> missing: {', '.join(missing)}")
    else:
        print("No missing fields.")


def main():
    locations, problems = scrape()
    report(locations, problems)
    save(locations)


if __name__ == "__main__":
    main()
