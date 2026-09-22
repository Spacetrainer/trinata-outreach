#!/usr/bin/env python3
"""
Trinata Outreach Automation -- Phase 4: Contact Finding

Looks at every business the fit-check robot marked "Fit-checked" and tries
to find someone to write to: a name, a job title, and above all an email
address. It never sends anything and never guesses -- if nothing real turns
up, it says so plainly.

It tries four places, cheapest first, and stops as soon as one works:
  1. The email already sitting on the map (free -- Phase 2 may have found one).
  2. The business's own website, read for a contact address (free).
  3. Hunter (Domain Search) -- uses Hunter's free monthly credits.
  4. Apollo -- a free people-search first (costs nothing), and only if that
     finds someone in a matching role does it spend one of Apollo's free
     monthly credits to confirm their email.

Writes: Contact Name, Contact Title, Contact Email, Message Status
("Contact found" or "No contact found"), and a new "Contact Notes" column
(added automatically, the same way Phase 3 added "Fit-check Notes") saying
where the answer came from.

Script version: 1 (22 Sep 2026)
"""

import os
import re
import sys
import html
import json
import time
import ipaddress
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin

import requests
import gspread
from google.oauth2.service_account import Credentials

# ---------------------------------------------------------------------------
# Settings -- change these if you want different behaviour
# ---------------------------------------------------------------------------

SCRIPT_VERSION = "1 (22 Sep 2026)"

DEFAULT_MAX_ROWS_PER_RUN = 10
MAX_APOLLO_LOOKUPS_PER_RUN = 2  # each one spends 1 of Apollo's free monthly credits;
                                # 2/day x 30 stays safely under Apollo's 75/month free allowance

FETCH_CONNECT_TIMEOUT = 8
FETCH_READ_TIMEOUT = 15
FETCH_BYTES_LIMIT = 400_000
MAX_REDIRECTS = 4
USER_AGENT = "Mozilla/5.0 (compatible; TrinataContactFinder/1.0; +https://trinata.org)"

PAUSE_BETWEEN_ROWS = 1.0  # seconds, to stay gentle with Google Sheets

CONTACT_NOTES_HEADER = "Contact Notes"
NOTE_CHAR_LIMIT = 600

REQUIRED_HEADERS = [
    "Company Name", "Website", "Contact Name", "Contact Title",
    "Contact Email", "Message Status", "Business Email",
]

# The titles from Trinata's target-contact list, plus a few realistic
# additions for very small businesses (Owner, Proprietor, General Manager)
# where there is unlikely to be a formal "CEO" or "Managing Director" at all.
# Flagged here, and in the handoff notes, as an addition beyond the original
# list rather than something silently assumed.
TITLE_PRIORITY = [
    (1, ["founder", "co-founder", "cofounder", "ceo", "chief executive officer",
         "managing director", " md ", "owner", "proprietor"]),
    (2, ["coo", "chief operating officer", "cto", "chief technology officer",
         "head of digital"]),
    (3, ["head of operations", "operations manager", "head of marketing",
         "marketing manager", "head of hr", "hr manager",
         "human resources manager", "general manager"]),
]
TARGET_TITLES_FOR_APOLLO = [
    "Founder", "Co-Founder", "CEO", "Chief Executive Officer", "Managing Director",
    "Owner", "Proprietor", "COO", "Chief Operating Officer", "CTO",
    "Chief Technology Officer", "Head of Digital", "Head of Operations",
    "Operations Manager", "Head of Marketing", "Marketing Manager",
    "Head of HR", "HR Manager", "General Manager",
]

# Addresses that are a social page, directory, delivery app or booking
# platform, not a business's own website -- copied from the same list
# fit_check.py uses, so both robots agree on what counts as a "real" site.
NOT_OWN_WEBSITE_HOSTS = {
    "instagram.com", "facebook.com", "fb.com", "fb.me", "tiktok.com", "twitter.com",
    "x.com", "linktr.ee", "linkin.bio", "wa.me", "whatsapp.com", "youtube.com",
    "youtu.be", "linkedin.com", "threads.net", "t.me", "pinterest.com", "snapchat.com",
    "tripadvisor.com", "yelp.com", "foursquare.com", "google.com", "goo.gl", "g.page",
    "ubereats.com", "glovoapp.com", "jumia.com.ng", "jumia.food", "chowdeck.com",
    "zomato.com", "opentable.com", "restaurantguru.com", "wanderlog.com",
    "yellowpages.com.ng", "businesslist.com.ng", "vconnect.com", "cybo.com",
    "dinesurf.com", "reviewit.ng",
}

# Email addresses that turn up on many small-business websites but are not a
# real contact address -- template placeholders, or a website builder's own
# tracking/analytics addresses.
JUNK_EMAIL_DOMAINS = {
    "example.com", "domain.com", "yourdomain.com", "email.com", "yourname.com",
    "sentry.io", "wixpress.com", "godaddy.com", "godaddysites.com", "schema.org",
    "w3.org", "squarespace.com", "wordpress.com", "gravatar.com", "sentry-next.io",
}

EMAIL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+\-]*@[A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z]{2,}")

APOLLO_BASE = "https://api.apollo.io/api/v1"


# ---------------------------------------------------------------------------
# Small pure helpers (these are what the tests check directly)
# ---------------------------------------------------------------------------

def normalize_domain(website):
    """Turn whatever is in the Website cell into a bare domain, or None."""
    if not website:
        return None
    w = website.strip()
    if not w:
        return None
    if "://" not in w:
        w = "http://" + w
    parsed = urlparse(w)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return None
    return host


def is_directory_or_social(domain):
    """True if this is a social page, directory, or booking/delivery platform."""
    if not domain:
        return True
    return any(domain == h or domain.endswith("." + h) for h in NOT_OWN_WEBSITE_HOSTS)


def is_plausible_email(value):
    if not value:
        return False
    return bool(EMAIL_RE.fullmatch(value.strip()))


def title_priority(title_text):
    """Lower number = more senior / more likely to be the decision-maker.
    999 means the title doesn't match anything on our list at all."""
    t = f" {(title_text or '').strip().lower()} "
    for priority, keywords in TITLE_PRIORITY:
        for kw in keywords:
            if kw in t:
                return priority
    return 999


def extract_email_from_html(page_text, base_domain):
    """Pull the best-looking real contact email out of a page's HTML, or
    return None. Prefers an address on the business's own domain; ignores
    known template/placeholder addresses. Never invents anything -- only
    reports an address that is actually printed on the page."""
    if not page_text:
        return None
    mailto_matches = re.findall(r'mailto:([^"\'?&\s<>]+)', page_text, flags=re.I)
    candidates = list(mailto_matches) + EMAIL_RE.findall(page_text)

    seen = []
    for raw in candidates:
        cleaned = html.unescape(raw).strip().rstrip(".,;:")
        if not EMAIL_RE.fullmatch(cleaned):
            continue
        email_domain = cleaned.split("@", 1)[1].lower()
        if any(email_domain == j or email_domain.endswith("." + j) for j in JUNK_EMAIL_DOMAINS):
            continue
        low = cleaned.lower()
        if low not in seen:
            seen.append(low)

    if not seen:
        return None
    for candidate in seen:
        if base_domain and candidate.endswith("@" + base_domain.lower()):
            return candidate
    return seen[0]


def col_letter(index):
    """1 -> A, 26 -> Z, 27 -> AA, and so on."""
    letters = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def build_header_map(headers):
    header_map = {}
    for i, h in enumerate(headers, start=1):
        if h and h.strip():
            header_map[h.strip()] = i
    return header_map


def select_waiting_rows(all_values, header_map):
    """Given the Sheet's full grid (header row + data rows) and a header
    name -> column number map, return the sheet row numbers (2, 3, ...)
    that are Fit-checked with no Contact Email yet."""
    waiting = []
    status_col = header_map.get("Message Status")
    email_col = header_map.get("Contact Email")
    for idx, row in enumerate(all_values[1:], start=2):
        status = row[status_col - 1].strip() if status_col and status_col <= len(row) else ""
        email_now = row[email_col - 1].strip() if email_col and email_col <= len(row) else ""
        if status == "Fit-checked" and not email_now:
            waiting.append(idx)
    return waiting


def get_cell(row, header_map, name):
    col = header_map.get(name)
    if not col or col > len(row):
        return ""
    return row[col - 1].strip()


def build_row_updates(row_num, header_map, result, source, note, notes_col):
    """Turn one row's result into the list of gspread batch_update ranges."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    full_note = f"{today} | source: {source} | {note}"
    updates = []

    def set_cell(name, value):
        col = header_map[name]
        updates.append({"range": f"{col_letter(col)}{row_num}", "values": [[value]]})

    if result.get("contact_name"):
        set_cell("Contact Name", result["contact_name"])
    if result.get("contact_title"):
        set_cell("Contact Title", result["contact_title"])
    if result.get("contact_email"):
        set_cell("Contact Email", result["contact_email"])

    new_status = "Contact found" if result.get("contact_email") else "No contact found"
    set_cell("Message Status", new_status)
    updates.append({"range": f"{col_letter(notes_col)}{row_num}", "values": [[full_note[:NOTE_CHAR_LIMIT]]]})
    return updates


# ---------------------------------------------------------------------------
# Safe website fetching (refuses private/internal addresses, even on a redirect)
# ---------------------------------------------------------------------------

def _host_is_private(hostname):
    """True only if EVERY address this hostname resolves to is private,
    internal, or otherwise unsafe. A stray or misconfigured second address
    (a placeholder IPv6 record is common with budget hosting) must not
    block a site that also has a perfectly normal public address -- only
    a hostname with nowhere safe to go at all gets refused."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return True
    for info in infos:
        ip = info[4][0]
        try:
            ip_obj = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if not (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local
                or ip_obj.is_reserved or ip_obj.is_multicast or ip_obj.is_unspecified):
            return False  # found at least one safe, public address -- good enough
    return True  # nothing resolved, or every address that did was unsafe


def safe_fetch(url, max_redirects=MAX_REDIRECTS, get=requests.get, host_check=_host_is_private):
    """Fetch a page's text, following redirects by hand and refusing to
    follow one into a private or internal address. Returns (text_or_None,
    a short plain-English note)."""
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en"}
    current_url = url
    for _ in range(max_redirects + 1):
        parsed = urlparse(current_url)
        if parsed.scheme not in ("http", "https"):
            return None, "not a web address"
        if not parsed.hostname:
            return None, "no host in that address"
        if host_check(parsed.hostname):
            return None, "address points at a private or internal network; refused"
        try:
            resp = get(current_url, headers=headers,
                       timeout=(FETCH_CONNECT_TIMEOUT, FETCH_READ_TIMEOUT),
                       allow_redirects=False, stream=True)
        except requests.RequestException as e:
            return None, f"could not connect ({e.__class__.__name__})"
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            if not location:
                return None, f"redirect with no destination (status {resp.status_code})"
            current_url = urljoin(current_url, location)
            continue
        if resp.status_code != 200:
            return None, f"the site answered with error code {resp.status_code}"
        content = b""
        for chunk in resp.iter_content(8192):
            content += chunk
            if len(content) >= FETCH_BYTES_LIMIT:
                break
        encoding = resp.encoding or "utf-8"
        try:
            return content.decode(encoding, errors="ignore"), "ok"
        except LookupError:
            return content.decode("utf-8", errors="ignore"), "ok"
    return None, "too many redirects"


# ---------------------------------------------------------------------------
# Hunter (Domain Search)
# ---------------------------------------------------------------------------

def try_hunter(domain, api_key, get=requests.get):
    try:
        resp = get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": domain, "api_key": api_key, "limit": 10},
            timeout=(FETCH_CONNECT_TIMEOUT, FETCH_READ_TIMEOUT),
        )
    except requests.RequestException as e:
        return None, f"could not be reached ({e.__class__.__name__})"

    try:
        payload = resp.json()
    except ValueError:
        return None, f"returned something unexpected (status {resp.status_code})"

    if resp.status_code != 200:
        errors = payload.get("errors") if isinstance(payload, dict) else None
        detail = errors[0].get("details") if errors else None
        return None, f"error (status {resp.status_code}): {detail or 'no detail given'}"

    data = (payload or {}).get("data") or {}
    emails = data.get("emails") or []
    if not emails:
        return None, "has no email on file for this domain"

    best = None
    best_priority = 1000
    for e in emails:
        position = (e.get("position") or "").strip()
        priority = title_priority(position) if position else 999
        confidence = e.get("confidence") or 0
        if best is None or priority < best_priority or (
            priority == best_priority and confidence > (best.get("confidence") or 0)
        ):
            best = e
            best_priority = priority

    name = " ".join(filter(None, [best.get("first_name"), best.get("last_name")])).strip()
    result = {
        "contact_name": name,
        "contact_title": best.get("position") or "",
        "contact_email": best.get("value") or "",
    }
    return result, f"found {len(emails)} address(es) on file; used the best match"


# ---------------------------------------------------------------------------
# Apollo (free people search, then a paid-in-credits email confirmation)
# ---------------------------------------------------------------------------

def _apollo_headers(api_key):
    return {"Content-Type": "application/json", "x-api-key": api_key, "accept": "application/json"}


def try_apollo(domain, api_key, budget, post=requests.post):
    try:
        resp = post(
            f"{APOLLO_BASE}/mixed_people/api_search",
            headers=_apollo_headers(api_key),
            json={
                "q_organization_domains_list": [domain],
                "person_titles": TARGET_TITLES_FOR_APOLLO,
                "page": 1,
                "per_page": 10,
            },
            timeout=(FETCH_CONNECT_TIMEOUT, 20),
        )
    except requests.RequestException as e:
        return None, f"could not be reached ({e.__class__.__name__})"

    if resp.status_code != 200:
        return None, f"search error (status {resp.status_code})"

    try:
        payload = resp.json()
    except ValueError:
        return None, "returned something that wasn't the expected answer"

    people = payload.get("people") or []
    if not people:
        return None, "has no one on file at this business matching our target job titles"

    ranked = sorted(people, key=lambda p: title_priority(p.get("title") or ""))
    top = ranked[0]
    if title_priority(top.get("title") or "") >= 999:
        return None, "lists people at this business, but none in a matching role"

    if budget["apollo_lookups"] >= MAX_APOLLO_LOOKUPS_PER_RUN:
        return None, "found a possible match, but this run's Apollo budget is used up; will try again another day"

    full_name = (top.get("name") or "").strip()
    parts = full_name.split(None, 1)
    first_name = parts[0] if parts else ""
    last_name = parts[1] if len(parts) > 1 else ""

    try:
        match_resp = post(
            f"{APOLLO_BASE}/people/match",
            headers=_apollo_headers(api_key),
            json={
                "first_name": first_name,
                "last_name": last_name,
                "organization_domain": domain,
                "reveal_personal_emails": False,
                "reveal_phone_number": False,
            },
            timeout=(FETCH_CONNECT_TIMEOUT, 20),
        )
    except requests.RequestException as e:
        return None, f"could not be reached to confirm the email ({e.__class__.__name__})"

    budget["apollo_lookups"] += 1

    if match_resp.status_code != 200:
        return None, f"found {full_name} ({top.get('title')}) but could not confirm an email (status {match_resp.status_code})"

    try:
        match_payload = match_resp.json()
    except ValueError:
        return None, "the email-confirmation answer wasn't in the expected form"

    person = match_payload.get("person") or {}
    email = (person.get("email") or "").strip()
    if not email or "not_unlocked" in email.lower():
        return None, f"found {full_name} ({top.get('title')}) but has no email on file for them"

    result = {
        "contact_name": person.get("name") or full_name,
        "contact_title": person.get("title") or top.get("title") or "",
        "contact_email": email,
    }
    return result, "found and confirmed (used 1 of this run's Apollo lookups)"


# ---------------------------------------------------------------------------
# The core decision: try each source, cheapest first, stop at the first hit
# ---------------------------------------------------------------------------

def find_contact(company, website, business_email, hunter_key, apollo_key, budget,
                  fetch=safe_fetch, hunter=try_hunter, apollo=try_apollo):
    notes = []
    domain = normalize_domain(website)
    has_real_site = domain is not None and not is_directory_or_social(domain)

    if is_plausible_email(business_email):
        return (
            {"contact_name": "", "contact_title": "", "contact_email": business_email.strip()},
            "map",
            "Email was already listed next to this business on the map.",
        )

    if not has_real_site:
        notes.append("no usable website was listed (only a social page, a directory, or nothing at all)")
        return (
            {"contact_name": "", "contact_title": "", "contact_email": ""},
            "none",
            "No contact found. " + " | ".join(notes),
        )

    page_text, fetch_note = fetch(f"https://{domain}")
    if not page_text:
        page_text, fetch_note = fetch(f"http://{domain}")
    if page_text:
        found = extract_email_from_html(page_text, domain)
        if found:
            return (
                {"contact_name": "", "contact_title": "", "contact_email": found},
                "website",
                "Found on the business's own website.",
            )
        notes.append("read the website but found no email on it")
    else:
        notes.append(f"could not read the website ({fetch_note})")

    if hunter_key:
        hunter_result, hunter_note = hunter(domain, hunter_key)
        if hunter_result and hunter_result.get("contact_email"):
            return (hunter_result, "hunter", "Hunter " + hunter_note)
        notes.append(f"Hunter {hunter_note}")
    else:
        notes.append("Hunter not tried (no key set)")

    if apollo_key:
        apollo_result, apollo_note = apollo(domain, apollo_key, budget)
        if apollo_result and apollo_result.get("contact_email"):
            return (apollo_result, "apollo", "Apollo " + apollo_note)
        notes.append(f"Apollo {apollo_note}")
    else:
        notes.append("Apollo not tried (no key set)")

    return (
        {"contact_name": "", "contact_title": "", "contact_email": ""},
        "none",
        "No contact found. " + " | ".join(notes),
    )


# ---------------------------------------------------------------------------
# Sheet plumbing
# ---------------------------------------------------------------------------

def ensure_contact_notes_column(worksheet, header_map, headers):
    if CONTACT_NOTES_HEADER in header_map:
        return header_map[CONTACT_NOTES_HEADER]
    new_index = len(headers) + 1
    worksheet.update(values=[[CONTACT_NOTES_HEADER]],
                      range_name=f"{col_letter(new_index)}1",
                      value_input_option="RAW")
    header_map[CONTACT_NOTES_HEADER] = new_index
    return new_index


def write_summary(lines):
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write("## Contact-finding summary\n\n")
            for line in lines:
                f.write(f"- {line}\n")
    else:
        for line in lines:
            print(line)


def main():
    report_lines = []

    def report(line):
        report_lines.append(line)
        print(line)

    report(f"Script version: {SCRIPT_VERSION}")

    google_key_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY")
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    hunter_key = os.environ.get("HUNTER_API_KEY") or None
    apollo_key = os.environ.get("APOLLO_API_KEY") or None
    try:
        max_rows = int(os.environ.get("MAX_ROWS_PER_RUN") or DEFAULT_MAX_ROWS_PER_RUN)
    except ValueError:
        max_rows = DEFAULT_MAX_ROWS_PER_RUN
    if max_rows < 1:
        max_rows = 1

    if not google_key_json or not sheet_id:
        report("STOPPED: a GitHub secret is missing or empty (GOOGLE_SERVICE_ACCOUNT_KEY or GOOGLE_SHEET_ID). Add it with the exact name.")
        write_summary(report_lines)
        sys.exit(1)

    try:
        creds_info = json.loads(google_key_json)
    except json.JSONDecodeError:
        report("STOPPED: the Google key doesn't look like valid JSON. Paste GOOGLE_SERVICE_ACCOUNT_KEY again.")
        write_summary(report_lines)
        sys.exit(1)

    try:
        creds = Credentials.from_service_account_info(
            creds_info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sheet_id)
        ws = sh.sheet1
    except PermissionError:
        report("STOPPED: the robot is not allowed into the Sheet. Share the Sheet with the service account's email as Editor.")
        write_summary(report_lines)
        sys.exit(1)
    except gspread.exceptions.APIError as e:
        report(f"STOPPED: could not open the Sheet ({e}). Check the GOOGLE_SHEET_ID secret.")
        write_summary(report_lines)
        sys.exit(1)

    headers = ws.row_values(1)
    header_map = build_header_map(headers)
    missing = [h for h in REQUIRED_HEADERS if h not in header_map]
    if missing:
        report(f"STOPPED: the Sheet is missing column heading(s): {', '.join(missing)}. Put them back, spelled exactly as before.")
        write_summary(report_lines)
        sys.exit(1)

    notes_col = ensure_contact_notes_column(ws, header_map, headers)

    all_values = ws.get_all_values()
    waiting = select_waiting_rows(all_values, header_map)
    report(f"Businesses waiting for a contact: {len(waiting)}")

    if not hunter_key:
        report("Note: no HUNTER_API_KEY set; Hunter will be skipped.")
    if not apollo_key:
        report("Note: no APOLLO_API_KEY set; Apollo will be skipped.")

    budget = {"apollo_lookups": 0}
    counts = {"map": 0, "website": 0, "hunter": 0, "apollo": 0, "none": 0}

    to_process = waiting[:max_rows]
    for row_num in to_process:
        row = all_values[row_num - 1]
        company = get_cell(row, header_map, "Company Name")
        website = get_cell(row, header_map, "Website")
        business_email = get_cell(row, header_map, "Business Email")

        try:
            result, source, note = find_contact(
                company, website, business_email,
                hunter_key, apollo_key, budget,
            )
        except Exception as e:  # a bad row should never stop the whole run
            result, source, note = (
                {"contact_name": "", "contact_title": "", "contact_email": ""},
                "none",
                f"Something went wrong checking this row ({e.__class__.__name__}); left for a later run.",
            )

        counts[source] = counts.get(source, 0) + 1
        updates = build_row_updates(row_num, header_map, result, source, note, notes_col)

        for attempt in range(3):
            try:
                ws.batch_update([dict(u) for u in updates], value_input_option="RAW")
                break
            except gspread.exceptions.APIError as e:
                if "429" in str(e) and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                report(f"Could not write row {row_num}: {e}")
        time.sleep(PAUSE_BETWEEN_ROWS)

    report(f"Checked this run: {len(to_process)}")
    report(f"Found via the map: {counts.get('map', 0)}")
    report(f"Found by reading the website: {counts.get('website', 0)}")
    report(f"Found via Hunter: {counts.get('hunter', 0)}")
    report(f"Found via Apollo: {counts.get('apollo', 0)}")
    report(f"No contact found: {counts.get('none', 0)}")
    report(f"Apollo lookups used this run: {budget['apollo_lookups']} (of {MAX_APOLLO_LOOKUPS_PER_RUN} allowed)")
    report(f"Still waiting for a later run: {len(waiting) - len(to_process)}")

    write_summary(report_lines)


if __name__ == "__main__":
    main()
