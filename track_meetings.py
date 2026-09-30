#!/usr/bin/env python3
"""
Trinata Outreach - Phase 8: the meeting tracker.

What it does, in plain words:
  1. Looks at the Google Calendar that holds Abolaji's booking page, in
     "look, don't touch" mode: it can only read, never create, change or
     delete anything. (The calendar is shared with the robot's Google account,
     the same one that already edits the Sheet.)
  2. Picks out calls booked by someone outside Trinata, from 14 days ago to
     4 months ahead.
  3. Works out which business in the Sheet booked, only among businesses we
     have actually contacted (by email or WhatsApp). In order of confidence:
       - the booker's email is that business's Contact Email
       - the booker's email is at that business's own web address
       - a phone number in the booking is that business's WhatsApp or phone
       - the business's name is written in the booking (less sure: the alert
         says "please check")
  4. For each new booking:
       - fills 'Meeting Booked' (the day they booked) and 'Meeting Time' (when
         the call is, Lagos time) on that business's row
       - sets Message Status to 'Meeting booked', which also stops the
         automatic follow-up email. An 'Opted out' business is never changed;
         Abolaji gets a "please check" alert instead.
       - emails Abolaji a short brief to prepare for the call
  5. A call that is moved or cancelled is updated on the row and Abolaji is
     told. A booking it cannot match to any business is also emailed to
     Abolaji. If it was one of his prospects, he can type the Sheet row number
     into the 'Sheet Row' cell on the 'Meetings' tab and the next run links it.
  6. Keeps one line per booking on the 'Meetings' tab, so nothing is handled
     twice.

This robot costs nothing: no Claude, and Google Calendar reading is free.

Privacy: this code lives in a public GitHub repo, so the run log anyone can
see never shows the details of a calendar event that is not an outreach
booking. Those go only to Abolaji's email and the (private) Sheet.

Preview mode (DRY_RUN=yes): reads and reports only. Changes nothing in the
Sheet and sends no emails.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

import draft_messages as dm
import send_emails as se

SCRIPT_VERSION = "1 (29 Sep 2026)"

# ---------------------------------------------------------------- settings
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
CALENDAR_API = "https://www.googleapis.com/calendar/v3/calendars/%s/events"
LOOK_BACK_DAYS = 14          # bookings for calls up to this many days ago
LOOK_AHEAD_DAYS = 120        # and up to this many days ahead
MAX_EVENTS = 1000            # safety limit on how many calendar entries are read per run
OUR_DOMAIN = "trinata.org"
ALERT_TO_DEFAULT = "abolaji@trinata.org"
ALERT_FROM_NAME = "Trinata Outreach Robot"
LAGOS = timezone(timedelta(hours=1))

ST_MEETING = "Meeting booked"   # new status: a call is booked; no automatic emails after this

# Sheet columns this robot adds (found by heading, never by position)
H_MEETING_BOOKED = "Meeting Booked"   # the day they booked (Lagos), e.g. 2026-10-02
H_MEETING_TIME = "Meeting Time"       # when the call is (Lagos), or 'Cancelled (was ...)'
NEW_HEADINGS = [H_MEETING_BOOKED, H_MEETING_TIME]
REQUIRED_HEADINGS = [dm.H_NAME, dm.H_EMAIL, dm.H_STATUS]
H_PHONE = "Phone"

# Rows that count as "contacted": only these can be matched to a booking.
CONTACTED_STATUSES = {s.lower() for s in (
    se.ST_SENT, se.ST_UNCONFIRMED, se.ST_SENDING, dm.ST_FOLLOWED_UP, dm.ST_WA_SENT, dm.ST_WA_READY,
    dm.ST_REPLIED, dm.ST_REPLIED_INTERESTED, dm.ST_OPTED_OUT, ST_MEETING)}

# The 'Meetings' tab: one line per booking
TAB = "Meetings"
M_EVENT = "Event ID"
M_FIRST_SEEN = "First Seen"
M_UPDATED = "Last Change"
M_BUSINESS = "Business"
M_ROW = "Sheet Row"
M_MATCHED_BY = "Matched By"
M_TIME = "Call Time (Lagos)"
M_BOOKER = "Booked By"
M_STATE = "State"
M_NOTES = "Notes"
TAB_HEADINGS = [M_FIRST_SEEN, M_UPDATED, M_BUSINESS, M_ROW, M_MATCHED_BY, M_TIME, M_BOOKER,
                M_STATE, M_NOTES, M_EVENT]
STATE_BOOKED = "Booked"
STATE_CANCELLED = "Cancelled"
STATE_UNMATCHED = "Not matched"

# How a booking was matched, most sure first
BY_EMAIL = "their email address"
BY_DOMAIN = "their company's email domain"
BY_PHONE = "their phone number"
BY_NAME = "the business name (please check)"
BY_HAND = "Sheet row typed in by hand"

BOOKING_HINTS = re.compile(r"booked by|appointment|calendar\.app\.google|booking page|"
                           r"reschedule or cancel|%s" % re.escape(dm.BOOKING_LINK.rsplit("/", 1)[-1]),
                           re.IGNORECASE)
EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
PHONE_IN_TEXT = re.compile(r"\+?\d[\d\s().-]{8,18}\d")


# ---------------------------------------------------------------- small helpers
class StopRun(Exception):
    """The whole run must stop (for example, the calendar is not shared)."""


def log(msg):
    print(msg, flush=True)


def fail(msg):
    log("STOPPED: " + msg)
    se.write_summary(["## Track Meetings - stopped", "", msg])
    sys.exit(1)


def clip(text, limit=dm.NOTE_CHAR_LIMIT):
    return dm.clip(text, limit)


def is_dry_run():
    return (os.environ.get("DRY_RUN") or "").strip().lower() in ("yes", "true", "1", "preview")


def alert_address():
    addr = (os.environ.get("ALERT_EMAIL") or "").strip() or ALERT_TO_DEFAULT
    if dm.check_shape(addr):
        fail("ALERT_EMAIL '%s' is not a valid email address." % addr)
    return addr


def calendar_id():
    cid = (os.environ.get("BOOKING_CALENDAR_ID") or "").strip()
    if not cid:
        fail("BOOKING_CALENDAR_ID is not set as a repo secret. It is the Google account (email "
             "address) that owns your booking page.")
    return cid


def row_dict(headers, row):
    row = list(row) + [""] * (len(headers) - len(row))
    return dict(zip(headers, row))


def lagos_text(dt):
    return dt.astimezone(LAGOS).strftime("%Y-%m-%d %H:%M") if dt else ""


def nice_time(dt):
    """'Fri 2 Oct 2026, 2:00 pm (Lagos)'"""
    if not dt:
        return "(time unknown)"
    d = dt.astimezone(LAGOS)
    hour = d.strftime("%I:%M %p").lstrip("0").lower()
    return "%s %d %s, %s (Lagos)" % (d.strftime("%a"), d.day, d.strftime("%b %Y"), hour)


def parse_time(value):
    """A Calendar start/end ({'dateTime': ...} or {'date': ...}) as an aware datetime, or None."""
    value = value or {}
    text = value.get("dateTime") or ""
    try:
        if text:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        if value.get("date"):
            return datetime.strptime(value["date"], "%Y-%m-%d").replace(tzinfo=LAGOS)
    except ValueError:
        return None
    return None


def parse_stamp(text):
    try:
        dt = datetime.fromisoformat((text or "").replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------- the calendar
class Calendar:
    """Read-only access to the booking calendar. Only ever sends GET requests."""

    def __init__(self, cal_id, session=None):
        self.cal_id = cal_id
        self.session = session

    def _session(self):
        if self.session is None:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account
            raw = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY") or "").strip()
            try:
                info = json.loads(raw)
            except ValueError:
                raise StopRun("GOOGLE_SERVICE_ACCOUNT_KEY is not valid JSON.")
            creds = service_account.Credentials.from_service_account_info(info, scopes=[CALENDAR_SCOPE])
            self.session = AuthorizedSession(creds)
            self.robot_email = info.get("client_email", "the robot's Google account")
        return self.session

    def events(self, now):
        """Every calendar entry whose time is in the window, including cancelled ones."""
        from urllib.parse import quote
        session = self._session()
        url = CALENDAR_API % quote(self.cal_id, safe="")
        params = {
            "timeMin": (now - timedelta(days=LOOK_BACK_DAYS)).isoformat(),
            "timeMax": (now + timedelta(days=LOOK_AHEAD_DAYS)).isoformat(),
            "singleEvents": "true",
            "showDeleted": "true",
            "maxResults": "250",
        }
        found = []
        while True:
            resp = None
            for attempt in range(3):
                try:
                    resp = session.get(url, params=params, timeout=60)
                except Exception as e:  # network trouble
                    resp = None
                    err = e.__class__.__name__
                    time.sleep(5 * (attempt + 1))
                    continue
                if resp.status_code in (429, 500, 502, 503):
                    time.sleep(10 * (attempt + 1))
                    continue
                break
            if resp is None:
                raise StopRun("Could not reach Google Calendar (%s). Nothing was changed." % err)
            if resp.status_code != 200:
                raise StopRun(self._explain(resp))
            data = resp.json()
            found += data.get("items", [])
            token = data.get("nextPageToken")
            if not token or len(found) >= MAX_EVENTS:
                return found[:MAX_EVENTS]
            params["pageToken"] = token

    def _explain(self, resp):
        text = (resp.text or "")[:2000]
        who = getattr(self, "robot_email", "the robot's Google account")
        if resp.status_code == 404:
            return ("Google Calendar says it cannot find the calendar in BOOKING_CALENDAR_ID, or it is not "
                    "shared with %s. In Google Calendar: Settings, your calendar, 'Share with specific "
                    "people', add %s with 'See all event details'. Nothing was changed." % (who, who))
        if resp.status_code == 403 and re.search(r"accessNotConfigured|has not been used|is disabled|"
                                                  r"SERVICE_DISABLED", text):
            return ("The Google Calendar API is switched off for the robot's Google Cloud project. Open "
                    "console.cloud.google.com, pick the project, search 'Google Calendar API' and press "
                    "Enable. Nothing was changed.")
        if resp.status_code in (401, 403):
            return ("Google Calendar refused the robot (status %d). Check the calendar is shared with %s "
                    "with 'See all event details'. Nothing was changed." % (resp.status_code, who))
        return "Google Calendar gave an error (status %d). Nothing was changed." % resp.status_code


# ---------------------------------------------------------------- reading one event
def outside_people(ev, cal_id):
    """[(email, display name)] of the people on this event who are not Abolaji or Trinata."""
    own = {cal_id.strip().lower()}
    for key in ("organizer", "creator"):
        addr = ((ev.get(key) or {}).get("email") or "").strip().lower()
        if addr:
            own.add(addr)
    people = []
    for a in ev.get("attendees") or []:
        addr = (a.get("email") or "").strip().lower()
        if not addr or a.get("self") or a.get("resource") or addr in own \
                or addr.endswith("@" + OUR_DOMAIN) or addr.endswith("calendar.google.com"):
            continue
        people.append((addr, (a.get("displayName") or "").strip()))
    # Some booking pages only write the booker in the description
    if not people:
        for addr in EMAIL_IN_TEXT.findall(ev.get("description") or ""):
            addr = addr.lower()
            if addr not in own and not addr.endswith("@" + OUR_DOMAIN) and \
                    addr not in [p[0] for p in people]:
                people.append((addr, ""))
    return people


def event_text(ev):
    parts = [ev.get("summary") or "", ev.get("description") or "", ev.get("location") or ""]
    parts += [(a.get("displayName") or "") for a in ev.get("attendees") or []]
    text = "\n".join(parts)
    return re.sub(r"<[^>]+>", " ", text)   # descriptions can be HTML


def looks_like_booking(ev, cal_id):
    """A booking-page booking, as far as can be told: someone from outside, and the wording of one."""
    if not outside_people(ev, cal_id):
        return False
    return bool(BOOKING_HINTS.search(event_text(ev)))


def phones_in(text):
    found = []
    for m in PHONE_IN_TEXT.findall(text or ""):
        n = dm.normalize_ng_mobile(m)
        if n and n not in found:
            found.append(n)
    return found


# ---------------------------------------------------------------- matching to the Sheet
def name_tokens(company):
    words = re.findall(r"[a-z0-9]+", (company or "").lower())
    return [w for w in words if len(w) >= 3 and w not in dm.NAME_STOPWORDS]


def build_index(rows):
    """Lookups for the rows we have contacted."""
    by_email, by_domain, by_phone, names = {}, {}, {}, []
    for i, rowd in rows:
        status = (rowd.get(dm.H_STATUS) or "").strip().lower()
        contacted = status in CONTACTED_STATUSES or bool((rowd.get(dm.H_FIRST_SENT) or "").strip())
        if not contacted:
            continue
        addr = (rowd.get(dm.H_EMAIL) or "").strip().lower()
        if addr and "@" in addr:
            by_email.setdefault(addr, []).append(i)
            domain = addr.split("@", 1)[1]
            if domain not in dm.FREE_MAIL_DOMAINS:
                by_domain.setdefault(domain, []).append(i)
        site = dm.host_of(rowd.get(dm.H_WEBSITE, ""))
        if site and "." in site and site not in dm.FREE_MAIL_DOMAINS:
            by_domain.setdefault(site, []).append(i)
        for col in (dm.H_WA_NUMBER, H_PHONE):
            n = dm.normalize_ng_mobile(rowd.get(col, ""))
            if n:
                by_phone.setdefault(n, []).append(i)
        tokens = name_tokens(rowd.get(dm.H_NAME, ""))
        if tokens and max(len(t) for t in tokens) >= 4 and len("".join(tokens)) >= 5:
            names.append((i, tokens))
    return by_email, by_domain, by_phone, names


def match_event(ev, cal_id, index):
    """(sorted row numbers, how matched) for this booking, or ([], '')."""
    by_email, by_domain, by_phone, names = index
    people = outside_people(ev, cal_id)
    rows = set()
    for addr, _ in people:
        rows.update(by_email.get(addr, []))
    if rows:
        return sorted(rows), BY_EMAIL
    for addr, _ in people:
        domain = addr.split("@", 1)[1]
        for d, nums in by_domain.items():
            if domain == d or domain.endswith("." + d):
                rows.update(nums)
    if rows:
        return sorted(rows), BY_DOMAIN
    text = event_text(ev)
    for n in phones_in(text):
        rows.update(by_phone.get(n, []))
    if rows:
        return sorted(rows), BY_PHONE
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    for i, tokens in names:
        if all(t in words for t in tokens):
            rows.add(i)
    if rows:
        return sorted(rows), BY_NAME
    return [], ""


# ---------------------------------------------------------------- the Meetings tab
def open_tab(ws, dry_run):
    """(tab or None, {event id: (line number, {heading: value})})."""
    sh = ws.spreadsheet
    try:
        tab = sh.worksheet(TAB)
    except Exception:
        if dry_run:
            return None, {}
        tab = dm.with_retry(lambda: sh.add_worksheet(title=TAB, rows=500, cols=len(TAB_HEADINGS)),
                            "Adding the Meetings tab")
        dm.with_retry(lambda: tab.update(values=[TAB_HEADINGS], range_name="A1", value_input_option="RAW"),
                      "Writing Meetings headings")
        return tab, {}
    grid = dm.with_retry(tab.get_all_values, "Reading the Meetings tab")
    if not grid or not any(c.strip() for c in grid[0]):
        if not dry_run:
            dm.with_retry(lambda: tab.update(values=[TAB_HEADINGS], range_name="A1",
                                             value_input_option="RAW"), "Writing Meetings headings")
        return tab, {}
    heads = [h.strip() for h in grid[0]]
    missing = [h for h in TAB_HEADINGS if h not in heads]
    if missing:
        raise StopRun("The '%s' tab is missing these headings: %s. Nothing was changed."
                      % (TAB, ", ".join(missing)))
    known = {}
    for n, r in enumerate(grid[1:], start=2):
        d = row_dict(heads, r)
        eid = d.get(M_EVENT, "").strip()
        if eid:
            known[eid] = (n, d)
    tab._trinata_heads = heads
    return tab, known


def save_tab(tab, known_updates, new_lines):
    if tab is None:
        return
    import gspread.utils as gu
    heads = getattr(tab, "_trinata_heads", TAB_HEADINGS)
    if known_updates:
        def build():
            out = []
            for line, values in known_updates:
                for h, v in values.items():
                    out.append({"range": gu.rowcol_to_a1(line, heads.index(h) + 1), "values": [[v]]})
            return out
        dm.with_retry(lambda: tab.batch_update(build(), value_input_option="RAW"), "Updating the Meetings tab")
    if new_lines:
        rows = [[d.get(h, "") for h in heads] for d in new_lines]
        dm.with_retry(lambda: tab.append_rows(rows, value_input_option="RAW"), "Adding to the Meetings tab")


def ensure_columns(ws, headers):
    missing = [h for h in NEW_HEADINGS if h not in headers]
    if not missing:
        return headers
    needed = len(headers) + len(missing)
    if ws.col_count < needed:
        dm.with_retry(lambda: ws.add_cols(needed - ws.col_count), "Adding columns")
    import gspread.utils as gu
    start = gu.rowcol_to_a1(1, len(headers) + 1)
    end = gu.rowcol_to_a1(1, needed)
    dm.with_retry(lambda: ws.update(values=[missing], range_name=start + ":" + end,
                                    value_input_option="RAW"), "Adding headings")
    log("Added new column(s): " + ", ".join(missing))
    return headers + missing


def hand_rows(text, max_row):
    """Row numbers Abolaji typed into 'Sheet Row' (e.g. '12' or '12, 14')."""
    nums = []
    for part in re.findall(r"\d+", text or ""):
        n = int(part)
        if 2 <= n <= max_row and n not in nums:
            nums.append(n)
    return nums


# ---------------------------------------------------------------- alert emails
def build_alert(to_addr, subject, lines):
    msg = EmailMessage()
    msg["From"] = formataddr((ALERT_FROM_NAME, se.SENDER_EMAIL))
    msg["To"] = to_addr
    msg["Subject"] = subject[:150]
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=OUR_DOMAIN)
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content("\n".join(lines + ["", "Sent by the meeting tracker, version %s." % SCRIPT_VERSION]) + "\n")
    return msg


def booker_text(people):
    return "; ".join(("%s <%s>" % (n, a)) if n else a for a, n in people) or "(not shown)"


def brief_lines(rowd, row_nums):
    """What Abolaji needs to prepare: who they are and what we offered them."""
    out = []
    for label, col in (("What they do", dm.H_WHAT), ("What we offered", dm.H_PROBLEM),
                       ("Kind of business", dm.H_SUBSECTOR), ("Website", dm.H_WEBSITE),
                       ("Phone", H_PHONE), ("Contact", dm.H_CONTACT_NAME),
                       ("Our email's subject", dm.H_SUBJECT), ("First contacted", dm.H_FIRST_SENT),
                       ("Their reply, in one line", dm.H_REPLY_NOTES)):
        value = (rowd.get(col) or "").strip()
        if value:
            out.append("%s: %s" % (label, clip(value, 400)))
    out.append("Sheet row: %s" % ", ".join(str(n) for n in row_nums))
    return out


def new_booking_alert(to_addr, rowd, row_nums, when, people, how, opted_out):
    name = rowd.get(dm.H_NAME, "") or "A business"
    lines = ["%s booked a call with you." % name, "",
             "When: " + nice_time(when),
             "Booked by: " + booker_text(people),
             "How the robot knew it was them: " + how, ""]
    if how == BY_NAME:
        lines += ["PLEASE CHECK: this was matched only because the business name appears in the booking. "
                  "If it is the wrong business, clear 'Meeting Booked' and 'Meeting Time' on that row and "
                  "put its Message Status back to what it was.", ""]
    if opted_out:
        lines += ["PLEASE CHECK: this business is marked Opted out, so the robot did not change its status. "
                  "If they now want to talk, that is your call.", ""]
    lines += ["---- To prepare ----"] + brief_lines(rowd, row_nums) + ["--------------------", ""]
    if not opted_out:
        lines.append("The robot has marked this business 'Meeting booked', so no automatic email "
                     "(including the follow-up) will go to them.")
    subject = "Call booked: %s, %s" % (name, nice_time(when))
    return build_alert(to_addr, subject, lines)


def change_alert(to_addr, business, row_text, what, old_when, new_when, people):
    if what == STATE_CANCELLED:
        subject = "Call cancelled: %s" % business
        lines = ["%s cancelled their call (it was %s)." % (business, nice_time(old_when)), ""]
    else:
        subject = "Call moved: %s, now %s" % (business, nice_time(new_when))
        lines = ["%s moved their call." % business, "",
                 "Was: " + nice_time(old_when), "Now: " + nice_time(new_when), ""]
    lines += ["Booked by: " + booker_text(people), "Sheet row: " + (row_text or "(none)")]
    return build_alert(to_addr, subject, lines)


def unmatched_alert(to_addr, when, people, summary):
    lines = ["Someone booked a call through your booking page, but the robot could not tell which "
             "business in the Sheet it is.", "",
             "When: " + nice_time(when),
             "Booked by: " + booker_text(people),
             "Calendar title: " + (summary or "(none)"), "",
             "If this is one of your prospects: open the 'Meetings' tab in the Sheet, find this booking, "
             "and type the business's Sheet row number into its 'Sheet Row' cell. The next run links it "
             "and stops any automatic follow-up to them.",
             "If it is not an outreach booking, you can ignore this email."]
    return build_alert(to_addr, "Call booked, business not recognised: %s" % nice_time(when), lines)


# ---------------------------------------------------------------- main work
def run(ws, grid, cal, alert_to, mailer=None, dry_run=False, now=None):
    """The whole run. Returns (report_lines, problem_text). Separated from main() for tests."""
    now = now or datetime.now(timezone.utc)
    headers = [h.strip() for h in grid[0]]
    missing = [h for h in REQUIRED_HEADINGS if h not in headers]
    if missing:
        raise StopRun("These column headings are missing: " + ", ".join(missing))
    events = cal.events(now)          # read the calendar first: if it fails, nothing is changed
    if not dry_run:
        headers = ensure_columns(ws, headers)
    rows = [(i, row_dict(headers, r)) for i, r in enumerate(grid[1:], start=2)]
    by_num = dict(rows)
    max_row = len(grid)
    index = build_index(rows)
    tab, known = open_tab(ws, dry_run)
    stamp_now = lagos_text(now)

    counts = {"entries": len(events), "new": 0, "moved": 0, "cancelled": 0, "unmatched": 0,
              "linked": 0, "alerts": 0, "alert_failed": 0, "checks": 0}
    details, problems = [], []
    sheet_writes = {}      # row number -> {heading: value}
    tab_updates = []       # (line number, {heading: value})
    tab_new = []           # new lines
    alerts = []            # (EmailMessage, what it is about, row numbers)

    def plan(row_num, values):
        sheet_writes.setdefault(row_num, {}).update(values)
        by_num[row_num].update(values)

    def mark_rows(row_nums, when, booked_day):
        """Fill the meeting columns and set 'Meeting booked' (never over an opt-out).
        Returns True if any of the rows is opted out."""
        opted = False
        for n in row_nums:
            rowd = by_num[n]
            values = {H_MEETING_BOOKED: booked_day, H_MEETING_TIME: lagos_text(when)}
            if dm.is_opted_out(rowd):
                opted = True
            else:
                values[dm.H_STATUS] = ST_MEETING
            plan(n, values)
        return opted

    for ev in events:
        eid = (ev.get("id") or "").strip()
        if not eid:
            continue
        cancelled = (ev.get("status") or "").lower() == "cancelled"
        when = parse_time(ev.get("start"))
        seen = known.get(eid)

        # --- a booking we already know about
        if seen:
            line, d = seen
            state = d.get(M_STATE, "").strip()
            row_text = d.get(M_ROW, "").strip()
            business = d.get(M_BUSINESS, "").strip() or "A booking"
            people = outside_people(ev, cal.cal_id)
            old_when = parse_stamp(d.get(M_TIME, "").replace(" ", "T") + "+01:00") if d.get(M_TIME) else None
            row_nums = hand_rows(row_text, max_row)

            # Abolaji typed a row number for a booking the robot could not match
            if state == STATE_UNMATCHED and row_nums and not cancelled:
                first = by_num[row_nums[0]]
                booked_day = lagos_text(parse_stamp(ev.get("created")) or now)[:10]
                opted = mark_rows(row_nums, when, booked_day)
                tab_updates.append((line, {M_STATE: STATE_BOOKED, M_BUSINESS: first.get(dm.H_NAME, ""),
                                           M_MATCHED_BY: BY_HAND, M_UPDATED: stamp_now,
                                           M_NOTES: "Linked by hand to row %s" % row_text}))
                counts["linked"] += 1
                details.append("%s (row %s): booking linked by hand" % (first.get(dm.H_NAME, ""), row_text))
                if opted:
                    counts["checks"] += 1
                    alerts.append((new_booking_alert(alert_to, first, row_nums, when, people, BY_HAND, True),
                                   first.get(dm.H_NAME, ""), row_nums))
                continue

            if state == STATE_CANCELLED:
                continue
            if cancelled:
                tab_updates.append((line, {M_STATE: STATE_CANCELLED, M_UPDATED: stamp_now,
                                           M_NOTES: "Cancelled %s" % stamp_now[:10]}))
                if state == STATE_BOOKED:
                    counts["cancelled"] += 1
                    for n in row_nums:
                        plan(n, {H_MEETING_TIME: "Cancelled (was %s)" % d.get(M_TIME, "")})
                    alerts.append((change_alert(alert_to, business, row_text, STATE_CANCELLED, old_when,
                                                None, people), business, row_nums))
                    details.append("%s (row %s): call cancelled" % (business, row_text))
                continue
            if when and old_when and abs((when - old_when).total_seconds()) >= 60:
                tab_updates.append((line, {M_TIME: lagos_text(when), M_UPDATED: stamp_now,
                                           M_NOTES: "Moved from %s" % d.get(M_TIME, "")}))
                if state == STATE_BOOKED:
                    counts["moved"] += 1
                    for n in row_nums:
                        plan(n, {H_MEETING_TIME: lagos_text(when)})
                    alerts.append((change_alert(alert_to, business, row_text, "moved", old_when, when, people),
                                   business, row_nums))
                    details.append("%s (row %s): call moved to %s" % (business, row_text, lagos_text(when)))
            continue

        # --- a new calendar entry
        if cancelled:
            continue
        people = outside_people(ev, cal.cal_id)
        if not people:
            continue   # nobody from outside: a personal entry, never looked at further
        row_nums, how = match_event(ev, cal.cal_id, index)
        booked_day = lagos_text(parse_stamp(ev.get("created")) or now)[:10]
        if row_nums:
            first = by_num[row_nums[0]]
            name = first.get(dm.H_NAME, "")
            opted = mark_rows(row_nums, when, booked_day)
            counts["new"] += 1
            if how == BY_NAME or opted:
                counts["checks"] += 1
            tab_new.append({M_FIRST_SEEN: stamp_now, M_UPDATED: stamp_now, M_BUSINESS: name,
                            M_ROW: ", ".join(str(n) for n in row_nums), M_MATCHED_BY: how,
                            M_TIME: lagos_text(when), M_BOOKER: booker_text(people), M_STATE: STATE_BOOKED,
                            M_NOTES: "Opted out: status left alone" if opted else "", M_EVENT: eid})
            alerts.append((new_booking_alert(alert_to, first, row_nums, when, people, how, opted), name, row_nums))
            details.append("%s (row %s): call booked for %s (matched by %s)"
                           % (name, ", ".join(str(n) for n in row_nums), lagos_text(when), how))
        elif looks_like_booking(ev, cal.cal_id):
            counts["unmatched"] += 1
            tab_new.append({M_FIRST_SEEN: stamp_now, M_UPDATED: stamp_now, M_BUSINESS: "", M_ROW: "",
                            M_MATCHED_BY: "", M_TIME: lagos_text(when), M_BOOKER: booker_text(people),
                            M_STATE: STATE_UNMATCHED,
                            M_NOTES: "Type the Sheet row number into 'Sheet Row' if this is a prospect",
                            M_EVENT: eid})
            alerts.append((unmatched_alert(alert_to, when, people, ev.get("summary")), "an unmatched booking", []))
            # No names or addresses in the public log for bookings that are not outreach.

    # Write the Sheet first, then the tab, then email (a crash before the tab is written means the
    # booking is simply handled again next run; the row values are the same, so nothing doubles up).
    if not dry_run:
        for row_num, values in sheet_writes.items():
            ok = se.write_row(ws, headers, row_num, by_num[row_num].get(dm.H_NAME, ""), values)
            if not ok:
                problems.append("Row %d moved while the robot was running, so it was not updated. "
                                "It will be handled again next run." % row_num)
                tab_new = [d for d in tab_new if str(row_num) not in d.get(M_ROW, "").split(", ")]
                tab_updates = [(l, v) for l, v in tab_updates
                               if str(row_num) not in known_row_text(known, l).split(", ")]
                alerts = [a for a in alerts if row_num not in a[2]]
        save_tab(tab, tab_updates, tab_new)
        for msg, about, _ in alerts:
            try:
                result, detail = mailer.send(msg, alert_to)
            except se.StopRun as e:
                result, detail = "failed", str(e)
            if result == "sent":
                counts["alerts"] += 1
            else:
                counts["alert_failed"] += 1
                problems.append("The email about %s could not be sent (%s). It is on the Meetings tab."
                                % (about, clip(detail, 150)))

    title = ("## Track Meetings - PREVIEW (nothing changed, nothing sent)" if dry_run
             else "## Track Meetings - run report")
    report = [
        title, "",
        "- Script version: " + SCRIPT_VERSION,
        "- Calendar entries looked at (%d days back, %d ahead): %d" % (LOOK_BACK_DAYS, LOOK_AHEAD_DAYS,
                                                                       counts["entries"]),
        "- New calls booked by businesses we contacted: %d" % counts["new"],
        "- Calls moved: %d" % counts["moved"],
        "- Calls cancelled: %d" % counts["cancelled"],
        "- Bookings linked by hand on the Meetings tab: %d" % counts["linked"],
        "- Bookings not matched to any business (details emailed, not shown here): %d" % counts["unmatched"],
        "- Of those, needing your check: %d" % counts["checks"],
        ("- Emails that would be sent to %s: %d" % (alert_to, len(alerts)) if dry_run
         else "- Emails sent to %s: %d" % (alert_to, counts["alerts"])),
        "- Cost: $0 (no Claude)",
    ]
    if counts["alert_failed"]:
        report.append("- EMAILS THAT FAILED: %d" % counts["alert_failed"])
    for prob in problems:
        report.append("- PROBLEM: " + prob)
    if details:
        report += ["", "### Booking by booking", ""] + ["- " + d for d in details]
    return report, "; ".join(problems)


def known_row_text(known, line):
    for _, (n, d) in known.items():
        if n == line:
            return d.get(M_ROW, "")
    return ""


def main():
    log("Track Meetings, script version " + SCRIPT_VERSION)
    dry_run = is_dry_run()
    cal = Calendar(calendar_id())
    alert_to = alert_address()
    mailer = None if dry_run else se.Mailer(se.get_password())
    ws = se.open_sheet()
    grid = dm.with_retry(ws.get_all_values, "Reading the Sheet")
    if not grid:
        fail("The Sheet is empty.")
    try:
        report, problem = run(ws, grid, cal, alert_to, mailer=mailer, dry_run=dry_run)
    except StopRun as e:
        fail(str(e))
    finally:
        if mailer is not None:
            mailer.close()
    for line in report:
        log(line)
    se.write_summary(report)
    if problem:
        sys.exit(1)


if __name__ == "__main__":
    main()
