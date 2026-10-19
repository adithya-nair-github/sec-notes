"""Earnings-call transcripts, built in-house (v2).

For every earnings 8-K (Item 2.02) of a watchlist company we try to get what management
actually said on the call, cheapest source first:

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
import gzip
import html as htmlmod
import io
import json
import os
import re
import subprocess

from . import earnings, eightk

VERSION = 2

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
        r"(headcount|workforce) (reduction|freeze)|hiring freeze|reduction in force|"
        r"(cost|expense|g&a|sg&a|opex)[a-z\s\-]{0,25}(reduction|savings|program|initiative|discipline|containment|take ?out)|"
        r"restructuring|streamlin\w+|lean(er)? (team|organization)|do more with less|efficiency (program|initiative)", re.I),
    "call_mna_integration": re.compile(
        r"integrat\w+ (of )?(the |our )?(acquisition|acquired|business)|purchase (price )?accounting|"
        r"(carve[- ]out|spin[- ]?off) (financials|reporting|readiness)|stand[- ]?alone (public )?company|"
        r"(closed|completed) (the|our) acquisition", re.I),
    "call_leadership": re.compile(
        r"(new|incoming|interim|former) (chief financial officer|cfo|controller|chief accounting officer)|"
        r"welcome (our|to) (new )?(cfo|chief financial officer)|cfo (transition|search)|search for (a )?(new )?cfo", re.I),
}
QA_START = re.compile(r"question[- ]and[- ]answer|q ?& ?a (session|portion)|open (the|up the) (line|lines|floor|call) (for|to) questions|"
                      r"first question (comes|is|will come) from|take (our|the) first question", re.I)
NEXT_QUESTION = re.compile(r"(next|first|following|final|last) question (comes|is|will come) from|"
                           r"question (comes|is) from|your line is (now )?open|go ahead with your question", re.I)
HANDOFF = re.compile(r"(?:turn|hand|pass)(?:ing)? (?:the call |it |things |the line )?(?:over|back)? ?(?:over )?to "
                     r"([A-Z][a-z]+(?: [A-Z][a-z]+)?)", re.S)
ROLE_NEAR = re.compile(r"chief financial officer|\bcfo\b|finance chief", re.I)
CEO_NEAR = re.compile(r"chief executive|\bceo\b|president", re.I)
ANALYST_TELL = re.compile(r"\?\s*$|thanks for taking (my|the) question|congrats on|congratulations on", re.I)


# ============================================================ sources
def release_links(raw_html):
    """Call / webcast links mentioned in the earnings press release (raw HTML keeps hrefs)."""
    seen, out = set(), []
    for u in URL.findall(raw_html or ""):
        u = htmlmod.unescape(u.rstrip(".,;"))
        if CALL_HINT.search(u) and not JUNK_HOSTS.search(u) and u not in seen:
            seen.add(u)
            out.append(u)
    return out[:8]


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


def sec_transcript(http, cik, adshs):
    """A transcript the company filed with the SEC itself (earnings 8-K or a follow-up 8-K)."""
    for adsh in adshs:
        for name, raw in exhibit_docs(http, cik, adsh, names_fn=_all_exhibits):
            text = eightk.to_text(raw)
            if looks_like_transcript(text):
                return {"kind": "sec_transcript", "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh.replace('-', '')}/{name}",
                        "segments": text_segments(text)}
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
    """A transcript page or PDF as text."""
    if url.lower().split("?")[0].endswith(".pdf"):
        data = http.get(url, want="bytes", sec=False)
        if not data:
            return ""
        try:
            from pdfminer.high_level import extract_text
            return extract_text(io.BytesIO(data))
        except Exception:
            return ""
    return eightk.to_text(http.get(url, sec=False) or "")


def captions(url, outdir):
    """YouTube captions (manual first, then automatic) as [(seconds, text)], or [] if none / blocked."""
    os.makedirs(outdir, exist_ok=True)
    tmpl = os.path.join(outdir, "cap.%(ext)s")
    for f in os.listdir(outdir):
        if f.startswith("cap."):
            os.remove(os.path.join(outdir, f))
    cmd = ["yt-dlp", "--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", "en.*,en",
           "--sub-format", "vtt", "-q", "--no-warnings", "-o", tmpl, url]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except (subprocess.TimeoutExpired, FileNotFoundError):
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
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except (subprocess.TimeoutExpired, FileNotFoundError):
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
SPEAKER_LINE = re.compile(r"^\s*([A-Z][A-Za-z.'\- ]{2,40}?)\s*(?:--|—|-|,|:)\s*(.{0,80}?)\s*$")


def text_segments(text):
    """Filed / posted transcripts: split into paragraphs and keep the printed speaker labels."""
    paras = [p.strip() for p in re.split(r"\n\s*\n|(?<=[.?!])\s{2,}", text) if p.strip()]
    out, speaker = [], ""
    for i, p in enumerate(paras):
        if re.fullmatch(r"operator:?", p, re.I):
            speaker = "Operator"
            continue
        m = SPEAKER_LINE.match(p) if len(p) < 130 else None
        if m:
            speaker = (m.group(1) + (" — " + m.group(2) if m.group(2) else "")).strip()
            continue
        out.append((None, p, speaker))
    return out


# ============================================================ who is talking
def attribute(segments, cfo_name="", ceo_name=""):
    """Add a speaker role to each (seconds, text[, label]) segment:
    cfo / ceo / management / analyst / operator."""
    cfo_keys = {x.lower() for x in (cfo_name or "").split() if len(x) > 2}
    ceo_keys = {x.lower() for x in (ceo_name or "").split() if len(x) > 2}
    out, role, qa, asking = [], "management", False, False
    for seg in segments:
        sec, text = seg[0], seg[1]
        label = seg[2] if len(seg) > 2 else ""
        if label:
            ll = label.lower()
            if "operator" in ll:
                role = "operator"
            elif ROLE_NEAR.search(ll) or (cfo_keys and cfo_keys & set(ll.split())):
                role = "cfo"
            elif CEO_NEAR.search(ll) or (ceo_keys and ceo_keys & set(ll.split())):
                role = "ceo"
            elif re.search(r"analyst|research|securities|capital|partners|bank|llc|& co|markets", ll):
                role = "analyst"
            else:
                role = "management"
            out.append((sec, text, role, label))
            continue
        if QA_START.search(text):
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
        if QA_START.search(text):
            qa_at = i
            break
    hits, counts, topics = [], {}, {}
    for i, (sec, text, role, label) in enumerate(attributed):
        if role in ("analyst", "operator"):
            continue
        for s in re.split(r"(?<=[.;!?])\s+", text):
            if len(s) < 40 or len(s) > 500 or earnings.BOILERPLATE.search(s) or s.strip().endswith("?"):
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


# ============================================================ one event, start to finish
def process_event(http, ev, outdir, cfo_name="", ceo_name="", model="base.en", fix_model="small.en",
                  use_browser=True, use_youtube_search=True, deps=None):
    """ev = {cik, company, adsh, date, followups:[adsh]}. Returns a record that says how far it got."""
    d = deps or {}
    browser = d.get("browser", browser_media)
    getcap = d.get("captions", captions)
    getaudio = d.get("audio", fetch_audio)
    stt = d.get("stt", transcribe)
    fix = d.get("fix", retranscribe)
    ytsearch = d.get("ytsearch", youtube_search)
    rec = {"cik": ev["cik"], "adsh": ev["adsh"], "date": ev["date"], "v": VERSION, "stage": "start", "hits": [], "tried": []}
    segs, src, audio_path = None, None, None

    # 1. transcript filed with the SEC
    t = sec_transcript(http, ev["cik"], [ev["adsh"]] + ev.get("followups", []))
    if t:
        segs, src = t["segments"], {"kind": t["kind"], "url": t["url"]}
    release = exhibit_docs(http, ev["cik"], ev["adsh"]) if not segs else []
    links = release_links(" ".join(r for _n, r in release)) if not segs else []
    rec["links"] = links[:6]
    candidates = []
    if not segs:
        for u in links:
            if YOUTUBE.search(u):
                candidates.append(("youtube", u))
                continue
            html = http.get(u, sec=False)
            if html and REGISTRATION.search(html) and not page_media(html, u):
                rec["tried"].append(f"registration:{u}")
                rec["registration"] = True
                candidates.append(("page_registration", u))
                continue
            candidates += page_media(html or "", u)
            candidates.append(("page", u))
    # 2. transcript linked from the IR / webcast page
    for kind, u in [c for c in candidates if c[0] == "transcript_link"][:2]:
        text = fetch_text_doc(http, u)
        rec["tried"].append(f"transcript_link:{u}")
        if len(text) > 8000:
            segs, src = text_segments(text), {"kind": "ir_transcript", "url": u}
            break
    # 3. captions on YouTube
    if not segs:
        yts = [u for k, u in candidates if k == "youtube"]
        if not yts and use_youtube_search:
            found = ytsearch(ev.get("company", ""), ev["date"])
            if found:
                yts = [found]
                candidates.append(("youtube_search", found))
        for u in yts[:2]:
            rec["tried"].append(f"captions:{u}")
            c = getcap(u, outdir)
            if c:
                segs, src = c, {"kind": "captions", "url": u}
                break
    # 4. audio (direct file, player stream found in the browser, YouTube/Vimeo)
    if not segs:
        queue = [(k, u) for k, u in candidates if k in ("file", "youtube", "youtube_search", "vimeo")]
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
            if u in seen:
                continue
            seen.add(u)
            rec["tried"].append(f"audio:{k}:{u[:120]}")
            audio_path = getaudio(u, outdir)
            if audio_path:
                src = {"kind": f"audio_{k}", "url": page_of.get(u) or u}
                break
        if audio_path:
            segs = stt(audio_path, model)
    if not segs:
        if not links and not candidates:
            rec["stage"] = "no_call_link_in_release"
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
        h.setdefault("verified", src["kind"] in ("sec_transcript", "ir_transcript"))
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
