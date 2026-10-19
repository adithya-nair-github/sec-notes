"""SEC-reporting job ads on the big career-site systems: Workday, iCIMS, SmartRecruiters
(plus Greenhouse / Lever / Ashby when the company's own careers page links to them).

1. discover(): open the company's careers pages (from its domain) and find which system it uses.
2. search(): ask that system's public job search (the same calls its careers page makes) for
   reporting keywords, then read the matching ads.
Light and polite: a handful of requests per company per week, results cached.
"""
import datetime as dt
import re
from urllib.parse import quote

from .eightk import to_text

WORKDAY = re.compile(r"https?://([a-z0-9\-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_\-]+)", re.I)
ICIMS = re.compile(r"https?://([a-z0-9\-]+\.icims\.com)", re.I)
SMARTR = re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9\-]+)", re.I)
GREENHOUSE = re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([a-z0-9_\-]+)", re.I)
LEVER = re.compile(r"jobs\.lever\.co/([a-z0-9_\-]+)", re.I)
ASHBY = re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_\-.]+)", re.I)
CAREER_LINK = re.compile(r"href=[\"']([^\"']*(?:career|jobs|join-us|work-with-us|opportunit)[^\"']*)[\"']", re.I)

SEARCH_TERMS = ["SEC reporting", "XBRL", "external reporting", "Workiva"]
TITLE_HINT = re.compile(r"sec|report|xbrl|controller|accounting|technical|disclosure|financial", re.I)
# the roles that own SEC reporting; anything else (interns, AP, tax, payroll, marketing "reporting"...) is noise
REPORTING_TITLE = re.compile(
    r"(sec|external|financial|finance|corporate|technical|regulatory) (reporting|accounting)|accounting (&|and) reporting|"
    r"reporting (manager|analyst|director|lead|specialist|accountant)|controller|chief accounting|accounting (manager|director|lead)|"
    r"director[^,]{0,30}accounting|\bxbrl\b|disclosure|consolidation", re.I)
NOT_REPORTING_TITLE = re.compile(
    r"intern\b|internship|co-?op\b|student|early talent|graduate|summer 20\d\d|paralegal|marketing|sales|payroll|\btax\b|"
    r"accounts payable|\bap\b|lease|procurement|compensation|rewards|billing|collections|fp&a|planning|treasury|cost account|"
    r"project controller|plant controller|site controller|revenue analyst|benefits", re.I)
NAME_STOP = {"inc", "corp", "corporation", "company", "co", "group", "holdings", "holding", "the", "international", "technologies",
             "technology", "industries", "systems", "solutions", "services", "global", "plc", "ltd", "llc", "trust", "partners", "and"}


def board_owner_ok(board, company, domain=""):
    """The careers board really belongs to this company (a careers page can link a partner's or parent's board)."""
    key = re.sub(r"[^a-z0-9]", "", (board.get("tenant") or board.get("host", "").split(".")[0].replace("careers-", "").replace("jobs-", "")
                                    or board.get("company") or board.get("slug") or "").lower())
    if not key:
        return False
    words = [w for w in re.findall(r"[a-z0-9]+", (company or "").lower()) if len(w) >= 3 and w not in NAME_STOP]
    dom = re.sub(r"[^a-z0-9]", "", (domain or "").lower().replace("www.", "").split(".")[0])
    if any(w in key or key in w for w in words):
        return True
    return bool(dom) and (dom in key or key in dom or dom[:5] == key[:5])
STRONG = ["xbrl", "ixbrl", "workiva", "wdesk", "activedisclosure", "toppan merrill", "disclosure management",
          "sec reporting", "sec filing", "external reporting", "10-k", "10-q", "form 10-k", "form 10-q"]
PROJECT = ["implement", "implementation", "selection", "evaluate", "transition to", "migrate", "migration",
           "new reporting platform", "new disclosure", "system conversion"]
VENDORS = ("workiva", "wdesk", "activedisclosure", "toppan merrill", "dfin", "donnelley", "certent", "insightsoftware")


def _abs(href, base):
    from urllib.parse import urljoin
    return urljoin(base, href)


def find_board_in(html):
    if not html:
        return None
    m = WORKDAY.search(html)
    if m and m.group(3).lower() not in ("wday", "job"):
        return {"ats": "workday", "tenant": m.group(1).lower(), "wd": m.group(2).lower(), "site": m.group(3)}
    m = ICIMS.search(html)
    if m and not m.group(1).lower().startswith(("www.", "community.", "cdn")):
        return {"ats": "icims", "host": m.group(1).lower()}
    m = SMARTR.search(html)
    if m and m.group(1).lower() not in ("oneclick-ui", "embed"):
        return {"ats": "smartrecruiters", "company": m.group(1)}
    for ats, rx in (("greenhouse", GREENHOUSE), ("lever", LEVER), ("ashby", ASHBY)):
        for m in rx.finditer(html):
            if m.group(1).lower() not in ("embed", "js", "assets", "job_board", "jobs"):
                return {"ats": ats, "slug": m.group(1)}
    return None


def discover(http, domain):
    """Which careers system the company uses, from its own careers pages. dict or None."""
    if not domain:
        return None
    d = domain.lower().replace("https://", "").replace("http://", "").strip("/").split("/")[0]
    d = d[4:] if d.startswith("www.") else d
    pages = [f"https://www.{d}/careers", f"https://{d}/careers", f"https://careers.{d}", f"https://jobs.{d}",
             f"https://www.{d}/about/careers", f"https://www.{d}"]
    followed = 0
    for url in pages:
        html = http.get(url, sec=False, tries=1, timeout=20)
        if not html:
            continue
        b = find_board_in(html)
        if b:
            return b
        # one hop: the careers page often links to "search jobs" on the ATS or another page
        for href in CAREER_LINK.findall(html)[:6]:
            if followed >= 3:
                break
            nxt = _abs(href, url)
            b = find_board_in(nxt)
            if b:
                return b
            if d in nxt or "career" in nxt or "jobs" in nxt:
                followed += 1
                b = find_board_in(http.get(nxt, sec=False, tries=1, timeout=20))
                if b:
                    return b
    return None


# ------------------------------------------------------------ searching each system
def _posted_days(text):
    t = (text or "").lower()
    if "today" in t:
        return 0
    if "yesterday" in t:
        return 1
    m = re.search(r"(\d+)\+?\s*day", t)
    return int(m.group(1)) if m else None


def search_workday(http, b, today):
    base = f"https://{b['tenant']}.{b['wd']}.myworkdayjobs.com/wday/cxs/{b['tenant']}/{b['site']}"
    seen, out = set(), []
    for term in SEARCH_TERMS:
        data = http.post_json(base + "/jobs", {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": term})
        for p in (data or {}).get("jobPostings", []):
            path = p.get("externalPath", "")
            if not path or path in seen or not TITLE_HINT.search(p.get("title", "")):
                continue
            seen.add(path)
            age = _posted_days(p.get("postedOn"))
            if age is not None and age > 60:
                continue
            det = http.get(base + path, want="json", sec=False, tries=1) or {}
            info = det.get("jobPostingInfo") or {}
            date = (info.get("startDate") or "")[:10] or ((today - dt.timedelta(days=age)).isoformat() if age is not None else today.isoformat())
            out.append({"title": p.get("title", ""), "date": date, "text": to_text(info.get("jobDescription", "")),
                        "url": f"https://{b['tenant']}.{b['wd']}.myworkdayjobs.com/{b['site']}{path}"})
            if len(out) >= 6:
                return out
    return out


def search_icims(http, b, today):
    seen, out = set(), []
    for term in SEARCH_TERMS:
        html = http.get(f"https://{b['host']}/jobs/search?ss=1&in_iframe=1&searchKeyword={quote(term)}", sec=False, tries=1) or ""
        for jid, title in re.findall(r'href="[^"]*/jobs/(\d+)/[^"]*"[^>]*>\s*(?:<[^>]+>\s*)*([^<]{4,140})', html):
            if jid in seen or not TITLE_HINT.search(title):
                continue
            seen.add(jid)
            page = http.get(f"https://{b['host']}/jobs/{jid}/job?in_iframe=1", sec=False, tries=1) or ""
            m = re.search(r"(?:posted date|date posted)[^0-9]{0,40}(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})", page, re.I)
            date = today.isoformat()
            if m:
                try:
                    v = m.group(1)
                    date = dt.date.fromisoformat(v).isoformat() if "-" in v else dt.datetime.strptime(v, "%m/%d/%Y").date().isoformat()
                except ValueError:
                    pass
            out.append({"title": title.strip(), "date": date, "text": to_text(page)[:20000],
                        "url": f"https://{b['host']}/jobs/{jid}/job"})
            if len(out) >= 6:
                return out
    return out


def search_smartrecruiters(http, b, today):
    seen, out = set(), []
    for term in SEARCH_TERMS:
        data = http.get(f"https://api.smartrecruiters.com/v1/companies/{b['company']}/postings?limit=20&q={quote(term)}",
                        want="json", sec=False, tries=1) or {}
        for p in data.get("content", []):
            pid = p.get("id")
            if not pid or pid in seen or not TITLE_HINT.search(p.get("name", "")):
                continue
            seen.add(pid)
            det = http.get(f"https://api.smartrecruiters.com/v1/companies/{b['company']}/postings/{pid}", want="json", sec=False, tries=1) or {}
            secs = ((det.get("jobAd") or {}).get("sections") or {})
            text = " ".join(to_text((secs.get(k) or {}).get("text", "")) for k in ("jobDescription", "qualifications"))
            out.append({"title": p.get("name", ""), "date": (p.get("releasedDate") or today.isoformat())[:10], "text": text,
                        "url": f"https://jobs.smartrecruiters.com/{b['company']}/{pid}"})
            if len(out) >= 6:
                return out
    return out


BOARD_API = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}


def search_board(http, b, today):
    data = http.get(BOARD_API[b["ats"]].format(slug=b["slug"]), want="json", sec=False, tries=1)
    jobs = []
    try:
        if b["ats"] == "greenhouse":
            for j in (data or {}).get("jobs", []):
                jobs.append({"title": j.get("title", ""), "url": j.get("absolute_url", ""), "date": (j.get("updated_at") or "")[:10], "text": to_text(j.get("content", ""))})
        elif b["ats"] == "lever":
            for j in data or []:
                ts = j.get("createdAt")
                jobs.append({"title": j.get("text", ""), "url": j.get("hostedUrl", ""),
                             "date": dt.datetime.utcfromtimestamp(ts / 1000).date().isoformat() if ts else "", "text": j.get("descriptionPlain", "")})
        else:
            for j in (data or {}).get("jobs", []):
                jobs.append({"title": j.get("title", ""), "url": j.get("jobUrl", ""), "date": (j.get("publishedAt") or "")[:10], "text": j.get("descriptionPlain", "")})
    except (AttributeError, TypeError):
        return []
    return [j for j in jobs if TITLE_HINT.search(j["title"])]


SEARCH = {"workday": search_workday, "icims": search_icims, "smartrecruiters": search_smartrecruiters,
          "greenhouse": search_board, "lever": search_board, "ashby": search_board}


def _near(blob, a_words, b_words, window=120):
    for a in a_words:
        for m in re.finditer(re.escape(a), blob):
            seg = blob[max(0, m.start() - window): m.end() + window]
            if any(x in seg for x in b_words):
                return True
    return False


def classify(postings):
    """Keep ads that are really about SEC / XBRL reporting. [{title,url,date,keywords,project_words,vendors}]"""
    out = []
    for j in postings:
        if not REPORTING_TITLE.search(j["title"]) or NOT_REPORTING_TITLE.search(j["title"]):
            continue
        blob = (j["title"] + " " + j.get("text", "")).lower()
        kws = [k for k in STRONG if k in blob]
        if not kws:
            continue
        out.append({"title": j["title"], "url": j["url"], "date": j["date"] or "", "keywords": kws[:4],
                    "project_words": [p for p in PROJECT if _near(blob, [p], kws)][:3],
                    "vendors": [v for v in VENDORS if v in blob]})
    return out


def signals(cik, company, ats_name, hits, today, max_age_days=75):
    out = []
    cutoff = (today - dt.timedelta(days=max_age_days)).isoformat()
    for h in hits:
        date = h["date"] or today.isoformat()
        if date < cutoff:
            continue
        vend = f" Mentions: {', '.join(h['vendors'])}." if h["vendors"] else ""
        if h["project_words"]:
            typ = "active_tooling_project"
            detail = f"Job ad '{h['title']}' suggests a reporting-tool project ({', '.join(h['project_words'])} near {', '.join(h['keywords'])}).{vend}"
        else:
            typ = "hiring_reporting"
            detail = f"Hiring: '{h['title']}' (mentions {', '.join(h['keywords'])}).{vend}"
        out.append({"type": typ, "cik": cik, "company": company, "date": date, "detail": detail, "url": h["url"],
                    "ref": "state", "source": f"Jobs ({ats_name})"})
    return out
