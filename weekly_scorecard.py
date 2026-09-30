#!/usr/bin/env python3
"""
Trinata Outreach - Phase 8: the weekly scorecard ("learn what works").

What it does, in plain words, every Monday morning:
  1. Reads the whole Sheet (it changes nothing on the main tab).
  2. Counts, for each kind of business, each channel (email or WhatsApp) and
     each kind of offer: how many were found, judged a fit, had a contact,
     were contacted, replied, showed interest, booked a call, said no thanks,
     or had a bad address.
  3. Counts what happened in the last 7 days, and what is waiting (for
     example, WhatsApp messages still waiting for Abolaji to tap send).
  4. Writes plain-English pointers, but only where there is enough to go on.
     With fewer than 10 businesses contacted in a group, it says "too early to
     tell" rather than guess.
  5. Rewrites the 'Scorecard' tab, adds one line to the 'Scorecard History'
     tab (so the weeks can be compared), and emails Abolaji the scorecard.

It costs nothing: no Claude, no paid service.

Privacy: the run log on GitHub is public, so it shows only totals. Business
names appear only in the email and in the (private) Sheet.

Preview mode (DRY_RUN=yes): works everything out and shows the totals, but
writes nothing and sends nothing.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

import draft_messages as dm
import send_emails as se

SCRIPT_VERSION = "2 (30 Sep 2026)"

# ---------------------------------------------------------------- settings
ALERT_TO_DEFAULT = "abolaji@trinata.org"
FROM_NAME = "Trinata Outreach Robot"
OUR_DOMAIN = "trinata.org"
LAGOS = timezone(timedelta(hours=1))
WEEK_DAYS = 7
MIN_CONTACTED_TO_JUDGE = 10     # a group needs this many contacted before the robot compares it
MIN_TOTAL_TO_JUDGE = 20         # below this many contacted overall, "too early to tell"
NO_CONTACT_WORRY = 0.40         # if more than 40% of prospects have no contact, say so
OPT_OUT_WORRY = 0.20            # if more than 20% of a group say no thanks, say so
LIST_LIMIT = 15                 # most names listed in one "waiting" list

TAB = "Scorecard"
HISTORY_TAB = "Scorecard History"
HISTORY_HEADINGS = ["Week Ending", "Businesses", "Fit (prospects)", "Contact Found", "Contacted",
                    "Replied", "Interested", "Meetings Booked", "Opted Out", "Bad Email",
                    "No Contact Found", "Contacted This Week", "Replies This Week", "Meetings This Week"]

# Headings used (all optional except the three below; missing ones just count as empty)
H_DATE_SOURCED = "Date Sourced"
H_MEETING_BOOKED = "Meeting Booked"
H_MEETING_TIME = "Meeting Time"
REQUIRED_HEADINGS = [dm.H_NAME, dm.H_STATUS, dm.H_SUBSECTOR]

NOT_CHECKED = {"sourced", ""}
NOT_PROSPECT_ICP = {"", "none", "unclear"}
EMAIL_SENT_STATUSES = {s.lower() for s in (se.ST_SENT, se.ST_UNCONFIRMED, se.ST_SENDING, dm.ST_FOLLOWED_UP)}
INTEREST_MARK = "interested or a question"   # how the reply reader labels an interested reply

STEPS = [("businesses", "Businesses found"), ("prospects", "Judged a fit (prospects)"),
         ("contact", "Contact found"), ("contacted", "Contacted"), ("replied", "Replied"),
         ("interested", "Interested"), ("meeting", "Booked a call"), ("opted_out", "Said no thanks"),
         ("bad_email", "Bad email address")]


# ---------------------------------------------------------------- small helpers
class StopRun(Exception):
    pass


def log(msg):
    print(msg, flush=True)


def fail(msg):
    log("STOPPED: " + msg)
    se.write_summary(["## Weekly Scorecard - stopped", "", msg])
    sys.exit(1)


def is_dry_run():
    return (os.environ.get("DRY_RUN") or "").strip().lower() in ("yes", "true", "1", "preview")


def alert_address():
    addr = (os.environ.get("ALERT_EMAIL") or "").strip() or ALERT_TO_DEFAULT
    if dm.check_shape(addr):
        fail("ALERT_EMAIL '%s' is not a valid email address." % addr)
    return addr


def row_dict(headers, row):
    row = list(row) + [""] * (len(headers) - len(row))
    return dict(zip(headers, row))


def day_of(text):
    """'YYYY-MM-DD' from the start of a cell, or '' if it doesn't start with a date."""
    text = (text or "").strip()[:10]
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return text
    except ValueError:
        return ""


def pct(part, whole):
    return "%d%%" % round(100.0 * part / whole) if whole else "-"


def get(rowd, heading):
    return (rowd.get(heading) or "").strip()


# ---------------------------------------------------------------- one row
def facts(rowd):
    """Everything the scorecard needs to know about one business, as simple yes/no flags."""
    status = get(rowd, dm.H_STATUS).lower()
    icp = get(rowd, dm.H_ICP).lower()
    has_email = bool(get(rowd, dm.H_EMAIL))
    has_wa = bool(dm.normalize_ng_mobile(get(rowd, dm.H_WA_NUMBER)))
    wa_ticked = get(rowd, dm.H_WA_SENT).lower() in dm.TICKED
    email_contacted = bool(get(rowd, se.H_MSG_ID)) or status in EMAIL_SENT_STATUSES or \
        (bool(day_of(get(rowd, dm.H_FIRST_SENT))) and has_email and not wa_ticked
         and status != dm.ST_WA_SENT.lower())
    wa_contacted = wa_ticked or status == dm.ST_WA_SENT.lower()
    contacted = email_contacted or wa_contacted
    # Only a real date counts. Some cells hold an empty tick box ("FALSE") that spread sideways
    # from the 'WhatsApp Sent' column, so "is anything in the cell?" is not good enough.
    meeting_time = get(rowd, H_MEETING_TIME)
    booked = get(rowd, H_MEETING_BOOKED)
    meeting = (bool(day_of(booked)) or booked.upper() == "TRUE") and \
        not meeting_time.lower().startswith("cancelled")
    replied = bool(day_of(get(rowd, dm.H_REPLY_DATE))) or status in (dm.ST_REPLIED.lower(),
                                                                     dm.ST_REPLIED_INTERESTED.lower())
    interested = status == dm.ST_REPLIED_INTERESTED.lower() or INTEREST_MARK in get(rowd, dm.H_REPLY_NOTES).lower()
    prospect = status not in NOT_CHECKED and status != "not a fit" and icp not in NOT_PROSPECT_ICP
    if contacted or meeting:
        prospect = True     # anyone we contacted was a prospect, whatever the ICP cell says now
    return {
        "businesses": True,
        "checked": status not in NOT_CHECKED,
        "prospects": prospect,
        "contact": prospect and (has_email or has_wa or contacted),
        "no_contact": status == "no contact found",
        "contacted": contacted,
        "email": email_contacted,
        "whatsapp": wa_contacted and not email_contacted,
        "followed_up": day_of(get(rowd, se.H_FOLLOWUP_DATE)) != "",
        "replied": replied or interested,
        "interested": interested,
        "meeting": meeting,
        "opted_out": dm.is_opted_out(rowd),
        "bad_email": status == dm.ST_BAD_EMAIL.lower(),
        "wa_waiting": status == dm.ST_WA_READY.lower() and not wa_ticked,
        "waiting_fit": status == "sourced",
        "waiting_contact": status == "fit-checked",
        "waiting_send": status in (dm.ST_CONTACT_FOUND.lower(), dm.ST_REDO.lower(), dm.ST_APPROVED.lower(),
                                   dm.ST_WA_ONLY.lower()),
        "not_sent": status == dm.ST_NOT_SENT.lower(),
    }


def channel_of(f):
    if f["email"]:
        return "Email"
    if f["whatsapp"]:
        return "WhatsApp"
    return ""


def offer_of(rowd):
    icp = get(rowd, dm.H_ICP)
    return icp if icp and icp.lower() not in NOT_PROSPECT_ICP else "Not yet judged / no fit"


# ---------------------------------------------------------------- the counting
def empty_counts():
    return {k: 0 for k, _ in STEPS}


def add(counts, f):
    for k, _ in STEPS:
        if f.get(k):
            counts[k] += 1


def build(grid, now=None):
    """Work out the whole scorecard. Returns a dict (separated from main() for tests)."""
    now = (now or datetime.now(timezone.utc)).astimezone(LAGOS)
    headers = [h.strip() for h in grid[0]]
    missing = [h for h in REQUIRED_HEADINGS if h not in headers]
    if missing:
        raise StopRun("These column headings are missing: " + ", ".join(missing))
    today = now.strftime("%Y-%m-%d")
    week_start = (now - timedelta(days=WEEK_DAYS - 1)).strftime("%Y-%m-%d")

    def this_week(day):
        return bool(day) and week_start <= day <= today

    total = empty_counts()
    by_sub, by_channel, by_offer = {}, {}, {}
    week = {"sourced": 0, "contacted": 0, "followed_up": 0, "replied": 0, "meetings": 0}
    extra = {"no_contact": 0, "checked": 0, "followed_up": 0, "not_sent": 0}
    status_counts = {}
    waiting = {"wa": [], "fit": 0, "contact": 0, "send": 0}
    upcoming, week_replies = [], []

    for n, raw in enumerate(grid[1:], start=2):
        rowd = row_dict(headers, raw)
        name = get(rowd, dm.H_NAME)
        if not name:
            continue
        f = facts(rowd)
        add(total, f)
        add(by_sub.setdefault(get(rowd, dm.H_SUBSECTOR) or "(not set)", empty_counts()), f)
        add(by_offer.setdefault(offer_of(rowd), empty_counts()), f)
        ch = channel_of(f)
        if ch:
            add(by_channel.setdefault(ch, empty_counts()), f)
        for k in extra:
            if f[k]:
                extra[k] += 1
        status = get(rowd, dm.H_STATUS) or "(blank)"
        status_counts[status] = status_counts.get(status, 0) + 1
        if f["wa_waiting"]:
            waiting["wa"].append("%s (row %d)" % (name, n))
        waiting["fit"] += f["waiting_fit"]
        waiting["contact"] += f["waiting_contact"]
        waiting["send"] += f["waiting_send"]

        if this_week(day_of(get(rowd, H_DATE_SOURCED))):
            week["sourced"] += 1
        if f["contacted"] and this_week(day_of(get(rowd, dm.H_FIRST_SENT))):
            week["contacted"] += 1
        if this_week(day_of(get(rowd, se.H_FOLLOWUP_DATE))):
            week["followed_up"] += 1
        reply_day = day_of(get(rowd, dm.H_REPLY_DATE))
        if this_week(reply_day):
            week["replied"] += 1
            week_replies.append("%s: %s" % (name, get(rowd, dm.H_REPLY_NOTES).split("| ")[-1][:160]
                                            or "(no note)"))
        if f["meeting"] and this_week(day_of(get(rowd, H_MEETING_BOOKED))):   # a date only
            week["meetings"] += 1
        call_day = day_of(get(rowd, H_MEETING_TIME))
        if f["meeting"] and call_day and call_day >= today:
            upcoming.append((get(rowd, H_MEETING_TIME), "%s, %s (row %d)" % (get(rowd, H_MEETING_TIME), name, n)))

    upcoming.sort()
    return {
        "now": now, "today": today, "week_start": week_start,
        "total": total, "by_sub": by_sub, "by_channel": by_channel, "by_offer": by_offer,
        "week": week, "extra": extra, "status_counts": status_counts, "waiting": waiting,
        "upcoming": [u[1] for u in upcoming], "week_replies": week_replies,
        "pointers": pointers(total, by_sub, by_channel, by_offer, extra, waiting),
    }


# ---------------------------------------------------------------- plain-English pointers
def rate_line(name, c):
    return "%s: %d contacted, %s replied, %s booked a call, %s said no thanks" % (
        name, c["contacted"], pct(c["replied"], c["contacted"]), pct(c["meeting"], c["contacted"]),
        pct(c["opted_out"], c["contacted"]))


def pointers(total, by_sub, by_channel, by_offer, extra, waiting):
    out = []
    contacted = total["contacted"]
    if contacted < MIN_TOTAL_TO_JUDGE:
        out.append("Too early to tell which kinds of business respond best: %d contacted so far. The "
                   "robot starts comparing groups once one has at least %d contacted, and it is worth "
                   "reading much into it only past about %d in total." % (contacted, MIN_CONTACTED_TO_JUDGE,
                                                                       MIN_TOTAL_TO_JUDGE))
    for label, groups in (("kind of business", by_sub), ("channel", by_channel), ("offer", by_offer)):
        judged = [(k, c) for k, c in groups.items() if c["contacted"] >= MIN_CONTACTED_TO_JUDGE]
        if len(judged) >= 2:
            best = max(judged, key=lambda kc: (kc[1]["replied"] + 2 * kc[1]["meeting"]) / kc[1]["contacted"])
            worst = min(judged, key=lambda kc: (kc[1]["replied"] + 2 * kc[1]["meeting"]) / kc[1]["contacted"])
            if best[0] != worst[0]:
                out.append("Best %s so far: %s. Weakest: %s." % (label, rate_line(best[0], best[1]),
                                                                  rate_line(worst[0], worst[1])))
        for k, c in judged:
            if c["opted_out"] / c["contacted"] > OPT_OUT_WORRY:
                out.append("%s: %s of those contacted said no thanks, which is high. Worth looking at "
                           "what that group is being offered." % (k, pct(c["opted_out"], c["contacted"])))
    checked_for_contact = total["contact"] + extra["no_contact"]
    if checked_for_contact >= 10:
        share = extra["no_contact"] / checked_for_contact
        line = ("No contact could be found for %d of %d prospects (%s)." % (
            extra["no_contact"], checked_for_contact, pct(extra["no_contact"], checked_for_contact)))
        if share > NO_CONTACT_WORRY:
            line += (" That is a lot of good prospects nobody can reach. A simple manual route (a phone "
                     "call or a visit) for these may be worth adding.")
        out.append(line)
    if total["contacted"] >= 10 and total["bad_email"] / max(1, total["contacted"]) > 0.15:
        out.append("%s of emails bounced (bad address). The contact finder may need a stricter rule."
                   % pct(total["bad_email"], total["contacted"]))
    if waiting["wa"]:
        out.append("%d WhatsApp message(s) are ready and waiting for you to tap send. Nothing moves for "
                   "those businesses until you do." % len(waiting["wa"]))
    if not out:
        out.append("Nothing stands out this week.")
    return out


# ---------------------------------------------------------------- writing it out
def funnel_line(name, c):
    return ("%s: %d found, %d prospects, %d with a contact, %d contacted, %d replied, %d interested, "
            "%d booked a call, %d said no thanks, %d bad email" % (
                name, c["businesses"], c["prospects"], c["contact"], c["contacted"], c["replied"],
                c["interested"], c["meeting"], c["opted_out"], c["bad_email"]))


def text_report(s, with_names):
    """The scorecard as plain text lines. with_names=False for the public GitHub log."""
    t, w = s["total"], s["week"]
    lines = ["Trinata outreach scorecard, week %s to %s" % (s["week_start"], s["today"]), ""]
    lines += ["THIS WEEK",
              "- New businesses found: %d" % w["sourced"],
              "- Businesses contacted for the first time: %d" % w["contacted"],
              "- Follow-up emails sent: %d" % w["followed_up"],
              "- Replies: %d" % w["replied"],
              "- Calls booked: %d" % w["meetings"], ""]
    if with_names and s["week_replies"]:
        lines += ["Replies this week:"] + ["- " + r for r in s["week_replies"]] + [""]
    if with_names and s["upcoming"]:
        lines += ["CALLS COMING UP"] + ["- " + u for u in s["upcoming"]] + [""]
    lines += ["WHAT IT SUGGESTS"] + ["- " + p for p in s["pointers"]] + [""]
    lines += ["ALL TIME, STEP BY STEP"]
    for k, label in STEPS:
        base = t["contacted"] if k in ("replied", "interested", "meeting", "opted_out", "bad_email") else None
        extra = " (%s of those contacted)" % pct(t[k], base) if base else ""
        lines.append("- %s: %d%s" % (label, t[k], extra))
    lines.append("- Follow-ups sent: %d" % s["extra"]["followed_up"])
    lines.append("- No contact could be found: %d" % s["extra"]["no_contact"])
    lines.append("")
    for title, key in (("BY KIND OF BUSINESS", "by_sub"), ("BY CHANNEL", "by_channel"), ("BY OFFER", "by_offer")):
        groups = s[key]
        if not groups:
            continue
        lines.append(title)
        for name in sorted(groups, key=lambda g: (-groups[g]["contacted"], -groups[g]["businesses"], g)):
            lines.append("- " + funnel_line(name, groups[name]))
        lines.append("")
    if "WhatsApp" in s["by_channel"]:
        lines += ["Note: WhatsApp replies can't be read by the robot, so WhatsApp only shows opt-outs you "
                  "ticked and calls booked.", ""]
    wt = s["waiting"]
    lines += ["WAITING",
              "- Waiting for the fit-check: %d" % wt["fit"],
              "- Waiting for a contact search: %d" % wt["contact"],
              "- Waiting to be written or sent: %d" % wt["send"],
              "- WhatsApp messages waiting for you to tap send: %d" % len(wt["wa"])]
    if with_names and wt["wa"]:
        lines += ["  " + x for x in wt["wa"][:LIST_LIMIT]]
        if len(wt["wa"]) > LIST_LIMIT:
            lines.append("  ...and %d more" % (len(wt["wa"]) - LIST_LIMIT))
    lines += ["", "EVERY STATUS IN THE SHEET"]
    for st in sorted(s["status_counts"], key=lambda k: -s["status_counts"][k]):
        lines.append("- %s: %d" % (st, s["status_counts"][st]))
    lines += ["", "Costs are not in the Sheet: check console.anthropic.com for the month's Claude spend.",
              "Weekly scorecard, version %s." % SCRIPT_VERSION]
    return lines


def tab_grid(s):
    """The Scorecard tab: a few small tables, one under the other."""
    head = ["Group"] + [label for _, label in STEPS] + ["Reply rate", "Call rate"]

    def table(title, groups):
        rows = [[title] + [""] * (len(head) - 1), head]
        for name in sorted(groups, key=lambda g: (-groups[g]["contacted"], -groups[g]["businesses"], g)):
            c = groups[name]
            rows.append([name] + [c[k] for k, _ in STEPS] + [pct(c["replied"], c["contacted"]),
                                                               pct(c["meeting"], c["contacted"])])
        return rows + [[""] * len(head)]

    out = [["Trinata outreach scorecard", "Updated %s (Lagos)" % s["now"].strftime("%Y-%m-%d %H:%M")]
           + [""] * (len(head) - 2), [""] * len(head)]
    out += table("All businesses", {"Everything": s["total"]})
    out += table("By kind of business", s["by_sub"])
    out += table("By channel", s["by_channel"])
    out += table("By offer", s["by_offer"])
    out.append(["What it suggests"] + [""] * (len(head) - 1))
    out += [[p] + [""] * (len(head) - 1) for p in s["pointers"]]
    out.append([""] * len(head))
    w = s["week"]
    out.append(["This week (%s to %s)" % (s["week_start"], s["today"])] + [""] * (len(head) - 1))
    for label, v in (("New businesses found", w["sourced"]), ("First contacted", w["contacted"]),
                     ("Follow-ups sent", w["followed_up"]), ("Replies", w["replied"]),
                     ("Calls booked", w["meetings"])):
        out.append([label, v] + [""] * (len(head) - 2))
    if s["upcoming"]:
        out.append([""] * len(head))
        out.append(["Calls coming up"] + [""] * (len(head) - 1))
        out += [[u] + [""] * (len(head) - 1) for u in s["upcoming"]]
    return out


def history_line(s):
    t, w = s["total"], s["week"]
    return [s["today"], t["businesses"], t["prospects"], t["contact"], t["contacted"], t["replied"],
            t["interested"], t["meeting"], t["opted_out"], t["bad_email"], s["extra"]["no_contact"],
            w["contacted"], w["replied"], w["meetings"]]


def write_tabs(ws, s):
    sh = ws.spreadsheet
    grid = tab_grid(s)
    width = len(grid[0])
    try:
        tab = sh.worksheet(TAB)
    except Exception:
        tab = dm.with_retry(lambda: sh.add_worksheet(title=TAB, rows=max(200, len(grid) + 20), cols=width),
                            "Adding the Scorecard tab")
    if tab.row_count < len(grid) or tab.col_count < width:
        dm.with_retry(lambda: tab.resize(rows=max(tab.row_count, len(grid) + 20), cols=max(tab.col_count, width)),
                      "Making the Scorecard tab bigger")
    dm.with_retry(tab.clear, "Clearing the Scorecard tab")
    dm.with_retry(lambda: tab.update(values=grid, range_name="A1", value_input_option="RAW"),
                  "Writing the Scorecard tab")

    try:
        hist = sh.worksheet(HISTORY_TAB)
    except Exception:
        hist = dm.with_retry(lambda: sh.add_worksheet(title=HISTORY_TAB, rows=300, cols=len(HISTORY_HEADINGS)),
                             "Adding the Scorecard History tab")
        dm.with_retry(lambda: hist.update(values=[HISTORY_HEADINGS], range_name="A1", value_input_option="RAW"),
                      "Writing Scorecard History headings")
    existing = dm.with_retry(hist.get_all_values, "Reading Scorecard History")
    if not existing or not any(c.strip() for c in existing[0]):
        dm.with_retry(lambda: hist.update(values=[HISTORY_HEADINGS], range_name="A1", value_input_option="RAW"),
                      "Writing Scorecard History headings")
        existing = [HISTORY_HEADINGS]
    if any(r and r[0].strip() == s["today"] for r in existing[1:]):
        return False     # already recorded today (the run was repeated): don't add a second line
    dm.with_retry(lambda: hist.append_rows([history_line(s)], value_input_option="RAW"),
                  "Adding to Scorecard History")
    return True


def build_email(to_addr, s):
    t = s["total"]
    msg = EmailMessage()
    msg["From"] = formataddr((FROM_NAME, se.SENDER_EMAIL))
    msg["To"] = to_addr
    msg["Subject"] = ("Outreach scorecard: %d contacted this week, %d replies, %d calls booked"
                      % (s["week"]["contacted"], s["week"]["replied"], s["week"]["meetings"]))
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=OUR_DOMAIN)
    msg["Auto-Submitted"] = "auto-generated"
    body = text_report(s, with_names=True)
    body.insert(1, "All time: %d contacted, %d replied, %d calls booked. The full tables are on the "
                   "'Scorecard' tab of the Sheet." % (t["contacted"], t["replied"], t["meeting"]))
    msg.set_content("\n".join(body) + "\n")
    return msg


def run(ws, grid, alert_to, mailer=None, dry_run=False, now=None):
    """Returns (public report lines, problem text)."""
    s = build(grid, now)
    problems = []
    title = ("## Weekly Scorecard - PREVIEW (nothing written, nothing sent)" if dry_run
             else "## Weekly Scorecard - run report")
    public = [title, "", "- Script version: " + SCRIPT_VERSION, "- Cost: $0 (no Claude)", "", "```"]
    public += text_report(s, with_names=False) + ["```"]
    if not dry_run:
        added = write_tabs(ws, s)
        public.append("- Scorecard tab rewritten; history line %s" % ("added" if added else
                                                                    "already there for today, not repeated"))
        try:
            result, detail = mailer.send(build_email(alert_to, s), alert_to)
        except se.StopRun as e:
            result, detail = "failed", str(e)
        if result == "sent":
            public.append("- Scorecard emailed to %s" % alert_to)
        else:
            problems.append("The scorecard email could not be sent (%s). It is on the Scorecard tab."
                            % dm.clip(detail, 150))
    for p in problems:
        public.append("- PROBLEM: " + p)
    return public, "; ".join(problems)


def main():
    log("Weekly Scorecard, script version " + SCRIPT_VERSION)
    dry_run = is_dry_run()
    alert_to = alert_address()
    mailer = None if dry_run else se.Mailer(se.get_password())
    ws = se.open_sheet()
    grid = dm.with_retry(ws.get_all_values, "Reading the Sheet")
    if not grid:
        fail("The Sheet is empty.")
    try:
        report, problem = run(ws, grid, alert_to, mailer=mailer, dry_run=dry_run)
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
