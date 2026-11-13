"""Company-name cleaning (brief Part 3), reused for competitor names, plus reviewer-name formatting.

clean_company_name(raw) -> (clean, needs_review)
name_violations(name)   -> list of reasons a cleaned name still fails the Part 3 standard
reviewer_name(raw)      -> "First L."

Deterministic rules only: no LLM, so the same input always gives the same name.
Lifted from the lead-list pipeline (scripts/clean.py, scripts/validate_final.py) so the
command and the list use one standard.
"""
from __future__ import annotations

import re

SEO_SEG = re.compile(
    r"^(best|top|top-rated|affordable|#1|the best)?\s*(emergency |cosmetic |family |general |pediatric |kids |sedation )?"
    r"(dentist|dentistry|dental (care|office|clinic|services)|orthodontist|orthodontics|invisalign|dental implants?)"
    r"\s*(in|near|of|for|at|&|and)?\b.*$", re.I)
SEO_WORDS = re.compile(r"\b(near me|best dentist|dentist in|dentists in|top rated|affordable dentist|emergency dentist in|"
                       r"same day|open now|walk[- ]ins?|call now|book now|free consultation|accepting|medi-?cal)\b", re.I)
LEGAL_TAIL = re.compile(
    r"(,?\s+(a\s+)?(professional\s+)?(dental\s+(?=corporation\s*$|corp\.?\s*$)(?<=professional dental ))?(corporation|corp\.?|incorporated|inc\.?|llc\.?|l\.l\.c\.|pllc|p\.?c\.?|"
    r"a\.?p\.?c\.?|apdc|ltd\.?|co\.?)\s*)+$", re.I)
CRED_TAIL = re.compile(r"(,?\s+(dds|d\.d\.s\.?|dmd|d\.m\.d\.?|ms|msd|m\.s\.d\.?|md|phd|mph|faces|facs|inc)\.?)+\s*$", re.I)
BUSINESS_WORDS = re.compile(
    r"\b(dental|dentistry|dentists?|smiles?|smile|care|center|centre|group|family|orthodontics?|ortho|clinic|studio|"
    r"office|associates|implants?|oral|surgery|periodontics|endodontics|prosthodontics|pediatric|kids|children|"
    r"aesthetics?|cosmetic|teeth|tooth|health|wellness|practice|partners|spa|arts?|boutique|institute|specialists?|"
    r"braces|denture|dentures|works|lab|house|place|square|plaza|village|valley|hills|beach|city|park|bay)\b", re.I)
SEPARATORS = re.compile(r"\s+[|\-–—/]\s+|\s*\|\s*|:\s+|\s+•\s+|/(?=\s*Dr\.?\s)|\s+-(?=[A-Z])|"
                        r"(?:(?<=Care)|(?<=Group)|(?<=Dental)|(?<=Center)|(?<=Office)|(?<=Clinic)|(?<=Studio)|(?<=Smiles)|(?<=Dentistry))-(?=(?:Best|Top|Cosmetic|Emergency|Family|General|Pediatric|Dentist|Dentistry|Orthodontist|Invisalign)\b)")
KEEP_UPPER = {"DDS", "DMD", "MS", "MSD", "LA", "USA", "II", "III", "IV", "OC", "SF", "TMJ", "BH", "CA", "&"}


def smart_title(s: str) -> str:
    if not s:
        return s
    letters = [c for c in s if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) < 0.8:
        return s  # already mixed case: trust the business's own casing
    out = []
    for w in s.split():
        core = re.sub(r"[^A-Za-z]", "", w)
        if core.upper() in KEEP_UPPER or (len(core) <= 1):
            out.append(w.upper() if core.upper() in KEEP_UPPER else w)
        elif "'" in w:
            out.append(w[:1].upper() + w[1:].lower())
        elif "-" in w:
            out.append("-".join(p[:1].upper() + p[1:].lower() for p in w.split("-")))
        else:
            out.append(w[:1].upper() + w[1:].lower())
    small = {"and", "of", "the", "in", "at", "for", "on", "by"}
    out = [w.lower() if i and w.lower() in small else w for i, w in enumerate(out)]
    s2 = " ".join(out)
    return re.sub(r"\bMc([a-z])", lambda m: "Mc" + m.group(1).upper(), s2)


USPS = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND "
        "OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()
STATE_NAMES = ("Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|"
               "Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|"
               "Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|"
               "North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|"
               "South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming")
# ", TX 78701" / ", Texas" at the end of a name. The comma is required, so "Dental Care of Texas" keeps its state.
STATE_SUFFIX = re.compile(r",\s*(" + "|".join(USPS) + r"|" + STATE_NAMES + r")\b\.?\s*\d{0,5}$")


def _strip_tails(s: str) -> str:
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"\s*\([^)]*\)\s*", " ", s).strip()
        s = LEGAL_TAIL.sub("", s).strip(" ,.-")
        s = re.sub(r",\s*(dr\.?\s+)?[A-Z][a-zA-Z.'\- ]+,?\s*(DDS|DMD|D\.D\.S\.|D\.M\.D\.)\.?$", "", s).strip(" ,")
        s = re.sub(r"\s+(of|in)\s+[A-Z][a-z]+(\s[A-Z][a-z]+)?,\s*(CA|California)\b.*$", r" \1 \2".rstrip(), s) if False else s
        s = STATE_SUFFIX.sub("", s).strip(" ,")
    return re.sub(r"\s+", " ", s).strip()


NON_LATIN = re.compile(r"[^\x00-\x7F\u00C0-\u024F’'“”]+")
EMOJI = re.compile(r"[\U0001F000-\U0001FFFF\u2600-\u27BF\uFE0F]")
ACRO = {"LA", "USA", "OC", "TMJ", "NYC", "II", "III", "IV", "USC", "UCLA", "LAX", "SCV", "AAA", "NY", "NJ", "EZ", "BH",
        "DTLA", "SF", "VIP", "HB", "ER"}


def polish(s: str) -> str:
    """Final normalisation shared by prospect and competitor names."""
    s = s.replace("’", "'").replace("‘", "'").replace("“", "").replace("”", "").replace('"', "")
    s = EMOJI.sub(" ", s)
    s = NON_LATIN.sub(" ", s)
    s = re.sub(r"\s+(in|near)\s+[A-Z][a-z]+(\s[A-Z][a-z]+)?$", "", s.strip())
    if not s.startswith("Dr. "):
        s = re.sub(r"\b(DDS|D\.D\.S\.?|DMD|D\.M\.D\.?|MSD)\b\.?", " ", s, flags=re.I)
    s = LEGAL_TAIL.sub("", s.strip())
    words = []
    for w in s.split():
        core = re.sub(r"[^A-Za-z]", "", w)
        if len(core) >= 5 and core.isupper() and core not in ACRO:
            w = w[:1] + w[1:].lower()
        words.append(w)
    small = {"and", "of", "the", "in", "at", "for", "on", "by"}
    words = [w[:1].upper() + w[1:] if (w[:1].islower() and (i == 0 or w not in small)) else w
             for i, w in enumerate(words)]
    s = " ".join(words)
    s = re.sub(r"\s+", " ", s).strip(" ,.-&|:/")
    s = re.sub(r"\s+(and|&)$", "", s)
    return s


def person_order_clear(raw: str) -> bool:
    r = raw.strip()
    return bool(re.match(r"^\s*dr\.?\s", r, re.I) or re.search(r",\s*(dds|dmd|d\.d\.s|d\.m\.d)", r, re.I)
                or re.search(r"\b[A-Z]\.\s", r))


def is_person_name(s: str) -> bool:
    core = re.sub(r"^(dr\.?\s+)", "", s, flags=re.I)
    core = CRED_TAIL.sub("", core).strip(" ,")
    words = core.replace(",", " ").split()
    return 2 <= len(words) <= 4 and not BUSINESS_WORDS.search(core) and all(re.match(r"^[A-Za-z][A-Za-z.'\-]*$", w) for w in words)


def clean_company_name(raw: str) -> tuple[str, bool]:
    s = (raw or "").strip()
    if not s:
        return "", False
    s = s.replace("’", "'").replace("“", '"').replace("”", '"')
    segs = [x.strip(" ,.") for x in SEPARATORS.split(s) if x and x.strip(" ,.")]
    chosen = None
    for seg in segs:
        if SEO_SEG.match(seg) and len(segs) > 1 and not BUSINESS_WORDS.search(re.sub(SEO_SEG, "", seg)):
            continue
        if SEO_WORDS.search(seg) and len(segs) > 1:
            continue
        # a doctor-name segment followed by a brand segment: prefer the brand
        if is_person_name(seg) and any(BUSINESS_WORDS.search(x) and not SEO_SEG.match(x) for x in segs[1:]) and chosen is None \
                and re.search(r"\b(dds|dmd|dr)\b", s, re.I) and segs.index(seg) == 0 and BUSINESS_WORDS.search(segs[1] if len(segs) > 1 else ""):
            continue
        chosen = seg
        break
    s = chosen or segs[0] if segs else s
    generic = re.compile(r"^(?:(?:family|cosmetic|general|implant|implants|&|and|dentistry|dental|care|"
                         r"orthodontics|pediatric|emergency|office|clinic|center|services?)\s*)+$", re.I)
    if generic.match(s):
        person = next((x for x in segs if is_person_name(x)), None)
        if person:
            s = person
    s = _strip_tails(s)
    s = smart_title(s)
    if is_person_name(s) and re.search(r"\b(dds|dmd|d\.d\.s|d\.m\.d|dr)\b", raw, re.I):
        core = re.sub(r"^(dr\.?\s+)", "", s, flags=re.I)
        core = CRED_TAIL.sub("", core).strip(" ,")
        # "Dr. Jane Kim", "Jane M. Kim, DDS" and "Jane Kim, DDS" read First Last. A bare
        # "Kim Jane DDS" is often surname-first: keep the listing order and no "Dr."
        # (build_list.py adds "Dr." only if the practice's own site confirms the order)
        s = f"Dr. {core}" if person_order_clear(raw) else core
    else:
        s = CRED_TAIL.sub("", s).strip(" ,") or s
    s = polish(s)
    # SEO tail after the brand: "Z Dental Group Dentist Los Angeles" -> "Z Dental Group"
    s = re.sub(r"^(.+?\b(?:dental|dentistry|group|care|center|centre|smiles?|studio|office|clinic|orthodontics))\s+"
               r"(?:dentist|dentists|dentistry|orthodontist)\s+[A-Z][a-z]+(?:\s[A-Z][a-z]+)?$", r"\1", s, flags=re.I)
    # doctor name trailing a brand when the raw name ended in a credential:
    # "Crenshaw Professional Dental Center James T. Black DDS" -> "Crenshaw Professional Dental Center"
    if len(segs) == 1 and re.search(r"\b(dds|dmd|d\.d\.s|d\.m\.d)\.?\s*$", raw, re.I):
        m = re.match(r"^(.+?\b(?i:dental|dentistry|group|care|center|centre|smiles?|studio|office|clinic|orthodontics|arts))"
                     r"\s+(?:Dr\.?\s+)?[A-Z][a-z]+(?:\s[A-Z]\.?)?\s[A-Z][a-z\-]+$", s)
        if m:
            s = m.group(1)
    needs = bool(len(s.split()) > 6 or re.search(r"\.[A-Za-z]", s) or s.lower() in ("dentist", "dentistry", "dental office")
                 or SEO_WORDS.search(s) or SEPARATORS.search(s) or re.search(r"[|#@]|\d{5,}", s)
                 or SEO_SEG.fullmatch(s or ""))
    return s, needs


# ---------------- validation (same standard as the lead list) ----------------

LEGAL = re.compile(r"\b(llc|l\.l\.c|inc|incorporated|pc|p\.c|pllc|apc|a\.p\.c|corp|corporation|ltd|co)\b\.?", re.I)
CREDS = re.compile(r"\b(dds|d\.d\.s|dmd|d\.m\.d|msd|m\.s\.d|phd)\b\.?", re.I)
SEO = re.compile(r"\b(best|near me|top[- ]rated|affordable dentist|dentist in|dentists in|open now|call now|book now)\b|#1",
                 re.I)
SEPS = re.compile(r"[|:;]|\s[-\u2013\u2014]\s|\s/\s")
STATE_TAIL = re.compile(r",\s*(" + "|".join(USPS) + r"|" + STATE_NAMES + r")\.?$")
CITY_TAIL = re.compile(r"\b(in|near)\s+[A-Z][a-z]+(\s[A-Z][a-z]+)?$")
SMART = re.compile("[\u2018\u2019\u201c\u201d]")
ACRONYMS = {"LA", "USA", "OC", "TMJ", "NYC", "II", "III", "IV", "USC", "UCLA", "LAX", "SCV", "ABC", "AAA", "NY", "NJ",
            "EZ", "BH", "CDG", "JJ", "DTLA", "SF", "KO", "VIP", "HB", "MD", "ER", "AM", "PM", "SoCal"}


def name_violations(n: str) -> list[str]:
    """Reasons a cleaned name is not fit for an email sentence. Empty list = passes."""
    v = []
    if not n or not n.strip():
        return ["empty"]
    if n != n.strip() or "  " in n:
        v.append("whitespace")
    if LEGAL.search(n):
        v.append("legal suffix")
    if CREDS.search(n):
        v.append("credential (DDS/DMD)")
    if SEO.search(n):
        v.append("SEO phrase")
    if SEPS.search(n):
        v.append("separator tail")
    if STATE_TAIL.search(n) and not re.search(r"\bof\s+\S+$", n):
        v.append("state tail")
    if CITY_TAIL.search(n):
        v.append("'in/near <City>' tail")
    if EMOJI.search(n):
        v.append("emoji")
    if SMART.search(n):
        v.append("smart quotes")
    if re.search(r"[^\w)'.!&]$", n) or n.endswith((".", ",")) and not re.search(r"\b(Dr|St|Jr)\.$", n):
        v.append("trailing punctuation")
    words = n.split()
    if len(words) > 6:
        v.append(f"{len(words)} words (>6)")
    cores = [re.sub(r"[^A-Za-z]", "", w) for w in words]
    all_caps = len([c for c in cores if c]) > 1 and all(c.isupper() for c in cores if c)
    caps = [c for c in cores if c.isupper() and c not in ACRONYMS and (len(c) >= 5 or (all_caps and len(c) >= 3))]
    if caps:
        v.append(f"ALL CAPS word(s): {' '.join(caps)}")
    return v


def clean_or_blank(raw: str) -> str:
    """Cleaned name, or "" when the cleaner cannot reach the standard (never show a raw SEO name)."""
    name, _ = clean_company_name(raw)
    return "" if name_violations(name) else name


# ---------------- reviewer names ----------------

CYR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
               ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t",
                "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya"]))


def _latin(s: str) -> str:
    """Cyrillic -> Latin, so 'Хейди Хунко' reads 'Kheydi K.' in an English email."""
    if not re.search("[\u0400-\u04ff]", s):
        return s
    out = []
    for ch in s:
        t = CYR.get(ch.lower())
        out.append(ch if t is None else (t.capitalize() if ch.isupper() else t))
    return "".join(out)


def reviewer_name(raw: str) -> str:
    """'sarah miller' -> 'Sarah M.', 'J. R. Smith' -> 'J. S.', 'Kim' -> 'Kim', '' -> 'A patient'."""
    raw = _latin(raw or "")
    parts = [p for p in re.split(r"\s+", re.sub(r"[^\w\s.'\-]", " ", raw or "")) if p and any(c.isalpha() for c in p)]
    if not parts:
        return "A patient"
    p0 = parts[0]
    first = p0[:1].upper() + p0[1:].lower() if (p0.isupper() or p0.islower()) else p0
    if len(parts) == 1:
        return first
    last = next((c for c in parts[-1] if c.isalpha()), "")
    return f"{first} {last.upper()}."
