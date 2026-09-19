#!/usr/bin/env python3
"""
Trinata prospect sourcing robot -- Phase 2 (free OpenStreetMap version)
=======================================================================

WHAT IT DOES, IN PLAIN WORDS
  1. Asks OpenStreetMap -- a free, community-built map, a bit like Google
     Maps that anyone may use and save -- for every restaurant in Lagos.
  2. Throws out chain outlets (big brands with many branches) and anything
     without a name.
  3. Adds the best NEW ones to your "Trinata Outreach" Google Sheet, up to
     a fixed number per run (25 unless you change it), each marked
     Message Status = Sourced. "Best" means the restaurants with the most
     contact details on the map come first: website, then email, then phone.
  4. Never adds the same restaurant twice.

It runs by itself every Monday (see .github/workflows/source-prospects.yml)
and can also be started by hand from the GitHub "Actions" tab.

WHY OPENSTREETMAP INSTEAD OF GOOGLE MAPS
  Google's terms do not allow copying and saving business names and
  addresses from Google Maps into your own list, and its Places API needs a
  billing account with a prepayment. OpenStreetMap's data is openly
  licensed, needs no key or card, and costs nothing. (If you ever publish
  this data, the licence asks you to credit "OpenStreetMap contributors".)

WHAT TO KNOW ABOUT THE DATA
  - OpenStreetMap is built by volunteers, so it is patchier than Google:
    many restaurants list no website, phone or email, and a few entries are
    out of date. "No website listed here" does NOT prove a business has no
    website -- the fit-check step (Phase 3) looks properly.
  - The robot never guesses or invents anything. It writes only what the
    map lists.

THE TWO SECRETS THIS NEEDS (kept in GitHub, never written in this file)
  GOOGLE_SERVICE_ACCOUNT_KEY  the robot's own Google login (the .json file)
  GOOGLE_SHEET_ID             the long code in the Sheet's web address

GOOD TO KNOW
  - The pace limit (MAX_NEW_PER_RUN) keeps the Sheet from filling with
    hundreds of rows at once, and keeps the later, paid AI steps affordable.
  - Don't want a restaurant? Don't delete its row (it would be added again
    on a later run). Type SKIP in its Message Status cell instead.
  - To widen the pilot later, add a sub-sector to PILOT_SUBSECTORS and give
    it an entry in SUBSECTORS below. The full sector list stays in your
    project document; most of it needs a different source than a map.
"""

import json
import os
import re
import sys
import time
from datetime import date

import gspread
import requests
from google.auth.exceptions import GoogleAuthError

# ---------------------------------------------------------------------
# 1. WHAT TO SEARCH FOR -- edit this section as the pilot grows
# ---------------------------------------------------------------------
CITY_LABEL = "Lagos, Nigeria"

# Only these sub-sectors are searched right now.
PILOT_SUBSECTORS = ["Restaurants"]

# For each sub-sector: which sector it sits under in your master list, and
# how OpenStreetMap labels that kind of place.
SUBSECTORS = {
    "Restaurants": {
        "sector": "Hospitality & Tourism",
        "osm_tags": [("amenity", "restaurant")],
    },
    # Examples to switch on later (also add the name to PILOT_SUBSECTORS):
    # "Cafes": {"sector": "Hospitality & Tourism", "osm_tags": [("amenity", "cafe")]},
    # "Hotels": {"sector": "Hospitality & Tourism", "osm_tags": [("tourism", "hotel")]},
}

# Where "Lagos" is. First choice: Lagos State's official outline in
# OpenStreetMap, found by its ISO code. Backup: a rectangle around greater
# Lagos, as (south, west, north, east).
LAGOS_ISO_CODE = "NG-LA"
LAGOS_BOX = (6.38, 3.10, 6.75, 3.75)

# ---------------------------------------------------------------------
# 2. PACE AND POLITENESS
# ---------------------------------------------------------------------
DEFAULT_MAX_NEW_PER_RUN = 25   # new rows added to the Sheet per run
ROWS_PER_SAVE = 100            # rows written to the Sheet in one go
PAUSE_BETWEEN_SERVERS = 5      # seconds to wait before trying another map server

# Free public OpenStreetMap servers. The robot tries them in this order.
OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
USER_AGENT = "Trinata-Outreach-Sourcing/1.0 (https://trinata.org)"

# ---------------------------------------------------------------------
# 3. HOW THE SHEET IS LAID OUT
#    The first 16 headings already exist in your Trinata Outreach Sheet.
#    The robot adds the extra ones itself, to the right, on its first run.
# ---------------------------------------------------------------------
PIPELINE_HEADERS = [
    "Company Name", "Website", "Sector", "Sub-sector", "ICP Match",
    "Contact Name", "Contact Title", "Contact Email", "What They Do",
    "Problem/Opportunity", "Message Status", "Date First Sent",
    "Date Last Contact", "Reply Summary", "Meeting Booked", "Notes",
]
EXTRA_HEADERS = ["Source ID", "Address", "Phone", "Business Email", "Date Sourced"]
STATUS_NEW = "Sourced"   # what the fit-check step (Phase 3) looks for

SHEETS_SCOPE = ["https://www.googleapis.com/auth/spreadsheets"]


class SourcingError(Exception):
    """A problem we can explain in plain English (bad key, Sheet not shared...)."""


def log(text=""):
    print(text, flush=True)


def clean(value):
    """Turn whatever we are handed into tidy text."""
    return "" if value is None else str(value).strip()


def first_value(text):
    """OpenStreetMap sometimes lists several values split by ';'. Keep the first."""
    return clean(text).split(";")[0].strip()


def name_key(name):
    """A restaurant's name boiled down for spotting duplicates."""
    return re.sub(r"[^a-z0-9]+", "", clean(name).lower())


# ---------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------
def need_env(name, what):
    value = os.environ.get(name, "").strip()
    if not value:
        raise SourcingError(
            f"The GitHub secret {name} is missing or empty ({what}). "
            "Add it under Settings > Secrets and variables > Actions > "
            "New repository secret."
        )
    return value


def read_max_new():
    raw = os.environ.get("MAX_NEW_PER_RUN", "").strip()
    if not raw:
        return DEFAULT_MAX_NEW_PER_RUN
    try:
        number = int(raw)
        if number < 1:
            raise ValueError
    except ValueError:
        raise SourcingError(
            f"The number of new restaurants per run must be a whole number of 1 or more, but it was '{raw}'."
        )
    return number


# ---------------------------------------------------------------------
# OpenStreetMap side
# ---------------------------------------------------------------------
def build_query(osm_tags, use_outline):
    """The question we send to OpenStreetMap, in its own query language."""
    if use_outline:
        setup = f'rel["ISO3166-2"="{LAGOS_ISO_CODE}"]["boundary"="administrative"];map_to_area->.lagos;'
        scope = "(area.lagos)"
    else:
        south, west, north, east = LAGOS_BOX
        setup = ""
        scope = f"({south},{west},{north},{east})"
    lines = []
    for key, value in osm_tags:
        for kind in ("node", "way"):
            lines.append(f'  {kind}["{key}"="{value}"]{scope};')
    return "[out:json][timeout:120];\n" + setup + "\n(\n" + "\n".join(lines) + "\n);\nout center;"


def run_overpass(query):
    """Send the question to a free OpenStreetMap server, trying the next one if it is busy."""
    last_problem = "no answer"
    for server in OVERPASS_SERVERS:
        try:
            response = requests.post(
                server, data={"data": query},
                headers={"User-Agent": USER_AGENT}, timeout=(15, 200),
            )
        except requests.RequestException as err:
            last_problem = f"{server}: {err}"
            log("  Could not reach one map server; trying the next...")
            continue

        if response.status_code != 200:
            last_problem = f"{server}: code {response.status_code}"
            log(f"  A map server said code {response.status_code} (probably busy); trying the next...")
            time.sleep(PAUSE_BETWEEN_SERVERS)
            continue

        try:
            data = response.json()
        except ValueError:
            last_problem = f"{server}: unreadable answer"
            log("  A map server sent an unreadable answer; trying the next...")
            continue

        remark = clean(data.get("remark"))
        if "error" in remark.lower():
            last_problem = f"{server}: {remark}"
            log("  A map server gave up partway (too busy); trying the next...")
            time.sleep(PAUSE_BETWEEN_SERVERS)
            continue

        return data.get("elements", [])

    raise SourcingError(
        "Could not get data from OpenStreetMap: its free servers are busy or "
        f"unreachable right now (last problem: {last_problem}). Nothing was "
        "changed. Press Run workflow again in a few minutes."
    )


def fetch_elements(osm_tags):
    """Get every matching place: Lagos State outline first, rectangle as backup."""
    methods = [("Lagos State outline", True), ("Lagos rectangle", False)]
    for label, use_outline in methods:
        elements = run_overpass(build_query(osm_tags, use_outline))
        if elements:
            return elements, label
        log(f"  The {label} found nothing; trying the next method...")
    raise SourcingError(
        "OpenStreetMap answered but listed no restaurants for Lagos. That is "
        "unexpected. Nothing was changed. Tell Claude what the run page says."
    )


def parse_element(element):
    """Pick out the useful facts from one OpenStreetMap entry (or say why to skip it)."""
    tags = element.get("tags") or {}
    name = clean(tags.get("name"))
    if not name:
        return {"skip": "no_name"}
    if clean(tags.get("brand")) or clean(tags.get("brand:wikidata")):
        return {"skip": "chain"}

    website = first_value(tags.get("website") or tags.get("contact:website") or tags.get("url"))
    if website and not website.lower().startswith(("http://", "https://")):
        website = "http://" + website
    email = first_value(tags.get("email") or tags.get("contact:email")).lower()
    if "@" not in email:
        email = ""
    phone = first_value(
        tags.get("phone") or tags.get("contact:phone")
        or tags.get("mobile") or tags.get("contact:mobile")
    )

    street = " ".join(
        part for part in (clean(tags.get("addr:housenumber")), clean(tags.get("addr:street"))) if part
    )
    address = ", ".join(
        part for part in (
            street,
            clean(tags.get("addr:suburb") or tags.get("addr:neighbourhood")),
            clean(tags.get("addr:city")),
        ) if part
    ) or clean(tags.get("addr:full"))

    return {
        "name": name,
        "website": website,
        "email": email,
        "phone": phone,
        "address": address,
        "source_id": f"osm:{element.get('type', 'item')}:{element.get('id', '')}",
        "score": (2 if website else 0) + (2 if email else 0) + (1 if phone else 0),
    }


def choose_new(candidates, known_ids, known_names, limit):
    """Best-first, skipping anything already in the Sheet or repeated in the list."""
    ordered = sorted(candidates, key=lambda c: (-c["score"], name_key(c["name"]), c["source_id"]))
    chosen, waiting, seen_keys = [], 0, set()
    for candidate in ordered:
        key = name_key(candidate["name"])
        if candidate["source_id"] in known_ids or key in known_names or key in seen_keys:
            continue
        seen_keys.add(key)
        if len(chosen) < limit:
            chosen.append(candidate)
        else:
            waiting += 1
    return chosen, waiting


# ---------------------------------------------------------------------
# Google Sheets side
# ---------------------------------------------------------------------
def sheets_call(function, *args, **kwargs):
    """Run a Google Sheets action; if Google says 'slow down', wait and retry."""
    for attempt in range(1, 6):
        try:
            return function(*args, **kwargs)
        except gspread.exceptions.APIError as err:
            busy = err.response.status_code in (429, 500, 502, 503, 504)
            if busy and attempt < 5:
                wait = 20 * attempt
                log(f"  Google Sheets is busy; waiting {wait} seconds, then retrying...")
                time.sleep(wait)
                continue
            raise


def open_sheet(key_json, sheet_id):
    """Log the robot in and open the Trinata Outreach Sheet."""
    try:
        key_info = json.loads(key_json)
    except ValueError:
        key_info = None
    if not isinstance(key_info, dict):
        raise SourcingError(
            "The GOOGLE_SERVICE_ACCOUNT_KEY secret is not a complete key file. "
            "Open the downloaded .json file, copy EVERYTHING inside it (from the "
            "first { to the last }) and paste it again as the secret."
        )
    robot_email = key_info.get("client_email", "the robot account")

    try:
        client = gspread.service_account_from_dict(key_info, scopes=SHEETS_SCOPE)
        return client.open_by_key(sheet_id), robot_email
    except PermissionError as err:
        detail = ""
        cause = err.__cause__
        if isinstance(cause, gspread.exceptions.APIError):
            detail = f" Google's own words: \"{cause.error.get('message', '')}\""
        raise SourcingError(
            f"The robot ({robot_email}) is not allowed into the Sheet. Most likely "
            "the Sheet has not been shared with that address: open the Sheet, click "
            "Share, paste the address, set it to Editor. (If Google's words below "
            "mention 'disabled' or 'not been used', switch on the Google Sheets API "
            f"in the Trinata Outreach Google Cloud project instead.){detail}"
        )
    except gspread.exceptions.SpreadsheetNotFound:
        raise SourcingError(
            "Google can't find a Sheet with the ID in the GOOGLE_SHEET_ID secret. "
            "It should be only the long code between /d/ and /edit in the Sheet's "
            "web address -- no spaces, and not the whole link."
        )
    except GoogleAuthError as err:
        raise SourcingError(
            f"Google would not accept the robot's key file ({err}). Check that "
            "the whole .json file was pasted into GOOGLE_SERVICE_ACCOUNT_KEY and "
            "that the key hasn't been deleted in Google Cloud."
        )
    except (ValueError, KeyError) as err:
        raise SourcingError(
            f"The robot's key file looks damaged or is the wrong file ({err}). "
            "Use the .json file downloaded from the service account's Keys tab."
        )


def ensure_headers(sheet):
    """Make sure row 1 has every heading we need, adding missing ones on the right."""
    wanted = PIPELINE_HEADERS + EXTRA_HEADERS
    current = [clean(h) for h in sheets_call(sheet.row_values, 1)]
    missing = [h for h in wanted if h not in current]
    if missing:
        needed_columns = len(current) + len(missing)
        if sheet.col_count < needed_columns:
            sheets_call(sheet.add_cols, needed_columns - sheet.col_count)
        first_new_cell = gspread.utils.rowcol_to_a1(1, len(current) + 1)
        sheets_call(sheet.update, values=[missing], range_name=first_new_cell)
        log(f"Added {len(missing)} new column heading(s): {', '.join(missing)}")
        current = current + missing
    return current


def load_known(sheet, headers):
    """Which restaurants are already in the Sheet (by map ID and by name)."""
    ids_column = headers.index("Source ID") + 1
    names_column = headers.index("Company Name") + 1
    ids = {clean(v) for v in sheets_call(sheet.col_values, ids_column)[1:] if clean(v)}
    names = {name_key(v) for v in sheets_call(sheet.col_values, names_column)[1:] if clean(v)}
    return ids, names


def build_row(headers, candidate, sector, subsector, today):
    """One prospect, laid out to match whatever order the Sheet's headings are in."""
    values = {
        "Company Name": candidate["name"],
        "Website": candidate["website"],
        "Sector": sector,
        "Sub-sector": subsector,
        "Message Status": STATUS_NEW,
        "Source ID": candidate["source_id"],
        "Address": candidate["address"],
        "Phone": candidate["phone"],
        "Business Email": candidate["email"],
        "Date Sourced": today,
    }
    return [values.get(heading, "") for heading in headers]


def save_rows(sheet, rows):
    for start in range(0, len(rows), ROWS_PER_SAVE):
        sheets_call(
            sheet.append_rows, rows[start:start + ROWS_PER_SAVE],
            value_input_option="RAW", insert_data_option="INSERT_ROWS",
        )


def write_run_summary(lines):
    """Show a short report on the GitHub run page (when running on GitHub)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------
# The main job
# ---------------------------------------------------------------------
def main():
    today = date.today().isoformat()
    sheet_id = need_env("GOOGLE_SHEET_ID", "the long code in the Sheet's web address")
    key_json = need_env("GOOGLE_SERVICE_ACCOUNT_KEY", "the robot's .json key file")
    max_new = read_max_new()

    for name in PILOT_SUBSECTORS:
        if name not in SUBSECTORS:
            raise SourcingError(
                f'"{name}" is listed in PILOT_SUBSECTORS but has no entry in SUBSECTORS.'
            )

    spreadsheet, robot_email = open_sheet(key_json, sheet_id)
    prospects = spreadsheet.sheet1
    headers = ensure_headers(prospects)
    known_ids, known_names = load_known(prospects, headers)

    log(f"Connected to the Sheet as {robot_email}.")
    log(f"Pace limit for this run: {max_new} new restaurant(s).\n")

    listed = no_name = chains = added = waiting = 0
    with_site = with_email = with_phone = 0
    method_used = ""
    room_left = max_new

    for subsector in PILOT_SUBSECTORS:
        info = SUBSECTORS[subsector]
        elements, method_used = fetch_elements(info["osm_tags"])
        listed += len(elements)

        candidates = []
        for element in elements:
            parsed = parse_element(element)
            if parsed.get("skip") == "no_name":
                no_name += 1
            elif parsed.get("skip") == "chain":
                chains += 1
            else:
                candidates.append(parsed)
        with_site += sum(1 for c in candidates if c["website"])
        with_email += sum(1 for c in candidates if c["email"])
        with_phone += sum(1 for c in candidates if c["phone"])

        chosen, still_waiting = choose_new(candidates, known_ids, known_names, room_left)
        rows = [build_row(headers, c, info["sector"], subsector, today) for c in chosen]
        if rows:
            save_rows(prospects, rows)
        for c in chosen:
            known_ids.add(c["source_id"])
            known_names.add(name_key(c["name"]))
        added += len(chosen)
        waiting += still_waiting
        room_left -= len(chosen)
        log(f"{subsector}: {len(chosen)} added, {still_waiting} still waiting.")

    usable = listed - no_name - chains
    lines = [
        f"### Prospect sourcing run - {today}",
        f"- OpenStreetMap listed **{listed}** places for {', '.join(PILOT_SUBSECTORS)} in {CITY_LABEL} "
        f"(found using the {method_used}).",
        f"- Skipped: {no_name} with no name, {chains} chain outlets.",
        f"- Of the {usable} that are left: {with_site} list a website, {with_email} an email, "
        f"{with_phone} a phone number.",
        f"- New businesses added to the Sheet: **{added}** (limit this run: {max_new}).",
        f"- Still waiting to be added on later runs: {waiting}.",
    ]
    if added == 0 and waiting == 0:
        lines.append("- Nothing new to add: everything OpenStreetMap lists is already in the Sheet.")

    log("")
    for line in lines:
        log(line)
    write_run_summary(lines)


def run():
    """Start the job; if something we can explain goes wrong, say so plainly."""
    try:
        main()
    except SourcingError as problem:
        log(f"\nSTOPPED: {problem}")
        write_run_summary(["### Prospect sourcing run - stopped", f"{problem}"])
        sys.exit(1)


if __name__ == "__main__":
    run()
