"""Inject data into template.html -> dfw-capital-radar.src.html. Usage: python3 -I build_html.py <data_json> [<cad_json>]"""
import json, sys
d = json.load(open(sys.argv[1]))
rows = d["rows"]
cad = json.load(open(sys.argv[2])) if len(sys.argv) > 2 else None
sources = [
    {"name": "HUD Active Insured Multifamily Mortgages (FHA-BF90-RM-A.xlsx)",
     "note": "loan rate, balance, original amount, endorsement dates, units, program. hud.gov/hud-partners/multifamily-fhasl-active.",
     "asOf": "Oct 31, 2026"},
    {"name": "HUD Insured Active Multifamily FHA Property Addresses",
     "note": "street address and HUD property ID per FHA number. hud.gov/hud-partners/multifamily-preservation.", "asOf": "Nov 3, 2026"},
    {"name": "HUD Active Multifamily Portfolio, Property Level",
     "note": "county per HUD property ID. hud.gov/hud-partners/multifamily-preservation.", "asOf": "Nov 3, 2026"},
]
if cad:
    for r in rows:
        m = cad["matches"].get(r["id"])
        if m: r.update(m)
    sources += cad["sources"]
keep = ["id","name","city","county","address","units","endorsed","origAmt","rate","upb","program","taxExempt","lihtc","marketValue","owner","cadSrc"]
rows = [{k: r[k] for k in keep if k in r and r[k] is not None and r[k] is not False} for r in rows]
meta = {"counts": d["counts"], "loanAsOf": "2026-10-31", "loanAsOfLabel": "Oct 31, 2026 (HUD loan file date)",
        "sources": sources,
        "countyNote": "Where the join found no county (69 Texas loans), county was inferred from the city when every matched loan in that city "
                      "sat in one county (or only in DFW counties). Mansfield and Springtown were assigned from a manual DFW city list."}
t = open("template.html").read()
t = t.replace("/*__META__*/null", json.dumps(meta)).replace("/*__ROWS__*/[]", json.dumps(rows, separators=(",", ":")))
open("dfw-capital-radar.src.html", "w").write(t)
print("rows", len(rows), "bytes", len(t))
