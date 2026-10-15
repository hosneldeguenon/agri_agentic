import os
import tempfile

os.environ.setdefault("AGRI_TODAY", "2026-10-12")
os.environ["AGRI_RUNTIME_DIR"] = tempfile.mkdtemp()

from agri_predict import audit, grounding, normalize  # noqa: E402


def test_grounding_accepts_only_evidence_numbers():
    ev = {"expected_irrigation_mm": 302, "harvest_date_fr": "21/08/2027", "expected_volume_m3": 7550}
    assert grounding.ungrounded_numbers("302 mm, récolte le 21/08/2027, 7 550 m³", [ev]) == []
    assert grounding.ungrounded_numbers("Gain de 99999 FCFA", [ev]) == [99999.0]


def test_units_with_digits_are_not_numbers():
    assert grounding.numbers_in_text("7550 m3 et CO2") == [7550.0]


def test_normalize_units_and_dates():
    r = normalize.normalize("Colines", "maiz", "5000", "m2", "12 mars 2027")
    assert r["plot"] == {"locality": "Collines", "crop": "mais", "area_ha": 0.5, "sowing_date": "2027-03-12"}
    assert r["status"] == "NEEDS_REVIEW"


def test_normalize_refuses_instead_of_guessing():
    assert normalize.normalize("Borgou", "", "2", "ha", "2027-05-01")["status"] == "UNUSABLE"
    assert normalize.normalize("Borgou", "mais", "", "", "2027-05-01")["status"] == "UNUSABLE"


def test_audit_chain_detects_edit():
    audit.append("tool_call", "t", "x", {"a": 1}, {"ok": True})
    audit.append("tool_call", "t", "y", {"a": 2}, {"ok": True})
    assert audit.verify()[0]
    p = audit._path()
    p.write_text(p.read_text().replace('"a": 1', '"a": 9'), encoding="utf-8")
    assert not audit.verify()[0]
