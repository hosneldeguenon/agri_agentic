"""Generate a SYNTHETIC household register with deliberate real-world mess.

No real person or household is represented: ids are codes, there are no names or phone numbers.
Deterministic (seed 7). Run:  python -m agri_predict.synth_households
"""
import random
import sqlite3

from .config import HOUSEHOLD_DB

SCHEMA = """CREATE TABLE households(
  household_id TEXT PRIMARY KEY, group_id TEXT, village TEXT, locality_raw TEXT, crop_raw TEXT,
  area_raw TEXT, area_unit TEXT, sowing_date_raw TEXT, expected_production_t TEXT, field_notes TEXT,
  synthetic INTEGER NOT NULL DEFAULT 1)"""

GROUPS = {"GV-BOR": ("Borgou", ["Tchatchou", "Bembereke", "Gnonkourokali"], "BO"),
          "GV-COL": ("Collines", ["Glazoue", "Dassa", "Savalou"], "CO"),
          "GV-ATA": ("Atacora", ["Natitingou", "Boukoumbe"], "AT"),
          "GV-ATL": ("Atlantique", ["Ouidah", "Allada"], "AL"),
          "GV-ZOU": ("Zou", ["Abomey", "Bohicon", "Zogbodomey"], "ZO")}

# hand-written messy rows: (group, locality_raw, crop_raw, area_raw, unit, date_raw, prod, note)
MESSY = [
    ("GV-BOR", "Borgu", "Maïs", "2,5", "ha", "15/05/2027", "5,4", "typo in department"),
    ("GV-BOR", "parakou", "mais", "12000", "", "2027-05-20", "", "town instead of department, m2 without unit"),
    ("GV-BOR", "Borgou", "riz", "500", "ha", "2027-06-01", "", "500 ha: probably 500 m2"),
    ("GV-BOR", "BORGOU", "Coton", "3", "ha", "2027-05-10", "", "crop not modelled"),
    ("GV-BOR", "Borgou", "mais", "", "", "2027-05-12", "3", "area missing"),
    ("GV-BOR", "Borgou", "mais", "1.5", "hectares", "03/04/2027", "", "ambiguous date 03/04"),
    ("GV-COL", "Collines", "Igname", "2", "ha", "12 mars 2027", "", "French long date"),
    ("GV-COL", "Colines", "manioc", "1", "ha", "2027-03-25", "", "typo"),
    ("GV-COL", "Collines", "maiz", "0,8", "ha", "2027-04-02", "", "crop typo"),
    ("GV-COL", "Glazoué", "tomate", "5000", "m2", "2027-02-20", "", "town + accent, m2"),
    ("GV-ATA", "Atacora", "riz", "1,2", "ha", "2024-03-01", "", "stale date (2024)"),
    ("GV-ATA", "Atacora", "", "2", "ha", "2027-05-05", "", "crop missing"),
    ("GV-ATA", "Natitingou", "mais", "2 ha", "", "2027-05-18", "4", "unit inside the number"),
    ("GV-ATL", "Atlantique", "tomate", "30", "ares", "2027-03-05", "", "ares"),
    ("GV-ATL", "Atlantique", "mais", "1", "ha", "31/02/2027", "", "impossible date"),
    ("GV-ATL", "Ouidah", "manioc", "0,5", "ha", "2026-09-20", "", "sowing already done"),
    ("GV-ZOU", "Zou", "mais", "1,5", "ha", "2027-03-18", "3,2", "first season, inside the advisor's window"),
    ("GV-ZOU", "zou", "mais", "2", "ha", "2026-12-15", "", "dry season: outside the window"),
    ("GV-ZOU", "Zou", "mais", "1", "ha", "20/08/2027", "", "second season window"),
    ("GV-ZOU", "Abomey", "manioc", "1", "ha", "2027-03-18", "", "crop with no confirmed window"),
]


def main():
    rnd = random.Random(7)
    HOUSEHOLD_DB.parent.mkdir(parents=True, exist_ok=True)
    if HOUSEHOLD_DB.exists():
        HOUSEHOLD_DB.unlink()
    c = sqlite3.connect(HOUSEHOLD_DB)
    c.execute(SCHEMA)
    counters = {g: 0 for g in GROUPS}

    def add(g, loc, crop, area, unit, d, prod, note):
        counters[g] += 1
        villages = GROUPS[g][1]
        hid = f"HH-{GROUPS[g][2]}-{counters[g]:03d}"
        c.execute("INSERT INTO households VALUES(?,?,?,?,?,?,?,?,?,?,1)",
                  (hid, g, rnd.choice(villages), loc, crop, area, unit, d, prod, note))

    for row in MESSY:
        add(*row)
    plans = {"GV-BOR": (["mais", "riz"], "2027-05-{:02d}"), "GV-COL": (["mais", "igname", "manioc"], "2027-03-{:02d}"),
             "GV-ATA": (["mais", "riz"], "2027-05-{:02d}"), "GV-ATL": (["mais", "tomate", "manioc"], "2027-03-{:02d}"),
             "GV-ZOU": (["mais"], "2027-03-{:02d}")}
    for g, (crops, fmt) in plans.items():
        for _ in range(6):
            add(g, GROUPS[g][0], rnd.choice(crops), str(round(rnd.uniform(0.5, 4), 1)), "ha",
                fmt.format(rnd.randint(5, 25)), str(round(rnd.uniform(1, 8), 1)), "clean record")
    c.commit()
    n = c.execute("SELECT COUNT(*) FROM households").fetchone()[0]
    c.close()
    print(f"{HOUSEHOLD_DB}: {n} synthetic households")


if __name__ == "__main__":
    main()
