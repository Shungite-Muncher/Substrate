"""Transparent, rule-based tone scoring for text.

Two axes, both oriented so that + is bad for a buyer:
  supply_tone  +1 = tightening (shortage, allocation, lead times stretching)
               -1 = easing (inventory glut, lead times shortening)
  price_tone   +1 = prices rising,  -1 = prices falling

Rules are deliberately simple and inspectable. Every match is returned so a
score can always be traced back to the exact phrase that moved it.
"""
from __future__ import annotations

import re

SUPPLY_UP = [
    r"shortages?", r"on allocation|allocation (?:mode|status)|put on allocation|allocat(?:e|ing) supply",
    r"lead[- ]times? (?:extend|stretch|lengthen|increas|ris|grow|jump|balloon)\w*",
    r"(?:extend|longer|stretch)\w* lead[- ]times?", r"sold out", r"capacity (?:constrain|crunch|tight)\w*",
    r"supply (?:constrain|crunch|tight|squeeze)\w*", r"tight(?:er|ening)? supply", r"force majeure",
    r"backlog", r"fully booked", r"(?:export|chip) (?:control|curb|ban|restriction)s?", r"tariffs?",
    r"(?:fab|plant|factory) (?:fire|outage|shutdown|halt)", r"earthquake", r"high utili[sz]ation",
    r"demand (?:outstrips|exceeds) supply", r"stockpil\w+", r"panic buying",
]
SUPPLY_DOWN = [
    r"oversuppl\w+", r"glut", r"inventory (?:correction|digestion|adjustment|overhang)", r"excess inventor\w+",
    r"lead[- ]times? (?:shorten|eas|normali[sz]|declin|fall|improv|drop)\w*", r"(?:shorter|easing) lead[- ]times?",
    r"weak(?:er|ening)? demand", r"demand (?:slump|slowdown|softness|weakness)", r"low utili[sz]ation",
    r"utili[sz]ation (?:fell|fall|drop|declin)\w*", r"capacity (?:expansion|additions?) (?:online|ramp)\w*",
    r"(?:ample|abundant|improving) supply", r"cut(?:s|ting)? production|production cuts?",
]
PRICE_UP = [
    r"price (?:hike|increase|rise)s?", r"rais\w* (?:its |their )?prices", r"prices? (?:rise|rising|rose|surg|climb|jump|soar|increas)\w*",
    r"contract prices? (?:up|rise|increase)\w*", r"asps? (?:up|rise|rising|increase)\w*", r"higher (?:asps?|prices)",
    r"pricing power", r"price (?:up|upward)",
]
PRICE_DOWN = [
    r"price cuts?", r"cut(?:s|ting)? prices", r"prices? (?:fall|fell|falling|declin|drop|slump|slid|eroding|decreas)\w*",
    r"price (?:erosion|pressure|war)", r"discount\w*", r"asps? (?:down|fall|declin)\w*", r"lower (?:asps?|prices)",
]

_C = {k: [re.compile(p, re.I) for p in v] for k, v in
      {"su": SUPPLY_UP, "sd": SUPPLY_DOWN, "pu": PRICE_UP, "pd": PRICE_DOWN}.items()}


def score(text: str) -> dict:
    hits = {k: [m.group(0) for p in pats for m in p.finditer(text)] for k, pats in _C.items()}

    def axis(up, down):
        u, d = len(hits[up]), len(hits[down])
        return 0.0 if u + d == 0 else round((u - d) / (u + d), 3)

    return {
        "supply_tone": axis("su", "sd"),
        "price_tone": axis("pu", "pd"),
        "matches": {"supply_tightening": hits["su"], "supply_easing": hits["sd"],
                    "price_up": hits["pu"], "price_down": hits["pd"]},
        "n": sum(len(v) for v in hits.values()),
    }


KEY_TERMS = re.compile(r"lead[- ]time|inventor|utili[sz]ation|pricing|price|supply|demand|capacity|allocation|"
                       r"backlog|outlook|guidance|shortage|book-to-bill", re.I)


def key_sentences(text: str, limit: int = 6) -> list[str]:
    sents = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text))
    picked = [s.strip() for s in sents if 40 < len(s) < 400 and KEY_TERMS.search(s)]
    return picked[:limit]
