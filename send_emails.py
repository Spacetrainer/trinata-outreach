#!/usr/bin/env python3
"""
Trinata Outreach - Phase 6: the sender.

What it does, in plain words:
  1. Opens the 'Trinata Outreach' Sheet.
  2. Finds rows whose Message Status is 'Approved' (set by the drafting robot;
     since version 2 no person is involved anywhere).
  3. Checks each one again just before sending: the email address is still
     written properly and its domain can receive mail, this address has never
     been emailed before, and the draft still has its sign-off and the
     'no thanks' opt-out line with no leftover blanks.
  4. Marks the row 'Sending', sends the email from hello@trinata.org through
     Zoho, then marks it 'Sent' with today's date. If anything goes wrong in
     between, the row is never sent twice: a row still on 'Sending' from an
     earlier run is marked 'Sent (unconfirmed)' by itself and never retried.
  Rows that fail the last-minute checks are marked 'Not sent' (final, reason
  in Send Notes) or 'Bad email'.
  5. Sends at most 10 emails a day (Lagos time), with a pause between each.

Opt-outs (new in version 3): a business that asked not to be contacted
(Message Status 'Opted out', or the 'Opted Out' box ticked, on any row with the
same email, WhatsApp number or name) is never emailed. The row is marked
'Not sent' with the reason.

The one follow-up (new in version 3):
  - Goes only to rows whose Message Status is exactly 'Sent', 7 or more days
    after Date First Sent (and not more than 45), that have no Reply Date, no
    follow-up yet and no opt-out. So never to 'Not sent', 'Bad email',
    'Sent (unconfirmed)', 'Opted out', 'Replied' or any WhatsApp row.
  - Only when the reply reader (read_replies.py) checked the inbox successfully
    just before, in the same run (REPLIES_CHECKED=success). If it could not,
    no follow-up goes out that day; first emails still do.
  - A fixed, polite text (no AI), sent as a reply in the same email thread,
    with the booking link and the 'no thanks' line. Then the status becomes
    'Followed up'. There is never a second follow-up.
  - Follow-ups count towards the 10 emails a day, and go first.

Test mode: if TEST_SEND_TO is set, it sends ONE approved draft to that address
instead of the business, plus ONE example follow-up (for the first 'Sent' row,
whatever its date), and does not change the Sheet at all.
"""

import os
import random
import re
import smtplib
import socket
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

import draft_messages as dm  # the Phase 5 file: shared email checks

SCRIPT_VERSION = "3 (28 Sep 2026)"

# ---------------------------------------------------------------- settings
SENDER_EMAIL = "hello@trinata.org"
SENDER_NAME = "Abolaji, Trinata Ltd"
SMTP_SERVERS = [("smtppro.zoho.com", 465), ("smtp.zoho.com", 465)]
SMTP_TIMEOUT = 60

DAILY_LIMIT = 10                 # emails per Lagos day, across all runs
PAUSE_MIN_SECONDS = 45           # gap between two emails in one run
PAUSE_MAX_SECONDS = 90
SUBJECT_MAX_CHARS = 150          # generous: a person may have edited it
NOTE_CHAR_LIMIT = 600
LAGOS = timezone(timedelta(hours=1))

UNSUBSCRIBE_HEADER = "<mailto:%s?subject=unsubscribe>" % SENDER_EMAIL
OPT_OUT_PHRASE = "no thanks"

SHEET_TAB = "Sheet1"

# Status words
ST_APPROVED = "Approved"
ST_SENDING = "Sending"
ST_SENT = "Sent"
ST_BAD_EMAIL = "Bad email"
ST_NEEDS_REVIEW = "Needs review"
ST_NOT_SENT = "Not sent"                     # final: will not be emailed (reason in Send Notes)
ST_UNCONFIRMED = "Sent (unconfirmed)"        # the robot stopped mid-send; never sent again

# Column headings (found by heading, never by position)
H_NAME = "Company Name"
H_EMAIL = "Contact Email"
H_STATUS = "Message Status"
H_FIRST_SENT = "Date First Sent"
H_LAST_CONTACT = "Date Last Contact"
H_SUBJECT = "Draft Subject"
H_BODY = "Draft Body"
H_SEND_NOTES = "Send Notes"
H_MSG_ID = "Message ID"
H_FOLLOWUP_DATE = "Date Follow-up Sent"
H_FOLLOWUP_ID = "Follow-up Message ID"
NEW_HEADINGS = [H_SEND_NOTES, H_MSG_ID, H_FOLLOWUP_DATE, H_FOLLOWUP_ID]

ST_FOLLOWED_UP = dm.ST_FOLLOWED_UP
FOLLOWUP_AFTER_DAYS = 7          # the follow-up goes this many days after the first email
FOLLOWUP_MAX_AGE_DAYS = 45       # after this long, no follow-up at all (it would feel odd)
FOLLOWUP_CLAIM = "sending"       # written before a follow-up goes out, so it is never sent twice
FOLLOWUP_TEXT = (
    "I'm following up on my earlier email (below), in case it got buried. If it would help "
    "to talk it through, you can pick a time for a free 15-minute call here: %s\n\n"
    "If now isn't the right time, no problem at all." % dm.BOOKING_LINK
)
REQUIRED_HEADINGS = [H_NAME, H_EMAIL, H_STATUS, H_FIRST_SENT,
                     H_LAST_CONTACT, H_SUBJECT, H_BODY]


# ---------------------------------------------------------------- helpers
class StopRun(Exception):
    """Raised when the whole run must stop (e.g. Zoho refuses the sender)."""


def log(msg):
    print(msg, flush=True)


def write_summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError:
            pass


def fail(msg):
    log("STOPPED: " + msg)
    write_summary(["## Send Emails - stopped", "", msg])
    sys.exit(1)


def lagos_today(now=None):
    now = now or datetime.now(timezone.utc)
    return now.astimezone(LAGOS).strftime("%Y-%m-%d")


def clip(text, limit=NOTE_CHAR_LIMIT):
    text = text or ""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def get_password():
    raw = os.environ.get("ZOHO_APP_PASSWORD", "")
    pw = "".join(raw.split())  # Zoho sometimes shows it with spaces
    if not pw:
        fail("ZOHO_APP_PASSWORD is not set as a repo secret.")
    return pw


def get_max_emails():
    raw = (os.environ.get("MAX_EMAILS_PER_RUN") or "").strip()
    if not raw:
        return DAILY_LIMIT
    try:
        n = int(raw)
    except ValueError:
        fail("The email limit must be a whole number, got '%s'." % raw)
    if n < 1:
        fail("The email limit must be at least 1.")
    return min(n, DAILY_LIMIT)


def get_test_address():
    addr = (os.environ.get("TEST_SEND_TO") or "").strip()
    if not addr:
        return ""
    if dm.check_shape(addr):
        fail("The test address '%s' is not a valid email address." % addr)
    return addr


# ---------------------------------------------------------------- checks
def content_problems(subject, body):
    """Checks on the draft itself. Returns a list of problems."""
    problems = []
    subject = (subject or "").strip()
    body = (body or "").strip()
    if not subject:
        problems.append("subject is empty")
    elif "\n" in subject or len(subject) > SUBJECT_MAX_CHARS:
        problems.append("subject must be one line under %d characters"
                        % SUBJECT_MAX_CHARS)
    if not body:
        problems.append("body is empty")
        return problems
    if OPT_OUT_PHRASE not in body.lower():
        problems.append("the 'no thanks' opt-out line is missing")
    if "Abolaji" not in body or "trinata.org" not in body.lower():
        problems.append("the sign-off (Abolaji, trinata.org) is missing")
    for ch in "[]{}<>":
        if ch in body or ch in subject:
            problems.append("contains a leftover blank or bracket (%s)" % ch)
            break
    return problems


def pre_send_check(rowd, already_emailed, domain_check=None, blocked=None):
    """
    Decide whether this Approved row may be sent now.
    Returns (verdict, reason) where verdict is one of:
      'ok', 'bad_email', 'review', 'later'
    """
    domain_check = domain_check or dm.check_domain
    email = (rowd.get(H_EMAIL) or "").strip()

    if (rowd.get(H_FIRST_SENT) or "").strip():
        return "review", ("row already has a Date First Sent, so it was not "
                          "sent again")

    why = dm.opt_out_reason(rowd, blocked or (set(), set(), set()))
    if why:
        return "review", why

    reason = dm.check_shape(email)
    if reason:
        return "bad_email", reason

    if email.lower() in already_emailed:
        return "review", "this address has already been emailed from another row"

    problems = content_problems(rowd.get(H_SUBJECT), rowd.get(H_BODY))
    if problems:
        return "review", "draft not sent: " + "; ".join(problems)

    domain = email.split("@")[1].lower()
    verdict, detail = domain_check(domain)
    if verdict == "bad":
        return "bad_email", "%s (%s)" % (detail, domain)
    if verdict == "unknown":
        return "later", detail
    return "ok", detail


# ---------------------------------------------------------------- email
def build_message(to_email, subject, body, in_reply_to=""):
    msg = EmailMessage()
    msg["From"] = formataddr((SENDER_NAME, SENDER_EMAIL))
    msg["To"] = to_email
    msg["Subject"] = subject.strip()
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=SENDER_EMAIL.split("@")[1])
    msg["Reply-To"] = SENDER_EMAIL
    msg["List-Unsubscribe"] = UNSUBSCRIBE_HEADER
    if in_reply_to:
        ref = "<%s>" % in_reply_to.strip().strip("<>")
        msg["In-Reply-To"] = ref
        msg["References"] = ref
    msg.set_content(body.strip() + "\n")
    return msg


class Mailer:
    """Keeps one connection to Zoho open for the run."""

    def __init__(self, password, connect=None):
        self.password = "".join((password or "").split())
        self.connect = connect or self._real_connect
        self.server = None
        self.server_name = ""

    @staticmethod
    def _real_connect(host, port):
        ctx = ssl.create_default_context()
        return smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT, context=ctx)

    def open(self):
        last_err = ""
        for host, port in SMTP_SERVERS:
            try:
                server = self.connect(host, port)
            except (OSError, smtplib.SMTPException) as e:
                last_err = "could not reach %s (%s)" % (host, e.__class__.__name__)
                continue
            try:
                server.login(SENDER_EMAIL, self.password)
            except smtplib.SMTPAuthenticationError:
                self._quit(server)
                last_err = ("Zoho rejected the robot's password on %s. Check that "
                            "ZOHO_APP_PASSWORD holds the app password named "
                            "'GitHub outreach robot'." % host)
                continue
            except (OSError, smtplib.SMTPException) as e:
                self._quit(server)
                last_err = "login problem on %s (%s)" % (host, e.__class__.__name__)
                continue
            self.server = server
            self.server_name = host
            return
        raise StopRun(last_err or "could not connect to Zoho")

    @staticmethod
    def _quit(server):
        try:
            server.quit()
        except Exception:
            pass

    def close(self):
        if self.server is not None:
            self._quit(self.server)
            self.server = None

    def send(self, msg, to_email):
        """
        Returns ('sent', detail) | ('refused', detail) | ('later', detail).
        Raises StopRun when Zoho refuses the sender itself.
        """
        for attempt in range(2):
            if self.server is None:
                self.open()
            try:
                self.server.send_message(msg, from_addr=SENDER_EMAIL,
                                         to_addrs=[to_email])
                return "sent", "accepted by Zoho (%s)" % self.server_name
            except smtplib.SMTPRecipientsRefused as e:
                code, text = list(e.recipients.values())[0]
                text = text.decode("utf-8", "replace") if isinstance(text, bytes) else str(text)
                if code >= 500:
                    return "refused", "address refused (%s %s)" % (code, clip(text, 150))
                return "later", "temporary refusal (%s %s)" % (code, clip(text, 150))
            except smtplib.SMTPSenderRefused as e:
                raise StopRun("Zoho refused to send from %s (%s %s). No more "
                              "emails were sent this run." % (
                                  SENDER_EMAIL, e.smtp_code,
                                  clip(str(e.smtp_error), 150)))
            except smtplib.SMTPDataError as e:
                text = e.smtp_error.decode("utf-8", "replace") \
                    if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
                if e.smtp_code >= 500:
                    raise StopRun("Zoho stopped the email (%s %s). No more emails "
                                  "were sent this run." % (e.smtp_code, clip(text, 150)))
                return "later", "temporary problem (%s %s)" % (e.smtp_code, clip(text, 150))
            except (smtplib.SMTPServerDisconnected, socket.timeout, OSError):
                self.close()
                if attempt == 0:
                    time.sleep(10)
                    continue
                return "later", "connection to Zoho dropped"
        return "later", "connection to Zoho dropped"


# ---------------------------------------------------------------- the follow-up
def days_since(date_text, today):
    """Whole days from a 'YYYY-MM-DD' date to today (also 'YYYY-MM-DD'). None if unreadable."""
    try:
        a = datetime.strptime((date_text or "").strip()[:10], "%Y-%m-%d")
        b = datetime.strptime(today, "%Y-%m-%d")
    except ValueError:
        return None
    return (b - a).days


def followup_block(rowd, today, blocked):
    """'' if this row should get its follow-up now, else the reason it should not."""
    status = (rowd.get(H_STATUS) or "").strip().lower()
    if status != ST_SENT.lower():
        return "status is '%s', not 'Sent'" % (rowd.get(H_STATUS) or "").strip()
    if (rowd.get(H_FOLLOWUP_DATE) or "").strip():
        return "already followed up"
    if (rowd.get(dm.H_REPLY_DATE) or "").strip():
        return "they replied"
    why = dm.opt_out_reason(rowd, blocked)
    if why:
        return why
    age = days_since(rowd.get(H_FIRST_SENT), today)
    if age is None:
        return "Date First Sent is missing or unreadable"
    if age < FOLLOWUP_AFTER_DAYS:
        return "not due yet"
    if age > FOLLOWUP_MAX_AGE_DAYS:
        return "first email is over %d days old" % FOLLOWUP_MAX_AGE_DAYS
    if dm.check_shape((rowd.get(H_EMAIL) or "").strip()):
        return "email address is not usable"
    return ""


def build_followup(rowd):
    """(subject, body, new_part) of the one follow-up. Fixed wording, nothing invented.
    new_part is the body without the quoted first email (whose '>' marks are normal)."""
    contact = (rowd.get(dm.H_CONTACT_NAME) or "").strip()
    first = contact.split()[0] if contact else ""
    if not re.match(r"^[A-Za-z][A-Za-z'-]*$", first):
        first = ""
    greeting = "Hi %s," % first if first else "Hi,"
    subject = " ".join((rowd.get(H_SUBJECT) or "").split())
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    subject = subject[:SUBJECT_MAX_CHARS]
    try:
        when = datetime.strptime((rowd.get(H_FIRST_SENT) or "")[:10], "%Y-%m-%d").strftime("%d %b %Y").lstrip("0")
    except ValueError:
        when = "earlier"
    original = (rowd.get(H_BODY) or "").strip()
    quoted = "\n".join("> " + line if line.strip() else ">" for line in original.splitlines())
    new_part = "%s\n\n%s\n\n%s" % (greeting, FOLLOWUP_TEXT, dm.SIGN_OFF)
    body = "%s\n\nOn %s, Abolaji from Trinata Ltd wrote:\n%s" % (new_part, when, quoted)
    return subject, body, new_part


def fresh_row(ws, headers, row_num):
    """The row as it is in the Sheet right now (another robot may have changed it)."""
    values = dm.with_retry(lambda: ws.row_values(row_num), "Re-reading row %d" % row_num)
    values = list(values) + [""] * (len(headers) - len(values))
    return dict(zip(headers, values))


# ---------------------------------------------------------------- Sheet
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


def write_row(ws, headers, row_num, expected_name, values):
    """Write {heading: value} to one row, after checking the row has not moved."""
    import gspread.utils as gu
    name_col = headers.index(H_NAME) + 1
    current = dm.with_retry(
        lambda: ws.cell(row_num, name_col).value, "Re-checking row %d" % row_num)
    if (current or "").strip() != (expected_name or "").strip():
        return False

    def build():  # rebuilt for every retry
        return [{"range": gu.rowcol_to_a1(row_num, headers.index(h) + 1),
                 "values": [[v]]} for h, v in values.items() if h in headers]

    dm.with_retry(lambda: ws.batch_update(build(), value_input_option="RAW"),
                  "Writing row %d" % row_num)
    time.sleep(1.5)
    return True


def open_sheet():
    # Reuse the Phase 5 opener, but keep this robot's own stop message.
    dm.fail = fail
    return dm.open_sheet()




# ---------------------------------------------------------------- main work
def send_followups(ws, headers, rows, mailer, allowance, today, blocked, replies_checked,
                   test_to, sleep, counts, details):
    """The one follow-up per business. Returns how many went out (or would, in test mode)."""
    version = SCRIPT_VERSION.split()[0]
    stamp = "%s | v%s | " % (today, version)
    if test_to:
        # Test: one example follow-up, for the first 'Sent' row, to the test address. Sheet untouched.
        for row_num, rowd in rows:
            if (rowd.get(H_STATUS) or "").strip().lower() != ST_SENT.lower():
                continue
            subject, body, _ = build_followup(rowd)
            msg = build_message(test_to, subject, body, in_reply_to=rowd.get(H_MSG_ID, ""))
            result, detail = mailer.send(msg, test_to)
            if result == "sent":
                counts["followed_up"] += 1
                details.append("TEST: example follow-up for %s sent to %s (%s)"
                               % (rowd.get(H_NAME, ""), test_to, detail))
            else:
                details.append("TEST: example follow-up NOT sent (%s)" % detail)
            return 1
        details.append("TEST: no 'Sent' row to make an example follow-up from")
        return 0

    due = [(i, r) for i, r in rows if not followup_block(r, today, blocked)]
    counts["followup_due"] = len(due)
    if not due:
        return 0
    if replies_checked != "success":
        counts["followup_held"] = len(due)
        details.append("Follow-ups held back today: the inbox could not be checked for replies first")
        return 0

    sent = 0
    for row_num, rowd in due:
        if sent >= allowance:
            counts["followup_later"] += 1
            continue
        name = rowd.get(H_NAME, "")
        # Look again, right now: a reply or an opt-out may have arrived since the Sheet was read.
        now_rowd = fresh_row(ws, headers, row_num)
        if (now_rowd.get(H_NAME) or "").strip() != (name or "").strip():
            counts["followup_later"] += 1
            details.append("%s: row moved while running, no follow-up" % name)
            continue
        why = followup_block(now_rowd, today, blocked)
        if why:
            details.append("%s: no follow-up (%s)" % (name, why))
            continue
        email = (now_rowd.get(H_EMAIL) or "").strip()
        subject, body, new_part = build_followup(now_rowd)
        problems = content_problems(subject, new_part)
        if problems:
            write_row(ws, headers, row_num, name, {
                H_FOLLOWUP_DATE: "not sent",
                H_SEND_NOTES: clip(stamp + "Follow-up not sent: " + "; ".join(problems) + " || "
                                   + (now_rowd.get(H_SEND_NOTES) or ""))})
            details.append("%s: follow-up not sent (%s)" % (name, "; ".join(problems)))
            continue
        msg = build_message(email, subject, body, in_reply_to=now_rowd.get(H_MSG_ID, ""))
        if counts["sent"] + sent > 0:
            sleep(random.randint(PAUSE_MIN_SECONDS, PAUSE_MAX_SECONDS))
        # Claim it first. If the robot stops mid-send, it is never sent a second time.
        if not write_row(ws, headers, row_num, name, {H_FOLLOWUP_DATE: "%s %s" % (FOLLOWUP_CLAIM, today)}):
            counts["followup_later"] += 1
            continue
        try:
            result, detail = mailer.send(msg, email)
        except StopRun:
            write_row(ws, headers, row_num, name, {H_FOLLOWUP_DATE: ""})
            raise
        if result == "sent":
            write_row(ws, headers, row_num, name, {
                H_STATUS: ST_FOLLOWED_UP,
                H_FOLLOWUP_DATE: today,
                H_LAST_CONTACT: today,
                H_FOLLOWUP_ID: msg["Message-ID"],
                H_SEND_NOTES: clip(stamp + "Follow-up sent to %s, %s || " % (email, detail)
                                   + (now_rowd.get(H_SEND_NOTES) or ""))})
            sent += 1
            counts["followed_up"] += 1
            details.append("%s: follow-up sent to %s" % (name, email))
        elif result == "refused":
            write_row(ws, headers, row_num, name, {
                H_STATUS: ST_BAD_EMAIL,
                H_FOLLOWUP_DATE: "not sent",
                H_SEND_NOTES: clip(stamp + "Follow-up refused: " + detail + " || "
                                   + (now_rowd.get(H_SEND_NOTES) or ""))})
            counts["bad"] += 1
            details.append("%s: Bad email on follow-up (%s)" % (name, detail))
        else:
            write_row(ws, headers, row_num, name, {H_FOLLOWUP_DATE: ""})
            counts["followup_later"] += 1
            details.append("%s: follow-up left for next run (%s)" % (name, detail))
    return sent


def run(ws, grid, password, max_emails, test_to="", connect=None,
        domain_check=None, sleep=time.sleep, now=None, replies_checked=""):
    """The whole run. Returns the report lines. Separated from main() for tests."""
    headers = [h.strip() for h in grid[0]]
    missing = [h for h in REQUIRED_HEADINGS if h not in headers]
    if missing:
        fail("These column headings are missing: " + ", ".join(missing))
    if not test_to:
        headers = ensure_columns(ws, headers)

    today = lagos_today(now)
    rows = []
    for i, row in enumerate(grid[1:], start=2):
        row = row + [""] * (len(headers) - len(row))
        rows.append((i, dict(zip(headers, row))))
    blocked = dm.opt_out_keys([r for _, r in rows])

    already_emailed = set()
    sent_today = 0
    stuck = []
    stuck_followups = []
    for i, rowd in rows:
        status = (rowd.get(H_STATUS) or "").strip().lower()
        email = (rowd.get(H_EMAIL) or "").strip().lower()
        first_sent = (rowd.get(H_FIRST_SENT) or "").strip()
        followup = (rowd.get(H_FOLLOWUP_DATE) or "").strip()
        if first_sent or status in (ST_SENT.lower(), ST_SENDING.lower(), ST_UNCONFIRMED.lower(),
                                    ST_FOLLOWED_UP.lower()):
            if email:
                already_emailed.add(email)
        if first_sent == today:
            sent_today += 1
        if followup == today or followup == "%s %s" % (FOLLOWUP_CLAIM, today):
            sent_today += 1
        if status == ST_SENDING.lower():
            stuck.append("%s (row %d)" % (rowd.get(H_NAME, ""), i))
        if followup.lower().startswith(FOLLOWUP_CLAIM):
            stuck_followups.append((i, rowd))

    # Rows still on 'Sending' were left by an earlier run that stopped part-way. The email may or may
    # not have gone out, so they are never sent again: marked 'Sent (unconfirmed)' with no one involved.
    if stuck and not test_to:
        for i, rowd in rows:
            if (rowd.get(H_STATUS) or "").strip().lower() == ST_SENDING.lower():
                write_row(ws, headers, i, rowd.get(H_NAME, ""), {
                    H_STATUS: ST_UNCONFIRMED,
                    H_SEND_NOTES: clip("%s | v%s | An earlier run stopped while sending, so it is "
                                       "not known whether this email went out. It will not be "
                                       "sent again." % (today, SCRIPT_VERSION.split()[0]))})
    # The same for a follow-up that was being sent when an earlier run stopped: never sent again.
    if stuck_followups and not test_to:
        for i, rowd in stuck_followups:
            write_row(ws, headers, i, rowd.get(H_NAME, ""), {
                H_STATUS: ST_FOLLOWED_UP,
                H_FOLLOWUP_DATE: "unconfirmed (%s)" % rowd.get(H_FOLLOWUP_DATE, "").split()[-1],
                H_SEND_NOTES: clip("%s | v%s | An earlier run stopped while sending the follow-up, so "
                                   "it is not known whether it went out. It will not be sent again. || "
                                   % (today, SCRIPT_VERSION.split()[0]) + (rowd.get(H_SEND_NOTES) or ""))})
            rowd[H_STATUS] = ST_FOLLOWED_UP

    approved = [(i, r) for i, r in rows
                if (r.get(H_STATUS) or "").strip().lower() == ST_APPROVED.lower()]

    if test_to:
        allowance = 1
    else:
        allowance = max(0, min(max_emails, DAILY_LIMIT - sent_today))

    counts = {"sent": 0, "bad": 0, "review": 0, "later": 0, "followed_up": 0, "followup_due": 0,
              "followup_held": 0, "followup_later": 0}
    details = []
    stop_reason = ""
    mailer = Mailer(password, connect=connect)
    version = SCRIPT_VERSION.split()[0]

    try:
        # Follow-ups first: they are few and time-sensitive.
        followups = send_followups(ws, headers, rows, mailer, allowance, today, blocked,
                                   replies_checked, test_to, sleep, counts, details)
        first_allowance = allowance if test_to else max(0, allowance - followups)

        for row_num, rowd in approved:
            if counts["sent"] >= first_allowance:
                break
            name = rowd.get(H_NAME, "")
            email = (rowd.get(H_EMAIL) or "").strip()
            stamp = "%s | v%s | " % (today, version)

            verdict, reason = pre_send_check(rowd, already_emailed, domain_check, blocked)
            if verdict != "ok":
                if verdict == "later":
                    counts["later"] += 1
                    details.append("%s: left for next run (%s)" % (name, reason))
                    continue
                new_status = ST_BAD_EMAIL if verdict == "bad_email" else ST_NOT_SENT
                key = "bad" if verdict == "bad_email" else "review"
                counts[key] += 1
                details.append("%s: %s (%s)" % (name, new_status, reason))
                if not test_to:
                    write_row(ws, headers, row_num, name, {
                        H_STATUS: new_status,
                        H_SEND_NOTES: clip(stamp + "Not sent: " + reason)})
                continue

            to_addr = test_to or email
            msg = build_message(to_addr, rowd.get(H_SUBJECT, ""),
                                rowd.get(H_BODY, ""))

            if counts["sent"] > 0 or followups > 0:
                sleep(random.randint(PAUSE_MIN_SECONDS, PAUSE_MAX_SECONDS))

            if not test_to:
                ok = write_row(ws, headers, row_num, name, {H_STATUS: ST_SENDING})
                if not ok:
                    counts["later"] += 1
                    details.append("%s: row moved while running, left alone" % name)
                    continue

            try:
                result, detail = mailer.send(msg, to_addr)
            except StopRun:
                if not test_to:
                    write_row(ws, headers, row_num, name, {H_STATUS: ST_APPROVED})
                raise

            if test_to:
                if result == "sent":
                    counts["sent"] += 1
                    details.append("TEST: %s's email sent to %s instead of %s (%s)"
                                   % (name, test_to, email, detail))
                else:
                    counts["later"] += 1
                    details.append("TEST: %s's email NOT sent (%s)" % (name, detail))
                break

            if result == "sent":
                write_row(ws, headers, row_num, name, {
                    H_STATUS: ST_SENT,
                    H_FIRST_SENT: today,
                    H_LAST_CONTACT: today,
                    H_MSG_ID: msg["Message-ID"],
                    H_SEND_NOTES: clip(stamp + "Sent to %s, %s" % (email, detail)),
                })
                counts["sent"] += 1
                already_emailed.add(email.lower())
                details.append("%s: Sent to %s" % (name, email))
            elif result == "refused":
                write_row(ws, headers, row_num, name, {
                    H_STATUS: ST_BAD_EMAIL,
                    H_SEND_NOTES: clip(stamp + "Not sent: " + detail)})
                counts["bad"] += 1
                details.append("%s: Bad email (%s)" % (name, detail))
            else:
                write_row(ws, headers, row_num, name, {
                    H_STATUS: ST_APPROVED,
                    H_SEND_NOTES: clip(stamp + "Will retry next run: " + detail)})
                counts["later"] += 1
                details.append("%s: left for next run (%s)" % (name, detail))
    except StopRun as e:
        stop_reason = str(e)
    finally:
        mailer.close()

    title = "## Send Emails - TEST run report" if test_to else \
        "## Send Emails - run report"
    report = [
        title,
        "",
        "- Script version: " + SCRIPT_VERSION,
        "- Inbox checked for replies first: %s" % ("yes" if replies_checked == "success" else "NO"),
        "- Follow-ups due: %d" % counts["followup_due"],
        "- Follow-ups sent this run: %d%s" % (counts["followed_up"],
                                              " (test example only)" if test_to else ""),
        "- Approved and waiting to send: %d" % len(approved),
        "- Already sent today (Lagos time, first emails and follow-ups): %d of %d"
        % (sent_today, DAILY_LIMIT),
        "- First emails sent this run: %d%s" % (counts["sent"],
                                                " (test email only)" if test_to else ""),
        "- Marked Bad email: %d" % counts["bad"],
        "- Marked Not sent (final, reason in Send Notes): %d" % counts["review"],
        "- Left for a later run: %d" % (counts["later"] + counts["followup_later"]),
    ]
    if counts["followup_held"]:
        report.append("- Follow-ups held back (the inbox check did not succeed, so a reply might "
                      "have been missed): %d. They go on the next run that can check." % counts["followup_held"])
    if not test_to and allowance == 0 and (approved or counts["followup_due"]):
        report.append("- Nothing sent: today's limit of %d is already reached."
                      % DAILY_LIMIT)
    if stuck:
        report.append("- Found still on 'Sending' from an earlier run, marked 'Sent (unconfirmed)' "
                      "and never sent again: " + ", ".join(stuck))
    if stuck_followups and not test_to:
        report.append("- Follow-ups interrupted by an earlier run, never sent again: %d" % len(stuck_followups))
    if stop_reason:
        report.append("- STOPPED EARLY: " + stop_reason)
    if details:
        report += ["", "### Row by row", ""] + ["- " + d for d in details]
    return report, bool(stop_reason)


def main():
    log("Send Emails, script version " + SCRIPT_VERSION)
    password = get_password()
    max_emails = get_max_emails()
    test_to = get_test_address()
    replies_checked = (os.environ.get("REPLIES_CHECKED") or "").strip().lower()

    ws = open_sheet()
    grid = dm.with_retry(ws.get_all_values, "Reading the Sheet")
    if not grid:
        fail("The Sheet is empty.")

    report, stopped = run(ws, grid, password, max_emails, test_to, replies_checked=replies_checked)
    for line in report:
        log(line)
    write_summary(report)
    if stopped:
        sys.exit(1)


if __name__ == "__main__":
    main()
