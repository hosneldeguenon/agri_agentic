"""Deterministic cleaning of messy plot records (typos, units, date formats).

Real extension-office registers are messy. This is done in code, not by the LLM,
so the same input always yields the same output and every decision is logged."""
import difflib
import re
import unicodedata
from datetime import date, timedelta

from . import engine
from .config import today

CITY_TO_DEPT = {
    "cotonou": "Littoral", "parakou": "Borgou", "natitingou": "Atacora", "porto-novo": "Oueme",
    "portonovo": "Oueme", "abomey": "Zou", "bohicon": "Zou", "glazoue": "Collines", "dassa": "Collines",
    "save": "Collines", "malanville": "Alibori", "kandi": "Alibori", "djougou": "Donga",
    "lokossa": "Mono", "ouidah": "Atlantique", "abomey-calavi": "Atlantique", "pobe": "Plateau",
    "sakete": "Plateau", "dogbo": "Couffo", "aplahoue": "Couffo", "tanguieta": "Atacora",
    "nikki": "Borgou", "bembereke": "Borgou", "ouemé": "Oueme",
}
CROP_ALIASES = {
    "mais": "mais", "maize": "mais", "corn": "mais", "riz": "riz", "rice": "riz", "paddy": "riz",
    "manioc": "manioc", "cassava": "manioc", "igname": "igname", "yam": "igname",
    "tomate": "tomate", "tomates": "tomate", "tomato": "tomate",
}
KNOWN_NOT_MODELED = {"coton", "anacarde", "cajou", "ananas", "arachide", "gombo", "niebe", "haricot",
                     "oignon", "piment", "soja", "sorgho"}
MONTHS = {"janvier": 1, "janv": 1, "fevrier": 2, "fev": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
          "juillet": 7, "juil": 7, "aout": 8, "septembre": 9, "sept": 9, "octobre": 10, "oct": 10,
          "novembre": 11, "nov": 11, "decembre": 12, "dec": 12}
MAX_AREA_HA = 50.0
MIN_AREA_HA = 0.01


def _plain(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower().strip()
    return re.sub(r"\s+", " ", s)


def _issue(code, severity, field, message):
    return {"code": code, "severity": severity, "field": field, "message": message}


def resolve_locality(raw):
    if raw is None or not str(raw).strip():
        return None, [_issue("LOCALITY_MISSING", "error", "locality", "No locality given.")]
    p = _plain(raw)
    depts = {_plain(d): d for d in engine.localites()}
    if p in depts:
        return depts[p], []
    if p in CITY_TO_DEPT:
        return CITY_TO_DEPT[p], [_issue("LOCALITY_FROM_CITY", "warn", "locality",
                                        f"'{raw}' is a town; mapped to department {CITY_TO_DEPT[p]}.")]
    close = difflib.get_close_matches(p, list(depts) + list(CITY_TO_DEPT), n=1, cutoff=0.78)
    if close:
        d = depts.get(close[0]) or CITY_TO_DEPT[close[0]]
        return d, [_issue("LOCALITY_FUZZY", "warn", "locality", f"'{raw}' read as '{d}' (typo correction).")]
    return None, [_issue("LOCALITY_UNKNOWN", "error", "locality",
                         f"'{raw}' not recognised. Departments: {', '.join(engine.localites())}.")]


def resolve_crop(raw):
    if raw is None or not str(raw).strip():
        return None, [_issue("CROP_MISSING", "error", "crop", "No crop given.")]
    p = _plain(raw)
    if p in CROP_ALIASES:
        return CROP_ALIASES[p], []
    if p in KNOWN_NOT_MODELED:
        return None, [_issue("CROP_NOT_MODELED", "error", "crop",
                             f"'{raw}' is a real crop but is not modelled. Supported: {', '.join(engine.KC)}.")]
    close = difflib.get_close_matches(p, list(CROP_ALIASES), n=1, cutoff=0.8)
    if close:
        c = CROP_ALIASES[close[0]]
        return c, [_issue("CROP_FUZZY", "warn", "crop", f"'{raw}' read as '{c}' (typo correction).")]
    return None, [_issue("CROP_UNKNOWN", "error", "crop",
                         f"'{raw}' not recognised. Supported: {', '.join(engine.KC)}.")]


def resolve_area(raw, unit):
    issues = []
    if raw is None or str(raw).strip() == "":
        return None, [_issue("AREA_MISSING", "error", "area", "No area given.")]
    txt = _plain(raw)
    m = re.search(r"(\d+(?:[.,]\d+)?)", txt)
    if not m:
        return None, [_issue("AREA_UNPARSEABLE", "error", "area", f"Cannot read area '{raw}'.")]
    val = float(m.group(1).replace(",", "."))
    u = _plain(unit) if unit else ""
    if not u:  # unit may be inside the raw string
        rest = txt[m.end():].strip()
        u = rest
    if re.match(r"^(ha|hectare|hectares)", u):
        ha = val
    elif re.match(r"^(m2|m²|metre|metres|m 2)", u) or "m2" in u:
        ha = val / 10000.0
    elif re.match(r"^(a|are|ares)$", u):
        ha = val / 100.0
    elif u == "":
        if val >= 100:
            ha = val / 10000.0
            issues.append(_issue("AREA_UNIT_ASSUMED_M2", "warn", "area", f"No unit; {val:g} read as m²."))
        else:
            ha = val
            issues.append(_issue("AREA_UNIT_ASSUMED_HA", "warn", "area", f"No unit; {val:g} read as hectares."))
    else:
        return None, [_issue("AREA_UNIT_UNKNOWN", "error", "area", f"Unknown unit '{unit or u}'.")]
    ha = round(ha, 4)
    if ha < MIN_AREA_HA:
        issues.append(_issue("AREA_TOO_SMALL", "error", "area", f"{ha} ha is implausibly small."))
    if ha > MAX_AREA_HA:
        issues.append(_issue("AREA_TOO_LARGE", "error", "area",
                             f"{ha} ha exceeds {MAX_AREA_HA:g} ha: not a smallholder plot, check the unit."))
    return ha, issues


def resolve_date(raw):
    if raw is None or str(raw).strip() == "":
        return None, [_issue("DATE_MISSING", "error", "sowing_date", "No sowing date given.")]
    txt = _plain(raw)
    d, issues = None, []
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", txt)
    if m:
        y, mo, da = map(int, m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})[/.\- ](\d{1,2})[/.\- ](\d{2,4})", txt)
        if m:
            da, mo, y = map(int, m.groups())
            y = y + 2000 if y < 100 else y
            if da <= 12 and mo <= 12 and da != mo:
                issues.append(_issue("DATE_AMBIGUOUS_ASSUMED_DDMM", "warn", "sowing_date",
                                     f"'{raw}' could be DD/MM or MM/DD; read as DD/MM."))
        else:
            m = re.fullmatch(r"(\d{1,2})\s*(?:er)?\s+([a-z]+)\.?\s+(\d{2,4})", txt)
            if not m or m.group(2) not in MONTHS:
                return None, [_issue("DATE_UNPARSEABLE", "error", "sowing_date", f"Cannot read date '{raw}'.")]
            da, y = int(m.group(1)), int(m.group(3))
            mo = MONTHS[m.group(2)]
            y = y + 2000 if y < 100 else y
    try:
        d = date(y, mo, da)
    except ValueError:
        return None, [_issue("DATE_INVALID", "error", "sowing_date", f"'{raw}' is not a real calendar date.")]
    t = today()
    if d > t + timedelta(days=400):
        issues.append(_issue("DATE_TOO_FAR", "error", "sowing_date", f"{d} is more than 400 days ahead."))
    elif d < t - timedelta(days=200):
        issues.append(_issue("DATE_STALE", "error", "sowing_date", f"{d} is over 200 days in the past: stale record."))
    elif d < t:
        issues.append(_issue("SOWING_ALREADY_DONE", "warn", "sowing_date",
                             f"{d} is already past: advice covers a season that has started."))
    return d.isoformat(), issues


def normalize(locality_raw, crop_raw, area_raw, area_unit, sowing_date_raw):
    loc, i1 = resolve_locality(locality_raw)
    crop, i2 = resolve_crop(crop_raw)
    ha, i3 = resolve_area(area_raw, area_unit)
    dt_, i4 = resolve_date(sowing_date_raw)
    issues = i1 + i2 + i3 + i4
    status = "UNUSABLE" if any(i["severity"] == "error" for i in issues) else \
             "NEEDS_REVIEW" if issues else "OK"
    return {"status": status, "plot": {"locality": loc, "crop": crop, "area_ha": ha, "sowing_date": dt_},
            "issues": issues}
