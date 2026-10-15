"""Number-grounding check for advisory text.

A small open-weights model can write fluent text with wrong figures. Before an
advisory enters the officer's queue, every number in the message must be present
in the tool outputs (the "evidence") the agent cites. Ungrounded numbers => the
draft is rejected and the agent must rewrite it (this is the recovery loop).
"""
import re

NUM_RE = re.compile(r"(?<![A-Za-z\u00c0-\u00ff\d])\d+(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d+)?")


def _to_float(tok: str) -> float:
    return float(re.sub(r"[ \u00a0\u202f]", "", tok).replace(",", "."))


def numbers_in_text(text: str) -> list[float]:
    return [_to_float(t) for t in NUM_RE.findall(text)]


def numbers_in_payload(obj) -> set[float]:
    out: set[float] = set()

    def add(x: float):
        out.update({x, float(round(x)), round(x, 1)})

    def walk(o):
        if isinstance(o, bool) or o is None:
            return
        if isinstance(o, (int, float)):
            add(float(o))
        elif isinstance(o, str):
            for t in NUM_RE.findall(o):
                add(_to_float(t))
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)

    walk(obj)
    return out


def ungrounded_numbers(message: str, payloads: list) -> list[float]:
    allowed: set[float] = set()
    for p in payloads:
        allowed |= numbers_in_payload(p)
    bad = []
    for x in numbers_in_text(message):
        if not any(abs(x - a) < 1e-9 for a in allowed) and x not in bad:
            bad.append(x)
    return bad
