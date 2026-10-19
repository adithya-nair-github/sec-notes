"""Earnings-call transcripts, built in-house (v2).

For every earnings 8-K (Item 2.02) of a watchlist company we try to get what management
actually said on the call, cheapest source first:

  0. own_transcript   a transcript you dropped in state/transcripts_in/<cik>_<YYYY-MM-DD>.txt
     hf_transcript    the open dataset defeatbeta/yahoo-finance-data on Hugging Face
                      (table stock_earning_call_transcripts, licence ODC-BY; speaker-split paragraphs)

  1. sec_transcript   the company filed the transcript itself with the SEC (8-K exhibit)
  2. ir_transcript    the investor-relations / webcast page links a transcript (HTML or PDF)
  3. captions         the call is on YouTube and has captions (no speech-to-text needed)
  4. audio            a replay we can download: a direct file, a stream the webcast player
                      loads (found by watching the page's network traffic in a headless
                      browser, the "reverse engineer the player" step), or YouTube audio.
                      Turned into text with open-source faster-whisper.

We only take what the page serves openly. Pages that ask for a registration form are
recorded as "registration_required" and skipped; nothing is ever submitted on anyone's behalf.

The text is then read for finance pain, quote by quote, with who said it (CFO / CEO /
management; analyst questions are never quoted as the company's words) and when.
Every step records why it stopped, so the reports show where the funnel leaks.
"""
import datetime as dt
import gzip
import html as htmlmod
import io
import json
import os
import re
import subprocess

from . import earnings, eightk

VERSION = 5

URL = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)
CALL_HINT = re.compile(r"webcast|conference ?call|earnings ?call|replay|events?\.|investor|\bir\.|q4inc|q4cdn|"
                       r"edge\.media-server|onlinexperiences|choruscall|notified|event\.on24|wsw\.com|"
                       r"viavid|webcasts?\.|streetevents|youtube|youtu\.be|vimeo|brightcove|kaltura", re.I)
MEDIA_FILE = re.compile(r"https?:(?:\\?/){2}[^\s\"'<>]+?\.(?:mp3|m4a|mp4|wav|aac|m3u8|mpd)(?:\?[^\s\"'<>]*)?", re.I)
YOUTUBE = re.compile(r"(?:youtube\.com/(?:watch\?v=|embed/|live/)|youtu\.be/)([A-Za-z0-9_\-]{11})", re.I)
VIMEO = re.compile(r"player\.vimeo\.com/video/(\d+)|vimeo\.com/(\d+)", re.I)
TRANSCRIPT_LINK = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.{0,200}?)</a>", re.I | re.S)
JUNK_HOSTS = re.compile(r"sec\.gov|facebook\.com|twitter\.com|x\.com/|linkedin\.com|instagram\.com|schema\.org|w3\.org|"
                        r"businesswire\.com|prnewswire\.com|globenewswire\.com/(?:Tracker|en/search)", re.I)
# web addresses printed without http:// ("ir.example.com/upcoming-events"); never part of an email address
BARE_URL = re.compile(r"(?<![@\w./-])((?:www\.)?(?:[a-z0-9-]+\.)+(?:com|net|org|io|co|us|biz|info|investor)(?:/[^\s\"'<>)\],;]*)?)", re.I)
WEBCAST_HOST = re.compile(r"edge\.media-server\.com|q4inc\.com|notified\.com|webcasts\.com|choruscall|onlinexperiences|viavid|"
                          r"on24\.com|wsw\.com|brightcove|kaltura|zoom\.us|webcaster|streamingmedia|youtube\.com|youtu\.be|vimeo\.com", re.I)
TRANSCRIPT_WORD = re.compile(r"transcript|prepared remarks|cfo commentary|management(?:'s)? remarks|call script|shareholder letter", re.I)
EVENT_HREF = re.compile(r"event", re.I)
NOT_EVENT = re.compile(r"press[-_ ]?release|news[-_ ]?release|/news/|/releases?/|/news$|sec[-_]filings|stock[-_]info", re.I)
EARN_WORD = re.compile(r"earnings|results|quarter|\bq[1-4]\b|fiscal|conference call|webcast|events?\b", re.I)
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
DATE_ANY = re.compile(r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? \d{1,2}(?:st|nd|rd|th)?,? \d{4}|"
                      r"\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2}", re.I)
# transcript sites we never use (paid, login-walled or third-party copies); a transcript must come from the company's own site
BLOCKED_TRANSCRIPT = re.compile(r"seekingalpha|fool\.com|factset|refinitiv|capitaliq|spglobal|bamsec|quartr|tikr|statementdog|investing\.com|"
                                r"gurufocus|marketbeat|benzinga|finviz|yahoo\.|nasdaq\.com|stockalarm|alphaspread|roic\.ai|fiscal\.ai|koyfin|"
                                r"marketscreener|zacks|morningstar|bloomberg|reuters|wsj\.com|investorplace|stocktitan", re.I)
IR_CDN = re.compile(r"q4cdn\.com|q4web\.com|gcs-web\.com|investorroom\.com|mziq\.com|mzweb|cloudfront\.net|amazonaws\.com|"
                    r"windows\.net|notified\.com|irpass|sec\.gov|b-cdn\.net|equisolve|issuerdirect|globenewswire\.com/\w+/resource", re.I)
REGISTRATION = re.compile(r"type=[\"']email[\"']|register (now|to (view|attend|listen))|registration (is )?required|"
                          r"sign in to (view|watch)|enter your (email|name)", re.I)

# what management says that the press release doesn't
CALL_PATTERNS = {
    "call_finance_systems": re.compile(
        r"\berp\b|new (accounting|financial|reporting|finance) (system|platform|software)|"
        r"finance transformation|system(s)? (implementation|migration|conversion|upgrade)|automat\w+ (of )?(the |our )?(close|reporting)|"
        r"(consolidation|reporting|disclosure) (tool|software|platform)|\bworkday\b|\boracle (cloud|fusion)\b|\bs/?4 ?hana\b|\bnetsuite\b", re.I),
    "call_close_controls_audit": re.compile(
        r"(financial|month[- ]end|quarter[- ]end|year[- ]end) close|material weakness|remediat\w+|"
        r"(new|change in|changed|transition\w* (of|to)) (our )?(independent )?(auditor|audit firm)|restat\w+|"
        r"(delay\w*|late) (in )?(the )?(filing|our 10-?k|our 10-?q)|\bsox\b|sarbanes|internal controls?", re.I),
    "call_reporting_regs": re.compile(
        r"\bcsrd\b|\bissb\b|climate (disclosure|reporting|rule)|sb[- ]?253|sb[- ]?261|scope [123] (emissions|reporting)|"
        r"sustainability (report|reporting|disclosure)|\bxbrl\b|ixbrl|esg (report|data|disclosure)|"
        r"(sec|regulatory|external) reporting|segment reporting|asu 20\d\d", re.I),
    "call_cost_headcount": re.compile(
        r"(headcount|workforce|staff(ing)?) (reduction|freeze|cuts?)|hiring freeze|reduction in force|layoffs?|"
        r"(reduc|cut)\w* (our )?(headcount|workforce|g&a|sg&a|overhead|corporate costs?)|"
        r"(g&a|sg&a|overhead|back[- ]office|corporate)[a-z\s\-]{0,25}(reduction|savings program|take ?out|rationaliz\w+)|"
        r"(restructuring|transformation|efficiency|cost[- ]savings|cost[- ]reduction) (program|plan|initiative)s?\b|"
        r"streamlin\w+ (our )?(organization|finance|back office|g&a|corporate|structure)|shared services|"
        r"lean(er)? (team|organization)|do more with less", re.I),
    "call_mna_integration": re.compile(
        r"integrat\w+ (of )?(the |our )?(acquisition|acquired|business)|purchase (price )?accounting|"
        r"(carve[- ]out|spin[- ]?off) (financials|reporting|readiness)|stand[- ]?alone (public )?company|"
        r"(closed|completed) (the|our) acquisition", re.I),
    "call_leadership": re.compile(
        r"(new|incoming|interim|former) (chief financial officer|cfo|controller|chief accounting officer)|"
        r"welcome (our|to) (new )?(cfo|chief financial officer)|cfo (transition|search)|search for (a )?(new )?cfo", re.I),
}
# the operator's opening ("after the presentation there will be a question-and-answer session") is not the Q&A
QA_FUTURE = re.compile(r"will be|will conduct|will open|following the|after the|later|at the end|instructions|"
                       r"to ask a question|in order to ask|you may ask|please press", re.I)
QA_START = re.compile(r"question[- ]and[- ]answer|q ?& ?a (session|portion)|open (the|up the) (line|lines|floor|call) (for|to) questions|"
                      r"first question (comes|is|will come) from|take (our|the) first question", re.I)
NEXT_QUESTION = re.compile(r"(next|first|following|final|last) question (comes|is|will come) from|"
                           r"question (comes|is) from|your line is (now )?open|go ahead with your question", re.I)
HANDOFF = re.compile(r"(?:turn|hand|pass)(?:ing)? (?:the call |it |things |the line )?(?:over|back)? ?(?:over )?to "
                     r"([A-Z][a-z]+(?: [A-Z][a-z]+)?)", re.S)
ROLE_NEAR = re.compile(r"chief financial officer|\bcfo\b|finance chief", re.I)
CEO_NEAR = re.compile(r"chief executive|\bceo\b|(?<!vice )president", re.I)
ANALYST_TELL = re.compile(r"\?\s*$|thanks for taking (my|the) question|congrats on|congratulations on", re.I)


# ============================================================ sources
def release_links(raw_html):
    """Call / webcast links mentioned in the earnings press release (raw HTML keeps hrefs)."""
    seen, out = set(), []
    raw = raw_html or ""
    found = [htmlmod.unescape(u.rstrip(".,;")) for u in URL.findall(raw)]
    text = re.sub(r"https?://\S+", " ", re.sub(r"(?s)<[^>]+>", " ", htmlmod.unescape(raw)))
    found += ["https://" + u.rstrip(".,;") for u in BARE_URL.findall(text)]
    for u in found:
        if CALL_HINT.search(u) and not JUNK_HOSTS.search(u) and u.lower() not in seen:
            seen.add(u.lower())
            out.append(u)
    return out[:8]


def to_paras(raw):
    """HTML to text that keeps paragraph breaks (speaker labels and turns depend on them)."""
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw or "")
    raw = re.sub(r"(?i)<br\s*/?>|</(?:p|div|tr|li|h[1-6]|table|section)>", "\n\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = htmlmod.unescape(raw)
    raw = re.sub(r"[ \t\r\f\v\xa0]+", " ", raw)
    return re.sub(r"\s*\n\s*\n\s*", "\n\n", raw).strip()


def exhibit_docs(http, cik, adsh, names_fn=None):
    """[(name, raw_html)] for the 8-K's EX-99 exhibits."""
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh.replace('-', '')}"
    idx = http.get(base + "/index.json", want="json")
    out = []
    for name in (names_fn or earnings.exhibit_names)(idx):
        raw = http.get(f"{base}/{name}")
        if raw:
            out.append((name, raw))
    return out


def looks_like_transcript(text):
    t = text[:200000]
    return (len(t) > 15000 and re.search(r"\boperator\b", t, re.I)
            and (QA_START.search(t) or NEXT_QUESTION.search(t)) is not None)


REMARKS_TITLE = re.compile(r"prepared remarks|cfo commentary|commentary (from|by) (the )?(cfo|chief financial officer)|"
                           r"letter to (our )?(shareholders|stockholders)|shareholder letter|conference call script|earnings call script", re.I)
PRESS_RELEASE = re.compile(r"(reports|announces|reported|announced)[^.]{0,80}(results|earnings)", re.I)


def looks_like_remarks(text):
    """Management's own prepared remarks / CFO commentary (filed or posted): no Q&A, all management."""
    head = text[:1500]
    return len(text) > 8000 and REMARKS_TITLE.search(head) is not None and not PRESS_RELEASE.search(text[:600])


def sec_transcript(http, cik, adshs):
    """A transcript the company filed with the SEC itself (earnings 8-K or a follow-up 8-K)."""
    for adsh in adshs:
        for name, raw in exhibit_docs(http, cik, adsh, names_fn=_all_exhibits):
            text = to_paras(raw)
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh.replace('-', '')}/{name}"
            if looks_like_transcript(text):
                return {"kind": "sec_transcript", "url": url, "segments": text_segments(text)}
            if looks_like_remarks(text):
                return {"kind": "sec_remarks", "url": url, "segments": text_segments(text)}
    return None


def _all_exhibits(idx):
    items = ((idx or {}).get("directory") or {}).get("item") or []
    names = [i.get("name", "") for i in items if i.get("name", "").lower().endswith((".htm", ".html", ".txt"))]
    return sorted(n for n in names if re.search(r"ex[-_]?99|exhibit[-_]?99|transcript", n, re.I))[:5]


def page_media(page_html, page_url):
    """Static scan of a page: transcripts, media files, YouTube/Vimeo. [(kind, url)], best first."""
    found = []
    for href, label in TRANSCRIPT_LINK.findall(page_html or ""):
        if re.search(r"transcript", href + " " + label, re.I):
            found.append(("transcript_link", _abs(href, page_url)))
    for m in MEDIA_FILE.findall(page_html or ""):
        found.append(("file", m.replace("\\/", "/")))
    for vid in YOUTUBE.findall(page_html or ""):
        found.append(("youtube", f"https://www.youtube.com/watch?v={vid}"))
    for a, b in VIMEO.findall(page_html or ""):
        found.append(("vimeo", f"https://vimeo.com/{a or b}"))
    seen, out = set(), []
    for k, u in found:
        if u not in seen:
            seen.add(u)
            out.append((k, u))
    return out


def _abs(href, base):
    from urllib.parse import urljoin
    return urljoin(base, htmlmod.unescape(href))


# ============================================================ the company's IR site
ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
HREF = re.compile(r"href\s*=\s*[\"']([^\"']+)[\"']", re.I)
ATTR_TEXT = re.compile(r"(?:title|aria-label)\s*=\s*[\"']([^\"']+)[\"']", re.I)


def base_domain(url):
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def date_variants(date, spread=(-1, 0, 1, 2)):
    """How a call date can be printed on an IR page ("Aug 4, 2026", "August 04, 2026", "8/4/2026"...), lowercased."""
    d0, out = dt.date.fromisoformat(date), set()
    for k in spread:
        d = d0 + dt.timedelta(days=k)
        full = MONTHS[d.month - 1]
        names = {full, full[:3], full[:3] + "."} | ({"sept", "sept."} if d.month == 9 else set())
        suf = "th" if 11 <= d.day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d.day % 10, "th")
        for mo in names:
            for day in (str(d.day), f"{d.day:02d}", f"{d.day}{suf}"):
                out.add(f"{mo} {day}, {d.year}")
                out.add(f"{mo} {day} {d.year}")
        out |= {f"{d.month}/{d.day}/{d.year}", f"{d.month:02d}/{d.day:02d}/{d.year}", d.isoformat()}
    return out


def _norm(t):
    return re.sub(r"\s+", " ", t or "").strip().lower()


def anchors(page_html, page_url):
    """[{url, blob, near_date}] for every link; near_date = the last date printed before the link (lowercased)."""
    out, h = [], page_html or ""
    for m in ANCHOR.finditer(h):
        hm = HREF.search(m.group(1))
        if not hm or hm.group(1).startswith(("mailto:", "tel:", "javascript:")):
            continue
        before = _norm(eightk.to_text(h[max(0, m.start() - 2500): m.start()]))
        dates = DATE_ANY.findall(before)
        label = _norm(eightk.to_text(m.group(2)) + " " + " ".join(ATTR_TEXT.findall(m.group(1))))
        out.append({"url": _abs(hm.group(1), page_url), "blob": label + " " + hm.group(1).lower(),
                    "near_date": dates[-1] if dates else ""})
    return out


def transcript_host_ok(url, page_url, company_domain=""):
    if BLOCKED_TRANSCRIPT.search(url):
        return False
    b = base_domain(url)
    return b in (base_domain(page_url), base_domain("https://" + company_domain) if company_domain else "") or bool(IR_CDN.search(url))


def ir_roots(domain):
    d = (domain or "").lower().replace("https://", "").replace("http://", "").strip("/").split("/")[0]
    d = d[4:] if d.startswith("www.") else d
    if not d:
        return []
    return [f"https://ir.{d}", f"https://investors.{d}", f"https://investor.{d}", f"https://www.{d}/investors",
            f"https://www.{d}/investor-relations"]


def ir_sources(http, start_urls, date, company_domain="", render=None, max_pages=8, roots=False):
    """Walk the company's IR site (events list -> the event page of this call) and collect what that page offers:
    transcript / prepared-remarks links, webcast pages, media files. Only items printed next to this call's date
    (or found on the event page itself) are taken, so another quarter's transcript is never used.
    Returns ([(kind, url)], pages_read)."""
    variants = date_variants(date)
    queue, seen, found, pages = [(u, 0) for u in start_urls], set(), [], 0
    tried_roots = 0
    while queue and pages < max_pages:
        u, depth = queue.pop(0)
        key = u.split("#")[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        if roots and depth == 0 and tried_roots:
            continue                      # guessed IR addresses: stop at the first one that answers
        page = http.get(u, sec=False, tries=1, timeout=25) or ""
        if roots and depth == 0:
            if not page:
                continue
            tried_roots += 1
        pages += 1
        links = anchors(page, u)
        useful = [a for a in links if (EVENT_HREF.search(a["url"]) and not NOT_EVENT.search(a["url"]))
                  or TRANSCRIPT_WORD.search(a["blob"]) or WEBCAST_HOST.search(a["url"])]
        if render and len(useful) < 2:  # events lists are often drawn by JavaScript
            r = render(u)
            if r:
                page, links = r, anchors(r, u)
        page_dated = any(v in _norm(eightk.to_text(page)) for v in variants)
        for a in links:
            nd = a["near_date"]
            ok = (nd in variants) if nd else (page_dated and depth > 0)
            if not ok:
                continue
            if TRANSCRIPT_WORD.search(a["blob"]) and transcript_host_ok(a["url"], u, company_domain):
                found.append(("transcript_link", a["url"]))
            elif YOUTUBE.search(a["url"]):
                found.append(("youtube", "https://www.youtube.com/watch?v=" + YOUTUBE.search(a["url"]).group(1)))
            elif MEDIA_FILE.search(a["url"]):
                found.append(("file", a["url"]))
            elif WEBCAST_HOST.search(a["url"]):
                found.append(("page", a["url"]))
        if page_dated and depth > 0 and not NOT_EVENT.search(u):
            found += [(k, x) for k, x in page_media(page, u) if k in ("file", "youtube", "vimeo")]
            found.append(("page", u))     # the event page itself often holds the replay player
        if depth < 2:
            nxt = [a for a in links if EVENT_HREF.search(a["url"]) and not NOT_EVENT.search(a["url"]) and base_domain(a["url"]) == base_domain(u)
                   and a["url"].split("#")[0].rstrip("/") not in seen]
            nxt = [a for a in nxt if a["near_date"] in variants or (not a["near_date"] and EARN_WORD.search(a["blob"]))
                   or (depth == 0 and re.search(r"events?(?:\b|[-_/])", a["blob"]) and not a["near_date"])]
            nxt.sort(key=lambda a: 0 if a["near_date"] in variants else 1)
            queue += [(a["url"], depth + 1) for a in nxt[: (4 if depth == 0 else 2)]]
    seen_f, out = set(), []
    for k, x in found:
        if x not in seen_f:
            seen_f.add(x)
            out.append((k, x))
    out.sort(key=lambda kx: 0 if kx[0] == "transcript_link" else 1)
    return out, pages


def render_page(url, wait_s=6):
    """The page as the browser draws it (for IR sites that load their events with JavaScript). HTML or ''."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ""
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            pg = b.new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=40000)
            pg.wait_for_timeout(wait_s * 1000)
            html = pg.content()
            b.close()
            return html
    except Exception:
        return ""


def browser_media(page_url, wait_s=25):
    """Open the webcast page in headless Chromium and record the media requests its player makes.
    Returns {"media": [urls], "registration": bool}. Never fills or submits forms."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"media": [], "registration": False, "error": "playwright not installed"}
    media, reg = [], False
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=["--autoplay-policy=no-user-gesture-required"])
            pg = b.new_page()
            pg.on("request", lambda r: media.append(r.url) if re.search(r"\.(m3u8|mp3|m4a|mp4|mpd|aac)(\?|$)", r.url, re.I) else None)
            pg.goto(page_url, wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(4000)
            body = pg.content()
            reg = bool(REGISTRATION.search(body))
            if not reg:
                for sel in ["button[aria-label*=play i]", ".vjs-big-play-button", "button:has-text('Play')",
                            "[class*=play-button]", "video", "audio"]:
                    try:
                        el = pg.locator(sel).first
                        if el.count():
                            el.click(timeout=2000)
                            break
                    except Exception:
                        pass
                pg.wait_for_timeout(wait_s * 1000)
            b.close()
    except Exception as e:  # a broken page is just a miss
        return {"media": list(dict.fromkeys(media)), "registration": reg, "error": str(e)[:120]}
    return {"media": list(dict.fromkeys(media)), "registration": reg}


def fetch_text_doc(http, url):
    """A transcript page or PDF as text (PDFs are spotted by their content: IR links often have no .pdf ending)."""
    data = http.get(url, want="bytes", sec=False)
    if not data:
        return ""
    if data[:5] == b"%PDF-":
        try:
            from pdfminer.high_level import extract_text
            return extract_text(io.BytesIO(data))
        except Exception:
            return ""
    return to_paras(data.decode("utf-8", errors="ignore"))


def captions(url, outdir):
    """YouTube captions (manual first, then automatic) as [(seconds, text)], or [] if none / blocked."""
    os.makedirs(outdir, exist_ok=True)
    tmpl = os.path.join(outdir, "cap.%(ext)s")
    for f in os.listdir(outdir):
        if f.startswith("cap."):
            os.remove(os.path.join(outdir, f))
    cmd = ["yt-dlp", "--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", "en.*,en",
           "--sub-format", "vtt", "-q", "--no-warnings", "-o", tmpl, url]
    LAST_ERR["msg"] = ""
    try:
        _err(subprocess.run(cmd, capture_output=True, text=True, timeout=180))
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        LAST_ERR["msg"] = type(e).__name__
        return []
    vtts = [f for f in os.listdir(outdir) if f.startswith("cap.") and f.endswith(".vtt")]
    if not vtts:
        return []
    return parse_vtt(open(os.path.join(outdir, vtts[0]), encoding="utf-8", errors="ignore").read())


def parse_vtt(vtt):
    """WebVTT to [(seconds, text)]; drops the rolling duplicates of auto captions."""
    out, last = [], ""
    for block in re.split(r"\n\s*\n", vtt):
        m = re.search(r"(\d+):(\d\d):(\d\d)\.\d+\s+-->", block) or re.search(r"(\d\d):(\d\d)\.\d+\s+-->", block)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        sec = g[0] * 3600 + g[1] * 60 + g[2] if len(g) == 3 else g[0] * 60 + g[1]
        lines = [re.sub(r"<[^>]+>", "", l).strip() for l in block.split("\n")[1:] if "-->" not in l]
        text = " ".join(l for l in lines if l)
        if not text or text == last:
            continue
        if last and text.startswith(last):
            text = text[len(last):].strip()
        last = " ".join(l for l in lines if l)
        if text:
            out.append((sec, text))
    return merge_short(out)


def merge_short(segs, target=200):
    """Glue caption fragments into sentence-sized segments."""
    out, buf, start = [], "", None
    for sec, t in segs:
        if start is None:
            start = sec
        buf = (buf + " " + t).strip()
        if len(buf) >= target and re.search(r"[.?!]$", buf):
            out.append((start, buf))
            buf, start = "", None
    if buf:
        out.append((start or 0, buf))
    return out


def fetch_audio(url, outdir, max_minutes=150):
    """Audio only via yt-dlp (handles YouTube, Vimeo, direct files and HLS streams). Path or None."""
    os.makedirs(outdir, exist_ok=True)
    tmpl = os.path.join(outdir, "%(id)s.%(ext)s")
    cmd = ["yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "9", "--no-playlist", "-q", "--no-warnings",
           "--match-filter", f"!duration | duration<{max_minutes * 60}", "-o", tmpl, "--print", "after_move:filepath", url]
    LAST_ERR["msg"] = ""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        _err(r)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        LAST_ERR["msg"] = type(e).__name__
        return None
    paths = [p for p in r.stdout.strip().splitlines() if p and os.path.exists(p)]
    return paths[-1] if paths else None


def youtube_search(company, date):
    """Last resort: search YouTube for '<company> earnings call <month year>'. URL or None.
    Accepts only a title that names the company and says earnings/results call."""
    core = re.sub(r",?\s+(inc\.?|corp\.?|corporation|company|co\.|holdings?|ltd\.?|plc|group|n\.?a\.?)$", "", company, flags=re.I).strip()
    q = f"ytsearch5:{core} earnings call {date[:4]}"
    try:
        r = subprocess.run(["yt-dlp", "--flat-playlist", "--print", "%(id)s\t%(title)s\t%(duration)s", "-q", "--no-warnings", q],
                           capture_output=True, text=True, timeout=90)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    key = core.lower().split()[0] if core else ""
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        vid, title, dur = parts
        try:
            dur = float(dur)
        except ValueError:
            dur = 0
        t = title.lower()
        if key and key in t and re.search(r"earnings|results|conference call", t) and 15 * 60 <= dur <= 150 * 60:
            return f"https://www.youtube.com/watch?v={vid}"
    return None


LAST_ERR = {"msg": ""}
YT = {"blocked": False}   # YouTube asked us to prove we are not a bot: respect it, stop asking for the rest of the run


def _err(r):
    """First ERROR line yt-dlp printed (kept so the report says why a video failed)."""
    lines = [l for l in (getattr(r, "stderr", "") or "").splitlines() if "ERROR" in l]
    LAST_ERR["msg"] = re.sub(r"\s+", " ", lines[0].split("ERROR:", 1)[-1])[:110].strip() if lines else ""
    if re.search(r"confirm you.?re not a bot|sign in to confirm", LAST_ERR["msg"], re.I):
        YT["blocked"] = True


# ============================================================ speech to text
_MODEL = {}


def _model(name):
    from faster_whisper import WhisperModel
    if name not in _MODEL:
        try:
            import ctranslate2
            gpu = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            gpu = False
        _MODEL[name] = WhisperModel(name, device="cuda" if gpu else "cpu", compute_type="float16" if gpu else "int8")
    return _MODEL[name]


def transcribe(path, model_name="base.en"):
    """[(seconds, text)] with faster-whisper (VAD on, greedy decoding: fast on CPU)."""
    segs, _ = _model(model_name).transcribe(path, vad_filter=True, beam_size=1, language="en")
    return merge_short([(int(s.start), s.text.strip()) for s in segs if s.text.strip()], target=160)


def retranscribe(path, start, end, model_name="small.en"):
    """Accurate re-pass of one window (used only for the quotes we keep)."""
    try:
        segs, _ = _model(model_name).transcribe(path, beam_size=5, language="en",
                                                clip_timestamps=[max(0, start - 2), end + 2])
        return " ".join(s.text.strip() for s in segs).strip()
    except Exception:
        return ""


# ============================================================ text transcripts
NAME = r"(?:[A-Z][a-zA-Z'’\-]*\.?\s){1,3}[A-Z][a-zA-Z'’\-]+"
LABEL = re.compile(r"^(Operator|" + NAME + r")\s*(?:(?:--|—|–|-|,|:|\|)\s*(.{2,90}))?$")
INLINE = re.compile(r"^(Operator|" + NAME + r")(?:\s*(?:--|—|–|-|,)\s*([^:]{2,80}))?\s*:\s+(.{20,})$", re.S)
NOT_A_NAME = re.compile(r"\b(call|conference|participants?|presenters?|corporate|executives?|analysts?|earnings|quarter|results|"
                        r"transcript|inc|corp|company|page|copyright|disclaimer|forward|statements?|session|remarks|questions?|answers?|"
                        r"thank|thanks|good|morning|afternoon|evening|welcome|hello)\b", re.I)
OFFICER = re.compile(r"officer|president|\bvp\b|vice president|investor relations|treasurer|controller|head of|director|chair|"
                     r"founder|\bcto\b|\bcoo\b|\bcao\b|general counsel|secretary", re.I)
BROKER = re.compile(r"analyst|research|securities|capital|partners|bank|llc|& co|markets|barclays|goldman|morgan|ubs|jefferies|citi|"
                    r"wells fargo|evercore|rbc|truist|bofa|merrill|bernstein|keybanc|raymond james|stifel|baird|cowen|oppenheimer|"
                    r"wolfe|piper|needham|william blair|mizuho|deutsche|credit suisse|btig|loop|roth|craig-hallum|benchmark|"
                    r"ladenburg|lake street|northland|riley|wainwright|maxim|canaccord|cantor|guggenheim|bmo|cibc|scotia|"
                    r"td cowen|seaport|wedbush|janney|d\.a\. davidson|stephens|nomura|hsbc|macquarie|zelman|sidoti", re.I)


def _label(line):
    """(name, title) if this line is a speaker label like 'Jesse Singh', 'Operator' or 'Jane Smith -- CFO'."""
    line = line.strip()
    if not line or len(line) > 120 or (line[-1] in ".?!" and line.lower() != "operator"):
        return None
    m = LABEL.match(line)
    if not m:
        return None
    name, title = m.group(1).strip(), (m.group(2) or "").strip()
    if name != "Operator" and NOT_A_NAME.search(name):
        return None
    return name, title


def text_segments(text):
    """Filed / posted transcripts or remarks: paragraphs with the speaker of each.
    Handles labels on their own line ('Jesse Singh'), with a title ('Jane Smith -- CFO') and inline ('Operator: ...'),
    and reads the roster at the top so name-only labels still get a title. Text before the first turn is 'Header'."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    if len(paras) < 5:
        paras = [p.strip() for p in re.split(r"\n|(?<=[.?!])\s{2,}", text or "") if p.strip()]
    roster, out, speaker, pending, started = {}, [], "Header", None, False

    def full(name, title):
        t = title or roster.get(name.lower(), "")
        return name + (" — " + t if t else "")

    for p in paras:
        lines = [l.strip() for l in p.split("\n") if l.strip()]
        lab = _label(lines[0])
        if lab:
            if pending and pending[1] and not started:
                roster[pending[0].lower()] = pending[1]   # a roster line ("Name, Title" then another name)
            if len(lines) == 1:
                pending = lab
                continue
            speaker, pending, started = full(*lab), None, True
            lines = lines[1:]
        else:
            m = INLINE.match(" ".join(lines))
            if m and not NOT_A_NAME.search(m.group(1) if m.group(1) != "Operator" else ""):
                speaker, pending, started = full(m.group(1), (m.group(2) or "").strip()), None, True
                out.append((None, m.group(3).strip(), speaker))
                continue
            if pending:
                speaker, pending, started = full(*pending), None, True
        out.append((None, " ".join(lines), speaker if started else "Header"))
    return out


# ============================================================ who is talking
def attribute(segments, cfo_name="", ceo_name=""):
    """Add a speaker role to each (seconds, text[, label]) segment:
    cfo / ceo / management / analyst / operator. In Q&A, a name-only label we did not hear
    in the prepared remarks is an analyst."""
    cfo_keys = {x.lower() for x in (cfo_name or "").split() if len(x) > 2}
    ceo_keys = {x.lower() for x in (ceo_name or "").split() if len(x) > 2}
    out, role, qa, asking = [], "management", False, False
    before_qa = set()
    for seg in segments:
        sec, text = seg[0], seg[1]
        label = seg[2] if len(seg) > 2 else ""
        if label:
            ll = label.lower()
            name = ll.split(" — ")[0]
            words = set(re.findall(r"[a-z']+", name))
            if "operator" in ll or ll == "header":
                role = "operator"
            elif ROLE_NEAR.search(ll) or (cfo_keys and len(cfo_keys & words) >= min(2, len(cfo_keys))):
                role = "cfo"
            elif CEO_NEAR.search(ll) or (ceo_keys and len(ceo_keys & words) >= min(2, len(ceo_keys))):
                role = "ceo"
            elif BROKER.search(ll):
                role = "analyst"
            elif OFFICER.search(ll):
                role = "management"
            elif qa and name not in before_qa:
                role = "analyst"
            else:
                role = "management"
            if not qa and role not in ("operator", "analyst"):
                before_qa.add(name)
            out.append((sec, text, role, label))
            if NEXT_QUESTION.search(text) or (QA_START.search(text) and not QA_FUTURE.search(text)):
                qa = True
            continue
        if NEXT_QUESTION.search(text) or (QA_START.search(text) and not QA_FUTURE.search(text)):
            qa = True
        if NEXT_QUESTION.search(text):
            out.append((sec, text, "operator", ""))
            asking, role = True, "analyst"
            continue
        if asking:
            out.append((sec, text, "analyst", ""))
            if ANALYST_TELL.search(text):
                asking, role = False, "management"
            continue
        h = HANDOFF.search(text)
        out.append((sec, text, role, ""))
        if h and not qa:
            who = h.group(1).lower().split()
            tail = text[h.start():h.start() + 160]
            if (cfo_keys and cfo_keys & set(who)) or ROLE_NEAR.search(tail):
                role = "cfo"
            elif (ceo_keys and ceo_keys & set(who)) or CEO_NEAR.search(tail):
                role = "ceo"
            else:
                role = "management"
    return out


# ============================================================ reading the call
def analyze(attributed, max_per_type=2):
    """Quotes of finance pain from management. [{type, quote, at, part, speaker}]"""
    qa_at = None
    for i, (sec, text, *_r) in enumerate(attributed):
        if NEXT_QUESTION.search(text) or (QA_START.search(text) and not QA_FUTURE.search(text)):
            qa_at = i
            break
    hits, counts, topics = [], {}, {}
    for i, (sec, text, role, label) in enumerate(attributed):
        if role in ("analyst", "operator"):
            continue
        for s in re.split(r"(?<=[.;!?])\s+", text):
            s = re.sub(r"^\s*(yeah|yes|sure|okay|ok|so|well|great|thanks|thank you)[,.!]?\s+", "", s, flags=re.I)
            s = s[:1].upper() + s[1:]
            if len(s) < 60 or len(s) > 500 or earnings.BOILERPLATE.search(s) or s.strip().endswith("?"):
                continue
            for typ, rx in CALL_PATTERNS.items():
                m = rx.search(s)
                if not m or earnings.NEGATION.search(s[: m.start()]):
                    continue
                topics[typ] = topics.get(typ, 0) + 1
                if counts.get(typ, 0) >= max_per_type:
                    continue
                counts[typ] = counts.get(typ, 0) + 1
                hits.append({"type": typ, "quote": s.strip()[:360], "at": sec,
                             "part": "Q&A" if qa_at is not None and i >= qa_at else "prepared remarks",
                             "speaker": role, "label": label})
    return hits, topics


def cfo_excerpt(attributed, limit=1500):
    """The opening of the CFO's prepared remarks (useful context for writing)."""
    parts = [t for _s, t, r, _l in attributed if r == "cfo"]
    return " ".join(parts)[:limit]


def mmss(sec):
    if sec is None:
        return ""
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def at_url(url, sec):
    if sec is None or not url:
        return url
    if "youtube.com/watch" in url:
        return f"{url}&t={int(sec)}s"
    return url


# ============================================================ open transcript dataset
HF_URL = ("https://huggingface.co/datasets/defeatbeta/yahoo-finance-data/resolve/main/data/US/"
          "stock_earning_call_transcripts.parquet")
_HF = {"con": None, "fails": 0, "err": ""}


def hf_calls(tickers, since):
    """{report_date: {symbol, fy, q, paras:[(speaker, text)]}} for a company's tickers since a date.
    Reads only the rows it needs from the dataset (DuckDB range requests). {} on any problem."""
    syms = []
    for t in tickers or []:
        t = (t or "").upper().strip()
        if re.fullmatch(r"[A-Z][A-Z.\-]{0,9}", t):
            syms += [t, t.replace(".", "-"), t.replace("-", ".")]
    syms = list(dict.fromkeys(syms))
    if not syms or _HF["fails"] >= 3:
        return {}
    try:
        import duckdb
        if _HF["con"] is None:
            con = duckdb.connect()
            if HF_URL.startswith("http"):
                con.execute("INSTALL httpfs; LOAD httpfs;")
            _HF["con"] = con
        rows = _HF["con"].execute(
            f"SELECT symbol, CAST(report_date AS VARCHAR), fiscal_year, fiscal_quarter, transcripts FROM read_parquet('{HF_URL}') "
            f"WHERE symbol IN ({','.join('?' * len(syms))}) AND TRY_CAST(report_date AS DATE) >= CAST(? AS DATE)",
            syms + [since]).fetchall()
    except Exception as e:  # dataset moved, network blip, no duckdb: the other sources still run
        _HF["fails"] += 1
        _HF["err"] = str(e)[:150]
        return {}
    out = {}
    for sym, rd, fy, fq, tr in rows:
        paras = sorted(tr or [], key=lambda p: (p or {}).get("paragraph_number") or 0)
        out[(rd or "")[:10]] = {"symbol": sym, "fy": fy, "q": fq,
                                "paras": [((p or {}).get("speaker") or "", (p or {}).get("content") or "") for p in paras]}
    return out


def hf_match(calls_by_date, date, before=3, after=5):
    """The dataset call that belongs to an earnings 8-K filed on `date` (call is usually the same day or the next)."""
    d0, best = dt.date.fromisoformat(date), None
    for rd, c in (calls_by_date or {}).items():
        try:
            diff = (dt.date.fromisoformat(rd) - d0).days
        except ValueError:
            continue
        if -before <= diff <= after and (best is None or abs(diff) < abs(best[0])):
            best = (diff, dict(c, report_date=rd))
    return best[1] if best else None


def inbox_match(files, date, before=3, after=5):
    """A transcript you dropped in state/transcripts_in for this call: [(date, path)] -> path or None."""
    d0 = dt.date.fromisoformat(date)
    for d, path in sorted(files or [], key=lambda x: abs((dt.date.fromisoformat(x[0]) - d0).days)):
        if -before <= (dt.date.fromisoformat(d) - d0).days <= after:
            return path
    return None


def filing_index(cik, adsh):
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh.replace('-', '')}/"


# ============================================================ one event, start to finish
def process_event(http, ev, outdir, cfo_name="", ceo_name="", model="base.en", fix_model="small.en",
                  use_browser=True, use_youtube_search=True, deps=None):
    """ev = {cik, company, adsh, date, followups:[adsh], domain}. Returns a record that says how far it got."""
    d = deps or {}
    browser = d.get("browser", browser_media)
    render = d.get("render", render_page) if use_browser else None
    getcap = d.get("captions", captions)
    getaudio = d.get("audio", fetch_audio)
    stt = d.get("stt", transcribe)
    fix = d.get("fix", retranscribe)
    ytsearch = d.get("ytsearch", youtube_search)
    rec = {"cik": ev["cik"], "adsh": ev["adsh"], "date": ev["date"], "v": VERSION, "stage": "start", "hits": [], "tried": []}
    segs, src, audio_path = None, None, None
    variants = date_variants(ev["date"])

    # 0. a transcript you supplied, or the open transcript dataset
    if ev.get("inbox_file"):
        try:
            segs = text_segments(open(ev["inbox_file"], encoding="utf-8", errors="ignore").read()) or None
            src = {"kind": "own_transcript", "url": filing_index(ev["cik"], ev["adsh"])} if segs else None
        except OSError:
            segs = None
    if not segs and ev.get("hf"):
        segs = [(None, text, speaker or "") for speaker, text in ev["hf"]["paras"] if text and text.strip()] or None
        if segs:
            src = {"kind": "hf_transcript", "url": filing_index(ev["cik"], ev["adsh"])}
            rec["hf"] = {"symbol": ev["hf"]["symbol"], "fy": ev["hf"]["fy"], "q": ev["hf"]["q"], "report_date": ev["hf"]["report_date"]}

    # 1. transcript or prepared remarks filed with the SEC
    t = sec_transcript(http, ev["cik"], [ev["adsh"]] + ev.get("followups", [])) if not segs else None
    if t:
        segs, src = t["segments"], {"kind": t["kind"], "url": t["url"]}
    release = exhibit_docs(http, ev["cik"], ev["adsh"]) if not segs else []
    links = release_links(" ".join(r for _n, r in release)) if not segs else []
    rec["links"] = links[:6]
    candidates, ir_start = [], []
    if not segs:
        for u in links:
            if YOUTUBE.search(u):
                candidates.append(("youtube", u))
            elif WEBCAST_HOST.search(u) or MEDIA_FILE.search(u):
                html = http.get(u, sec=False, tries=1) or ""
                if html and REGISTRATION.search(html) and not page_media(html, u):
                    rec["tried"].append(f"registration:{u}")
                    rec["registration"] = True
                    candidates.append(("page_registration", u))
                    continue
                candidates += page_media(html, u)
                candidates.append(("page", u))
            else:
                ir_start.append(u)        # the company's IR / events page
        # 2. the company's IR site: the event page of this call (transcript, remarks, replay)
        found, pages = ir_sources(http, ir_start, ev["date"], ev.get("domain", ""), render=render) if ir_start else ([], 0)
        # no luck: guess the IR site, from the press release's own web address first (the watchlist website can be
        # a different company with a similar name), else from the watchlist website
        guess = base_domain(ir_start[0]) if ir_start else ev.get("domain", "")
        if not found and guess:
            f2, p2 = ir_sources(http, ir_roots(guess), ev["date"], guess, render=render, roots=True)
            found, pages = found + f2, pages + p2
        rec["ir"] = {"pages": pages, "found": len(found)}
        # a release link that is itself the event page (".../events/q3-call") can hold the player too
        candidates = found + candidates + [("page", u) for u in ir_start if re.search(r"event|webcast|call|replay", u, re.I)
                                           and not NOT_EVENT.search(u) and ("page", u) not in found]
    for kind, u in [c for c in candidates if c[0] == "transcript_link"][:3]:
        text = fetch_text_doc(http, u)
        head = _norm(text[:5000])
        right_call = any(v in head for v in variants) or not DATE_ANY.search(head)
        rec["tried"].append(f"transcript_link:{u}:{len(text)}{'' if right_call else ':other_date'}")
        if len(text) > 8000 and right_call:
            segs = text_segments(text)
            src = {"kind": "ir_remarks" if looks_like_remarks(text) and not looks_like_transcript(text) else "ir_transcript", "url": u}
            break
    # 3. captions on YouTube
    if not segs:
        yts = [] if YT["blocked"] else [u for k, u in candidates if k == "youtube"]
        if not yts and use_youtube_search and not YT["blocked"]:
            found_yt = ytsearch(ev.get("company", ""), ev["date"])
            if found_yt:
                yts = [found_yt]
                candidates.append(("youtube_search", found_yt))
        for u in yts[:2]:
            c = getcap(u, outdir)
            rec["tried"].append(f"captions:{u}" + (f" [{LAST_ERR['msg']}]" if not c and LAST_ERR.get("msg") else ""))
            if c:
                segs, src = c, {"kind": "captions", "url": u}
                break
    # 4. audio (direct file, player stream found in the browser, YouTube/Vimeo)
    if not segs:
        queue = [(k, u) for k, u in candidates if k in ("file", "vimeo") or (k in ("youtube", "youtube_search") and not YT["blocked"])]
        page_of = {}
        if use_browser:
            for k, u in [c for c in candidates if c[0] == "page"][:2]:
                bm = browser(u)
                rec["tried"].append(f"browser:{u}:{len(bm.get('media', []))}{':reg' if bm.get('registration') else ''}")
                if bm.get("registration"):
                    rec["registration"] = True
                queue += [("stream", m) for m in bm.get("media", [])[:3]]
                for m in bm.get("media", [])[:3]:
                    page_of[m] = u
        queue += [(k, u) for k, u in candidates if k == "page"]  # yt-dlp's generic extractor, last
        seen = set()
        for k, u in queue:
            if u in seen or (YT["blocked"] and YOUTUBE.search(u)):
                continue
            seen.add(u)
            audio_path = getaudio(u, outdir)
            rec["tried"].append(f"audio:{k}:{u[:120]}" + (f" [{LAST_ERR['msg']}]" if not audio_path and LAST_ERR.get("msg") else ""))
            if audio_path:
                src = {"kind": f"audio_{k}", "url": page_of.get(u) or u}
                break
        if audio_path:
            segs = stt(audio_path, model)
    if not segs:
        if not links and not candidates:
            rec["stage"] = "no_call_link_found"
        elif rec.get("registration") and not audio_path:
            rec["stage"] = "registration_required"
        elif audio_path:
            rec["stage"] = "transcription_empty"
        else:
            rec["stage"] = "no_usable_source"
        _cleanup(audio_path)
        return rec, None

    attributed = attribute(segs, cfo_name, ceo_name)
    hits, topics = analyze(attributed)
    # accurate re-pass of the kept quotes (audio only)
    if audio_path and fix:
        for h in hits:
            if h["at"] is None:
                continue
            better = fix(audio_path, h["at"], h["at"] + 45, fix_model)
            key = re.sub(r"\W+", " ", h["quote"].lower()).split()[:4]
            if better and key and " ".join(key[:2]) in re.sub(r"\W+", " ", better.lower()):
                # keep the sentence of the re-pass that carries the same pattern
                for s in re.split(r"(?<=[.;!?])\s+", better):
                    if CALL_PATTERNS[h["type"]].search(s):
                        h["quote"], h["verified"] = s.strip()[:360], True
                        break
    for h in hits:
        h.setdefault("verified", src["kind"] in ("sec_transcript", "sec_remarks", "ir_transcript", "ir_remarks", "hf_transcript", "own_transcript"))
        if src["kind"].endswith("remarks"):
            h["part"] = "prepared remarks"
    _cleanup(audio_path)
    rec.update(stage="done", source=src, hits=hits, topics=topics, cfo_excerpt=cfo_excerpt(attributed),
               minutes=round((segs[-1][0] or 0) / 60) if segs and segs[-1][0] is not None else None,
               words=sum(len(s[1].split()) for s in segs))
    lines = []
    for sec, text, role, label in attributed:
        lines.append(f"[{mmss(sec)}] ({label or role}) {text}" if sec is not None else f"({label or role}) {text}")
    return rec, "\n".join(lines)


def _cleanup(path):
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


# ============================================================ into the engine
LABEL_DATE = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun", 7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}
SPEAKER_WORD = {"cfo": "the CFO", "ceo": "the CEO", "management": "management"}


def signals_for_company(doc, today, window_days=150):
    """Engine signals from one company's call file: one per topic, from the newest call that raised it,
    with how many of the last calls raised it. Only calls inside the window count."""
    import datetime as dt
    events = sorted((r for r in doc.get("events", {}).values() if r.get("stage") == "done"), key=lambda r: r["date"], reverse=True)
    if not events:
        return []
    cutoff = (today - dt.timedelta(days=window_days)).isoformat()
    out, used = [], set()
    for r in events:
        if r["date"] < cutoff:
            break
        for h in r.get("hits", []):
            if h["type"] in used:
                continue
            used.add(h["type"])
            n_calls = sum(1 for e in events if h["type"] in (e.get("topics") or {}))
            y, m, d_ = r["date"][:4], int(r["date"][5:7]), int(r["date"][8:10])
            who = SPEAKER_WORD.get(h.get("speaker"), "management")
            if h.get("speaker") == "cfo" and doc.get("cfo_name"):
                who = f"the CFO, {doc['cfo_name']}"
            when = f"{LABEL_DATE[m]} {d_}, {y}"
            where = f"{h['part']}" + (f", {mmss(h['at'])}" if h.get("at") is not None else "")
            exact = "" if h.get("verified") else " [machine transcript: paraphrase, don't quote word for word]"
            repeat = f" Raised on {n_calls} of the {len(events)} recent calls we have transcripts for." if n_calls > 1 else ""
            q = h["quote"].rstrip(" .;")
            src_kind = (r.get("source") or {}).get("kind", "")
            if src_kind.endswith("remarks"):
                detail = f"In its prepared remarks for the {when} earnings call, {who} said: \"{q}.\"{repeat}{exact}"
            else:
                detail = f"On its {when} earnings call ({where}), {who} said: \"{q}.\"{repeat}{exact}"
            src = r.get("source") or {}
            out.append({"type": h["type"], "cik": doc["cik"], "company": doc.get("company", ""), "date": r["date"],
                        "detail": detail, "url": at_url(src.get("url", ""), h.get("at")), "ref": "state",
                        "source": "Earnings call"})
    return out


def save_transcript(folder, cik, date, text, adsh=""):
    os.makedirs(os.path.join(folder, cik), exist_ok=True)
    path = os.path.join(folder, cik, f"{date}{'_' + adsh[-6:] if adsh else ''}.txt.gz")
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(text)
    return path


def prune(doc, folder, keep=4):
    """Rolling window: keep the newest `keep` calls per company, drop older ones and their transcripts."""
    evs = sorted(doc.get("events", {}).items(), key=lambda kv: kv[1].get("date", ""), reverse=True)
    for adsh, r in evs[keep:]:
        p = r.get("transcript_file")
        if p and os.path.exists(p):
            os.remove(p)
        del doc["events"][adsh]
    return doc


def load_doc(path, cik, company):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return {"cik": cik, "company": company, "events": {}}
