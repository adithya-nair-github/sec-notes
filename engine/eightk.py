"""Read an 8-K and decide whether its Item 5.02 is about a finance leader."""
import html as htmlmod
import re

FIN_TITLES = re.compile(
    r"chief financial officer|chief accounting officer|principal financial officer|"
    r"principal accounting officer|\bcontroller\b|\bcomptroller\b|\bcfo\b|\bcao\b|"
    r"treasurer|vice president[,]? (?:of )?(?:financial reporting|finance|accounting)|"
    r"head of (?:financial reporting|accounting)", re.I)
EXEC_TITLES = re.compile(r"chief executive officer|\bceo\b|chief operating officer|\bcoo\b|\bpresident\b", re.I)
CHANGE_VERBS = re.compile(
    r"appoint|named|elect(?:ed|ion) .{0,40}(?:officer|controller)|promot|resign|retire|depart|"
    r"step(?:s|ped)? down|succeed|successor|terminat|separat|transition|will serve as|hired|join(?:s|ed) the company",
    re.I)


def to_text(raw):
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw or "")
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = htmlmod.unescape(raw)
    return re.sub(r"\s+", " ", raw).strip()


def item_section(text, item="5.02"):
    """Text of one Item section, cut before the next Item or the signature block."""
    m = re.search(r"item\s*" + re.escape(item), text, re.I)
    if not m:
        return ""
    rest = text[m.end():]
    end = re.search(r"item\s*\d\.\d\d|signatures?\b", rest, re.I)
    sec = rest[: end.start()] if end else rest[:6000]
    # drop the standard item heading so snippets start at the actual news
    sec = re.sub(r"^[\s.:\-]*(?:departure of directors or certain officers"
                 r"(?:[;,]?\s*(?:and\s+)?(?:election of directors|appointment of certain officers|"
                 r"compensatory arrangements of certain officers))*|"
                 r"changes in registrant'?s certifying accountant|"
                 r"costs associated with exit or disposal activities|"
                 r"notice of delisting or failure to satisfy a continued listing rule or standard[;,]?\s*(?:transfer of listing)?|"
                 r"entry into a material definitive agreement|"
                 r"creation of a direct financial obligation or an obligation under an off-balance sheet arrangement of a registrant|"
                 r"non-reliance on previously issued financial statements or a related audit report or completed interim review|"
                 r"non-reliance on previously issued financial statements)[\s.:;]*",
                 "", sec, flags=re.I)
    return sec.strip(" .:-")


def classify_502(raw_html):
    """Returns (kind, snippet): kind in {'finance', 'exec', None}."""
    sec = item_section(to_text(raw_html), "5.02")
    if not sec:
        return None, ""
    sentences = re.split(r"(?<=[.;])\s+", sec)
    fin = [s for s in sentences if FIN_TITLES.search(s) and CHANGE_VERBS.search(s)]
    if fin:
        return "finance", " ".join(fin[:2])[:450]
    for s in sentences:
        if EXEC_TITLES.search(s) and CHANGE_VERBS.search(s):
            return "exec", s[:400]
    return None, ""


def names_in(snippet):
    """Rough person-name grab ('Kyle G. Koreyva') for matching against contacts."""
    return set(re.findall(r"\b([A-Z][a-z]+(?:\s[A-Z]\.)?\s[A-Z][a-zA-Z'\-]+)\b", snippet or ""))
