"""HiWi alert for the FSMB BaSaMa & HiWi board.

Fetches the HiWi-filtered listing, compares against previously seen entries
(seen.json next to this script) and emails new ones via Gmail SMTP.

Usage:
    python hiwi_alert.py            # check and notify
    python hiwi_alert.py --dry-run  # print what would be sent, update nothing
    python hiwi_alert.py --test-mail  # send a test email

Config (config.json next to this script, git-ignored):
    {"to": "you@gmail.com", "from": "you@gmail.com", "app_password": "xxxx xxxx xxxx xxxx",
     "pages": 2, "keywords": []}
Environment variables HIWI_TO, HIWI_FROM and GMAIL_APP_PASSWORD override config.json
(used by the GitHub Actions workflow).
"""

import argparse
import html
import json
import os
import re
import smtplib
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from email.mime.text import MIMEText
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path

BASE = "https://classic.fsmb.de"
LIST_URL = BASE + "/basama-hiwi?tx_basama_basama%5Bcontroller%5D=Basama&cHash=e1d32efb81b4d6b6f9e44142714ea2a5"
HERE = Path(__file__).resolve().parent
SEEN_FILE = HERE / "seen.json"
CONFIG_FILE = HERE / "config.json"
LOG_FILE = HERE / "hiwi_alert.log"

opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
opener.addheaders = [("User-Agent", "Mozilla/5.0 (hiwi-alert personal script)")]


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


class FormParser(HTMLParser):
    """Collects the hidden inputs of #basama-form (Extbase referrer + HMAC tokens)."""

    def __init__(self):
        super().__init__()
        self.in_form = False
        self.action = None
        self.hidden = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and a.get("id") == "basama-form":
            self.in_form = True
            self.action = a.get("action")
        elif tag == "input" and self.in_form and a.get("type") == "hidden" and a.get("name"):
            self.hidden[a["name"]] = a.get("value") or ""

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_form = False


ROW_RE = re.compile(r"<tr>\s*<td><a href=\"(/basama-hiwi/entry/(\d+)[^\"]*)\">(.*?)</a></td>(.*?)</tr>", re.S)
CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)


def parse_rows(page):
    entries = []
    for href, eid, title, rest in ROW_RE.findall(page):
        cells = CELL_RE.findall(rest)
        flags = ["fa-check" in c for c in cells[:8]]  # BA SA MA DA HW exp the kon
        inst = re.search(r'title="([^"]*)"[^>]*>\s*([^<]*?)\s*</a>', cells[8]) if len(cells) > 8 else None
        date = re.sub(r"<[^>]+>|\s+", "", cells[9]) if len(cells) > 9 else ""
        entries.append({
            "id": eid,
            "title": html.unescape(title.strip()),
            "url": BASE + html.unescape(href),
            "hiwi": flags[4] if len(flags) > 4 else False,
            "institute": html.unescape(inst.group(1)) if inst else "",
            "date": date,
        })
    return entries


def fetch_hiwi(pages):
    first = opener.open(LIST_URL, timeout=30).read().decode("utf-8", "replace")
    fp = FormParser()
    fp.feed(first)
    if not fp.hidden:
        raise RuntimeError("search form not found - page layout changed?")
    action = urllib.parse.urljoin(BASE, html.unescape(fp.action))
    entries = {}
    for p in range(1, pages + 1):
        data = dict(fp.hidden)
        data["tx_basama_basama[demand][hiwi]"] = "1"
        data["tx_basama_basama[demand][page]"] = str(p)
        body = urllib.parse.urlencode(data).encode()
        page = opener.open(action, data=body, timeout=30).read().decode("utf-8", "replace")
        rows = parse_rows(page)
        if not rows:
            break
        for e in rows:
            if e["hiwi"]:  # double check the HW column in case the filter is ignored
                entries.setdefault(e["id"], e)
    return list(entries.values())


def send_mail(cfg, subject, text):
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = cfg["to"]
    with smtplib.SMTP_SSL(cfg.get("smtp_host", "smtp.gmail.com"), cfg.get("smtp_port", 465), timeout=30) as s:
        s.login(cfg["from"], cfg["app_password"].replace(" ", ""))
        s.send_message(msg)


def format_entries(entries):
    return "\n\n".join(
        f"- {e['title']}\n  {e['institute']} | eingetragen {e['date']}\n  {e['url']}" for e in entries
    )


def main():
    if sys.stdout:  # None under pythonw (scheduled task)
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-mail", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
    for key, env in (("to", "HIWI_TO"), ("from", "HIWI_FROM"), ("app_password", "GMAIL_APP_PASSWORD")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]

    if args.test_mail:
        send_mail(cfg, "HiWi alert: test", "Test mail from hiwi_alert.py - email delivery works.")
        log("test mail sent")
        return

    entries = fetch_hiwi(int(cfg.get("pages", 2)))
    keywords = [k.lower() for k in cfg.get("keywords", [])]
    if keywords:
        entries = [e for e in entries if any(k in (e["title"] + " " + e["institute"]).lower() for k in keywords)]

    first_run = not SEEN_FILE.exists()
    seen = set(json.loads(SEEN_FILE.read_text())) if not first_run else set()
    new = [e for e in entries if e["id"] not in seen]

    if args.dry_run:
        print(f"{len(entries)} HiWi entries fetched, {len(new)} not yet seen:\n")
        print(format_entries(new or entries))
        return

    if new and not first_run:
        subject = f"{len(new)} neue HiWi-Stelle(n): {new[0]['title'][:60]}"
        send_mail(cfg, subject, format_entries(new) + f"\n\nAlle Stellen: {LIST_URL}")
        log(f"mailed {len(new)} new: {[e['id'] for e in new]}")
    elif first_run:
        log(f"first run: recorded {len(entries)} existing entries, no mail")
    else:
        log("no new entries")

    SEEN_FILE.write_text(json.dumps(sorted(seen | {e["id"] for e in entries})))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"ERROR: {exc!r}")
        sys.exit(1)
