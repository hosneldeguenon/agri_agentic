"""Sowing windows confirmed by an extension advisor.

Source: face-to-face interview with an agricultural extension advisor of the Zou department (answers noted by
the author, see FIELD_INPUT.md). Only the entries below are covered. Everything else is reported as
'not_covered' and is NOT checked. Month granularity only: the advisor stressed that dates also depend on the
commune, the actual onset of rains and the forecast, so 'in_window' never means 'good date'.
Kept free of digits in text fields: the grounding check treats every number in a tool output as citable.
"""
SOURCE = "interview with an extension advisor of the Zou department (face-to-face, noted by the author)"

WINDOWS = {
    ("Zou", "mais"): [(3, 4, "first season (March to April)"), (8, 9, "second season (August to September)")],
}


def check(locality: str, crop: str, sowing_date_iso: str) -> dict:
    seasons = WINDOWS.get((locality, crop))
    if not seasons:
        return {"status": "not_covered", "source": None,
                "note": "No confirmed sowing-window data for this department and crop: the season is NOT checked."}
    month = int(sowing_date_iso[5:7])
    for lo, hi, name in seasons:
        if lo <= month <= hi:
            return {"status": "in_window", "season": name, "source": SOURCE,
                    "note": "Inside a usual sowing window. Local rain onset, commune and forecast still decide."}
    return {"status": "out_of_season", "source": SOURCE,
            "note": "Outside the usual sowing windows for this department and crop: " +
                    " and ".join(n for _, _, n in seasons) + "."}
