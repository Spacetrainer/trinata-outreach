#!/usr/bin/env python3
"""
Trinata Outreach - Phase 6: the sender.

What it does, in plain words:
  1. Opens the 'Trinata Outreach' Sheet.
  2. Finds rows whose Message Status is 'Approved' (a person typed it).
  3. Checks each one again just before sending: the email address is still
     written properly and its domain can receive mail, this address has never
     been emailed before, and the draft still has its sign-off and the
     'no thanks' opt-out line with no leftover blanks.
  4. Marks the row 'Sending', sends the email from hello@trinata.org through
     Zoho, then marks it 'Sent' with today's date. If anything goes wrong in
     between, the row is never sent twice: a row stuck on 'Sending' is left for
     a person to look at.
  5. Sends at most 10 emails a day (Lagos time), with a pause between each.

Test mode: if TEST_SEND_TO is set, it sends ONE approved draft to that address
instead of the business, and does not change the Sheet at all.
"""

import os
import random
import smtplib
import socket
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

import draft_messages as dm  # the Phase 5 file: shared email checks

SCRIPT_VERSION = "1 (23 Sep 2026)"

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
NEW_HEADINGS = [H_SEND_NOTES, H_MSG_ID]
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


def pre_send_check(rowd, already_emailed, domain_check=None):
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
def build_message(to_email, subject, body):
    msg = EmailMessage()
    msg["From"] = formataddr((SENDER_NAME, SENDER_EMAIL))
    msg["To"] = to_email
    msg["Subject"] = subject.strip()
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=SENDER_EMAIL.split("@")[1])
    msg["Reply-To"] = SENDER_EMAIL
    msg["List-Unsubscribe"] = UNSUBSCRIBE_HEADER
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
def run(ws, grid, password, max_emails, test_to="", connect=None,
        domain_check=None, sleep=time.sleep, now=None):
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

    already_emailed = set()
    sent_today = 0
    stuck = []
    for i, rowd in rows:
        status = (rowd.get(H_STATUS) or "").strip().lower()
        email = (rowd.get(H_EMAIL) or "").strip().lower()
        first_sent = (rowd.get(H_FIRST_SENT) or "").strip()
        if first_sent or status in (ST_SENT.lower(), ST_SENDING.lower()):
            if email:
                already_emailed.add(email)
        if first_sent == today:
            sent_today += 1
        if status == ST_SENDING.lower():
            stuck.append("%s (row %d)" % (rowd.get(H_NAME, ""), i))

    approved = [(i, r) for i, r in rows
                if (r.get(H_STATUS) or "").strip().lower() == ST_APPROVED.lower()]

    if test_to:
        allowance = 1
    else:
        allowance = max(0, min(max_emails, DAILY_LIMIT - sent_today))

    counts = {"sent": 0, "bad": 0, "review": 0, "later": 0}
    details = []
    stop_reason = ""
    mailer = Mailer(password, connect=connect)
    version = SCRIPT_VERSION.split()[0]

    try:
        for row_num, rowd in approved:
            if counts["sent"] >= allowance:
                break
            name = rowd.get(H_NAME, "")
            email = (rowd.get(H_EMAIL) or "").strip()
            stamp = "%s | v%s | " % (today, version)

            verdict, reason = pre_send_check(rowd, already_emailed, domain_check)
            if verdict != "ok":
                if verdict == "later":
                    counts["later"] += 1
                    details.append("%s: left for next run (%s)" % (name, reason))
                    continue
                new_status = ST_BAD_EMAIL if verdict == "bad_email" else ST_NEEDS_REVIEW
                key = "bad" if verdict == "bad_email" else "review"
                counts[key] += 1
                details.append("%s: %s (%s)" % (name, new_status, reason))
                if not test_to:
                    prefix = "REVIEW: " if verdict == "review" else ""
                    write_row(ws, headers, row_num, name, {
                        H_STATUS: new_status,
                        H_SEND_NOTES: clip(prefix + stamp + "Not sent: " + reason)})
                continue

            to_addr = test_to or email
            msg = build_message(to_addr, rowd.get(H_SUBJECT, ""),
                                rowd.get(H_BODY, ""))

            if counts["sent"] > 0:
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
        "- Approved and waiting to send: %d" % len(approved),
        "- Already sent today (Lagos time): %d of %d" % (sent_today, DAILY_LIMIT),
        "- Sent this run: %d%s" % (counts["sent"],
                                   " (test email only)" if test_to else ""),
        "- Marked Bad email: %d" % counts["bad"],
        "- Marked Needs review: %d" % counts["review"],
        "- Left for a later run: %d" % counts["later"],
    ]
    if not test_to and allowance == 0 and approved:
        report.append("- Nothing sent: today's limit of %d is already reached."
                      % DAILY_LIMIT)
    if stuck:
        report.append("- ATTENTION, stuck on 'Sending' (check Zoho's Sent folder, "
                      "then type Sent or Approved): " + ", ".join(stuck))
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

    ws = open_sheet()
    grid = dm.with_retry(ws.get_all_values, "Reading the Sheet")
    if not grid:
        fail("The Sheet is empty.")

    report, stopped = run(ws, grid, password, max_emails, test_to)
    for line in report:
        log(line)
    write_summary(report)
    if stopped:
        sys.exit(1)


if __name__ == "__main__":
    main()
