"""
Homework bot: logs in to Brightspace, collects upcoming assignments, quizzes and
discussions (with their instructions), and sends them to your phone with ntfy.

Settings come from environment variables (GitHub secrets):
  BRIGHTSPACE_URL       e.g. https://yourschool.brightspace.com
  BRIGHTSPACE_USERNAME  your school username or email
  BRIGHTSPACE_PASSWORD  your school password
  NTFY_TOPIC            the private topic name you subscribed to in the ntfy app
  MODE                  test | daily | weekly   (default: daily)
  TIMEZONE              default America/New_York
"""
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import urllib.request
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------- settings
def env(name, default=None, required=True):
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"Missing setting: {name}. Add it under Settings > Secrets and variables > Actions.")
    return (v or "").strip()

raw_url = env("BRIGHTSPACE_URL")
if not raw_url.startswith("http"):
    raw_url = "https://" + raw_url
_u = urlparse(raw_url)
BASE = f"{_u.scheme}://{_u.netloc}"
USERNAME = env("BRIGHTSPACE_USERNAME")
PASSWORD = env("BRIGHTSPACE_PASSWORD")
TOPIC = env("NTFY_TOPIC")
MODE = (os.environ.get("MODE") or "daily").strip().lower()
TZ = ZoneInfo(os.environ.get("TIMEZONE") or "America/New_York")
NOW = dt.datetime.now(dt.timezone.utc)
DEBUG_DIR = "debug"


# ---------------------------------------------------------------- phone notifications
def notify(title, message, tags=None, priority=3, click=None):
    body = {"topic": TOPIC, "title": title[:250], "message": message[:3900],
            "priority": priority}
    if tags:
        body["tags"] = tags
    if click:
        body["click"] = click
    req = urllib.request.Request(
        "https://ntfy.sh/", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


# ---------------------------------------------------------------- text helpers
def clean_text(rich):
    """Turn a Brightspace RichText ({Text, Html}) or string into short plain lines."""
    if not rich:
        return ""
    if isinstance(rich, dict):
        if "Text" in rich and isinstance(rich["Text"], dict):  # quiz Description {Text:{Text,Html}}
            rich = rich["Text"]
        txt = rich.get("Text") or ""
        if not txt.strip() and rich.get("Html"):
            h = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", rich["Html"])
            h = re.sub(r"(?i)<li[^>]*>", "\n• ", h)
            txt = html.unescape(re.sub(r"<[^>]+>", " ", h))
    else:
        txt = str(rich)
    lines = []
    for ln in txt.replace("\r", "").split("\n"):
        ln = re.sub(r"[ \t ]+", " ", ln).strip()
        if ln:
            lines.append(ln)
    out = "\n".join(lines)
    if len(out) > 1200:
        out = out[:1200].rsplit(" ", 1)[0] + " …"
    return out


def parse_date(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_due(d):
    local = d.astimezone(TZ)
    t = local.strftime("%I:%M%p").lstrip("0").lower()
    return local.strftime("%a %b ") + str(local.day) + ", " + t


# ---------------------------------------------------------------- browser + login
def save_debug(page, name):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        page.screenshot(path=f"{DEBUG_DIR}/{name}.png", full_page=True)
    except Exception:
        pass


def api(page, path):
    """GET a Brightspace API path using the logged-in browser session."""
    r = page.evaluate(
        """async (p) => {
            const r = await fetch(p, {credentials: 'include', headers: {'Accept': 'application/json'}});
            return {status: r.status, text: await r.text()};
        }""", path)
    if r["status"] != 200:
        return None
    try:
        return json.loads(r["text"])
    except ValueError:
        return None


def latest_versions(page):
    data = api(page, "/d2l/api/versions/") or []
    v = {d.get("ProductCode"): d.get("LatestVersion") for d in data if isinstance(d, dict)}
    return v.get("lp") or "1.30", v.get("le") or "1.67"


def is_logged_in(page, lp):
    if "/d2l/" not in page.url or "/d2l/login" in page.url:
        return False
    return api(page, f"/d2l/api/lp/{lp}/users/whoami") is not None


def settle(page):
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass


def visible(page, selector):
    loc = page.locator(selector)
    for i in range(loc.count()):
        if loc.nth(i).is_visible():
            return loc.nth(i)
    return None


USER_FIELDS = ("input[type=email], input[name=userName], input#userName, input[name=loginfmt], "
               "input[autocomplete=username], input[name*=user i], input[id*=user i], "
               "input[name*=login i], input[type=text]")


def login(page):
    page.goto(BASE + "/d2l/login", wait_until="domcontentloaded")
    settle(page)
    lp, _ = latest_versions(page)
    for step in range(8):
        settle(page)
        if is_logged_in(page, lp):
            return
        pw = visible(page, "input[type=password]")
        user = visible(page, USER_FIELDS)
        if pw:
            if user and not user.input_value():
                user.fill(USERNAME)
            pw.fill(PASSWORD)
            pw.press("Enter")
        elif user:
            user.fill(USERNAME)
            user.press("Enter")
        else:
            # e.g. a "Sign in with school account" button, or "Stay signed in? Yes"
            clicked = False
            for name in (r"^yes$", r"continue", r"next", r"sign in|log in|login"):
                btn = page.get_by_role("button", name=re.compile(name, re.I))
                if btn.count() and btn.first.is_visible():
                    btn.first.click(); clicked = True; break
                link = page.get_by_role("link", name=re.compile(name, re.I))
                if link.count() and link.first.is_visible():
                    link.first.click(); clicked = True; break
            if not clicked:
                break
        page.wait_for_timeout(2500)
    settle(page)
    if not is_logged_in(page, lp):
        # last try: go home in case login finished on another page
        page.goto(BASE + "/d2l/home", wait_until="domcontentloaded")
        settle(page)
        if not is_logged_in(page, lp):
            save_debug(page, "login-failed")
            raise RuntimeError("Couldn't log in to Brightspace. Check your username, password "
                               "and BRIGHTSPACE_URL. A screenshot of where it got stuck is in "
                               "the run's 'debug' download.")


# ---------------------------------------------------------------- collecting work
def paged_items(page, path):
    """Brightspace 'PagedResultSet' (Items + Bookmark)."""
    items, bookmark = [], None
    for _ in range(20):
        p = path + (("&" if "?" in path else "?") + "bookmark=" + bookmark if bookmark else "")
        data = api(page, p)
        if not data:
            break
        items += data.get("Items", [])
        info = data.get("PagingInfo") or {}
        if not info.get("HasMoreItems"):
            break
        bookmark = info.get("Bookmark")
    return items


def courses(page, lp):
    out = []
    items = paged_items(page, f"/d2l/api/lp/{lp}/enrollments/myenrollments/?orgUnitTypeId=3")
    if not items:  # some schools use a different type id for classes
        items = [it for it in paged_items(page, f"/d2l/api/lp/{lp}/enrollments/myenrollments/")
                 if "course" in (((it.get("OrgUnit") or {}).get("Type") or {}).get("Code") or "").lower()]
    for it in items:
        ou, acc = it.get("OrgUnit") or {}, it.get("Access") or {}
        if acc.get("IsActive", True) and acc.get("CanAccess", True):
            out.append({"id": ou.get("Id"), "name": ou.get("Name") or "Class"})
    return out


def short_course(name):
    # "BIO101 - Biology (Fall 2026) - Section 3" -> keep it readable
    name = re.sub(r"\s*\((Fall|Spring|Summer|Winter)[^)]*\)", "", name, flags=re.I)
    return name.strip()[:60]


STATS = {}


def count(kind, due, course, name):
    STATS[kind] = STATS.get(kind, 0) + 1
    if not due:
        STATS["no_date"] = STATS.get("no_date", 0) + 1
        if len(STATS.setdefault("examples", [])) < 6:
            STATS["examples"].append(f"{course}: {name}")


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def api_date(d):
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def collect(page, lp, le, start, end):
    work, topics = [], {}
    course_list = courses(page, lp)
    STATS.update(classes=len(course_list), assignments=0, quizzes=0, discussions=0, no_date=0, examples=[])
    names = {c["id"]: short_course(c["name"]) for c in course_list}
    for c in course_list:
        ou, cname = c["id"], short_course(c["name"])

        # Assignments
        for f in api(page, f"/d2l/api/le/{le}/{ou}/dropbox/folders/") or []:
            if f.get("IsHidden"):
                continue
            due = parse_date(f.get("DueDate")) or parse_date((f.get("Availability") or {}).get("EndDate"))
            count("assignments", due, cname, f.get("Name"))
            if not due or not (start <= due <= end):
                continue
            subs = api(page, f"/d2l/api/le/{le}/{ou}/dropbox/folders/{f['Id']}/submissions/mysubmissions/")
            submitted = bool(subs) and any(
                isinstance(s, dict) and (s.get("Submissions") or s.get("Status") in (1, 3)) for s in subs)
            work.append({"id": f"a-{ou}-{f['Id']}", "kind": "Assignment", "course": cname, "title": f.get("Name", "Assignment"),
                         "due": due, "done": submitted,
                         "details": clean_text(f.get("CustomInstructions")),
                         "link": f"{BASE}/d2l/lms/dropbox/user/folder_submit_files.d2l?db={f['Id']}&ou={ou}"})

        # Quizzes / tests
        qpath = f"/d2l/api/le/{le}/{ou}/quizzes/"
        for _ in range(10):
            data = api(page, qpath)
            if not data:
                break
            for q in data.get("Objects", []):
                if q.get("IsActive") is False:
                    continue
                due = parse_date(q.get("DueDate")) or parse_date(q.get("EndDate"))
                count("quizzes", due, cname, q.get("Name"))
                if not due or not (start <= due <= end):
                    continue
                details = clean_text(q.get("Instructions")) or clean_text(q.get("Description"))
                work.append({"id": f"q-{ou}-{q.get('QuizId')}", "kind": "Quiz", "course": cname, "title": q.get("Name", "Quiz"),
                             "due": due, "done": False, "details": details,
                             "link": f"{BASE}/d2l/lms/quizzing/user/quiz_summary.d2l?qi={q.get('QuizId')}&ou={ou}"})
            nxt = data.get("Next")
            if not nxt:
                break
            qpath = urlparse(nxt)._replace(scheme="", netloc="").geturl()

        # Discussions
        for forum in api(page, f"/d2l/api/le/{le}/{ou}/discussions/forums/") or []:
            if forum.get("IsHidden"):
                continue
            fid = forum.get("ForumId")
            for t in api(page, f"/d2l/api/le/{le}/{ou}/discussions/forums/{fid}/topics/") or []:
                if t.get("IsHidden"):
                    continue
                details = clean_text(t.get("Description")) or clean_text(forum.get("Description"))
                topics[(ou, norm(t.get("Name")))] = {
                    "details": details,
                    "link": f"{BASE}/d2l/le/{ou}/discussions/topics/{t.get('TopicId')}/View"}
                due = (parse_date(t.get("DueDate")) or parse_date(t.get("EndDate"))
                       or parse_date(forum.get("DueDate")) or parse_date(forum.get("EndDate")))
                count("discussions", due, cname, t.get("Name"))
                if not due or not (start <= due <= end):
                    continue
                work.append({"id": f"d-{ou}-{t.get('TopicId')}", "kind": "Discussion", "course": cname, "title": t.get("Name", "Discussion"),
                             "due": due, "done": False, "details": details,
                             "link": f"{BASE}/d2l/le/{ou}/discussions/topics/{t.get('TopicId')}/View"})
    # Everything Brightspace itself lists as due (the same list as "Work To Do").
    # This catches discussions and other items whose due date is set somewhere else,
    # such as on the course's Content page.
    seen = {(norm(w["course"]), norm(w["title"])) for w in work}
    path = (f"/d2l/api/le/{le}/content/myItems/due/?startDateTime={api_date(start)}"
            f"&endDateTime={api_date(end)}")
    for _ in range(10):
        data = api(page, path)
        if not data:
            break
        for it in data.get("Objects", []):
            if it.get("IsExempt"):
                continue
            due = parse_date(it.get("DueDate")) or parse_date(it.get("EndDate"))
            if not due or not (start <= due <= end):
                continue
            ou = it.get("OrgUnitId")
            cname = names.get(ou, "Class")
            title = it.get("ItemName") or "To do"
            key = (norm(cname), norm(title))
            if key in seen:
                continue
            seen.add(key)
            url = it.get("ItemUrl") or ""
            low = url.lower()
            kind = ("Discussion" if "discussion" in low else "Quiz" if "quiz" in low
                    else "Assignment" if "dropbox" in low else "To do")
            match = topics.get((ou, norm(title)))
            if not match and kind == "Discussion":
                match = next((v for (o, n), v in topics.items() if o == ou and (n in norm(title) or norm(title) in n) and n), None)
            link = url if url.startswith("http") else (BASE + url if url.startswith("/") else f"{BASE}/d2l/home/{ou}")
            work.append({"kind": kind, "course": cname, "title": title, "due": due,
                         "done": bool(it.get("DateCompleted")),
                         "details": (match or {}).get("details", ""),
                         "link": (match or {}).get("link") or link})
        nxt = data.get("Next")
        if not nxt:
            break
        path = urlparse(nxt)._replace(scheme="", netloc="").geturl()

    # Course Schedule (the calendar): due dates teachers put on the schedule.
    path = (f"/d2l/api/le/{le}/calendar/events/myEvents/?startDateTime={api_date(start)}"
            f"&endDateTime={api_date(end)}")
    for _ in range(10):
        data = api(page, path)
        if data is None:
            STATS["schedule_error"] = True
            break
        objs = data.get("Objects", data if isinstance(data, list) else [])
        for ev in objs:
            STATS["schedule"] = STATS.get("schedule", 0) + 1
            due = (parse_date(ev.get("EndDateTime")) or parse_date(ev.get("StartDateTime")))
            if not due and ev.get("EndDay"):
                try:
                    d = dt.date.fromisoformat(str(ev["EndDay"])[:10])
                    due = dt.datetime(d.year, d.month, d.day, 23, 59, tzinfo=TZ)
                except ValueError:
                    due = None
            if not due or not (start <= due <= end):
                continue
            ou = ev.get("OrgUnitId")
            cname = names.get(ou) or short_course(ev.get("OrgUnitName") or "Class")
            title = re.sub(r"\s*-\s*(Due|Due Date|Ends|End Date|Availability Ends)\s*$", "",
                           ev.get("Title") or "To do", flags=re.I).strip()
            key = (norm(cname), norm(title))
            if key in seen:
                continue
            seen.add(key)
            ent = str((ev.get("AssociatedEntity") or {}).get("AssociatedEntityType") or "").lower()
            kind = ("Discussion" if "discussion" in ent else "Quiz" if "quiz" in ent
                    else "Assignment" if "dropbox" in ent else "To do")
            match = topics.get((ou, norm(title))) if kind == "Discussion" else None
            url = ev.get("CalendarEventViewUrl") or ""
            link = url if url.startswith("http") else (BASE + url if url.startswith("/") else f"{BASE}/d2l/home/{ou}")
            work.append({"kind": kind, "course": cname, "title": title, "due": due, "done": False,
                         "details": (match or {}).get("details") or clean_text(ev.get("Description")),
                         "link": (match or {}).get("link") or link})
        nxt = data.get("Next") if isinstance(data, dict) else None
        if not nxt:
            break
        path = urlparse(nxt)._replace(scheme="", netloc="").geturl()

    for w in work:
        if not w.get("id"):
            w["id"] = "x-" + hashlib.sha1(f"{w['course']}|{w['title']}|{w['due'].isoformat()}".encode()).hexdigest()[:12]
    work.sort(key=lambda w: w["due"])
    return work


def save_list(work):
    """Save the list for the Homework Tracker app to read."""
    data = {"updated": NOW.isoformat(), "items": [
        {"id": w["id"], "kind": w["kind"], "course": w["course"], "title": w["title"],
         "due": w["due"].astimezone(dt.timezone.utc).isoformat(), "submitted": w["done"],
         "details": w["details"], "link": w["link"]} for w in work]}
    with open("assignments.json", "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- sending
ICON = {"Assignment": "memo", "Quiz": "pencil2", "Discussion": "speech_balloon"}


def send_item(w):
    hours = (w["due"] - NOW).total_seconds() / 3600
    msg = f"Due {fmt_due(w['due'])}"
    if w["details"]:
        msg += "\n\n" + w["details"]
    else:
        msg += "\n\n(No instructions posted — tap to open it in Brightspace.)"
    notify(f"{w['course']} – {w['title']}", msg, tags=[ICON.get(w["kind"], "books")],
           priority=4 if hours <= 24 else 3, click=w["link"])


def summary_lines(items):
    return "\n".join(f"• {fmt_due(w['due'])} — {w['course']}: {w['title']}" for w in items)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        try:
            login(page)
            lp, le = latest_versions(page)
            if MODE == "weekly":
                end = NOW + dt.timedelta(days=8)
            elif MODE == "test":
                end = NOW + dt.timedelta(days=14)
            else:
                end = NOW + dt.timedelta(hours=48)
            everything = collect(page, lp, le, NOW - dt.timedelta(days=7), NOW + dt.timedelta(days=35))
            save_list(everything)
            work = [w for w in everything if not w["done"] and NOW <= w["due"] <= end]
        except Exception as e:
            save_debug(page, "error")
            notify("⚠️ Homework bot had a problem", str(e)[:800], tags=["warning"], priority=4)
            raise
        finally:
            browser.close()

    if MODE == "test":
        st = STATS
        notify("🔎 What the bot can see",
               f"Classes: {st.get('classes', 0)}\n"
               f"Assignments: {st.get('assignments', 0)} · Quizzes: {st.get('quizzes', 0)} · "
               f"Discussions: {st.get('discussions', 0)}\n"
               f"With NO due date set: {st.get('no_date', 0)}\n"
               f"Course Schedule items: {'could not read' if st.get('schedule_error') else st.get('schedule', 0)}\n\n" +
               ("Some without a due date:\n" + "\n".join("• " + e for e in st.get("examples", []))
                if st.get("examples") else ""), tags=["mag"])
        notify("✅ Homework bot is working",
               f"Logged in and found {len(work)} thing(s) due in the next 2 weeks "
               f"({sum(w['kind']=='Discussion' for w in work)} discussion(s)).\n\n" +
               (summary_lines(work[:15]) or "Nothing due right now."), tags=["white_check_mark"])
        for w in work[:3]:
            send_item(w)
    elif MODE == "weekly":
        notify(f"📅 This week: {len(work)} thing(s) due",
               summary_lines(work) or "Nothing due this week 🎉", tags=["calendar"])
        for w in work:
            send_item(w)
    else:  # daily
        for w in work:
            send_item(w)
    print(f"Done. Mode={MODE}, items sent={len(work)}")


if __name__ == "__main__":
    main()
