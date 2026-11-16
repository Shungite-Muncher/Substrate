"""Match DFW HUD loans to appraisal-district parcels by street address.
Usage: python3 -I match_cad.py <hud_json> <tad_txt> <dcad_dir> <out_json>
Only exact (street number + normalized street name) matches on apartment-class parcels are kept."""
import json, re, sys
import pandas as pd
hud, tad_path, dcad_dir, out = sys.argv[1:5]
rows = json.load(open(hud))["rows"]

SUF = {"STREET":"ST","ST.":"ST","AVENUE":"AVE","AV":"AVE","AVE.":"AVE","DRIVE":"DR","DR.":"DR","ROAD":"RD","RD.":"RD","BOULEVARD":"BLVD",
       "BLVD.":"BLVD","LANE":"LN","PARKWAY":"PKWY","PKY":"PKWY","FREEWAY":"FWY","HIGHWAY":"HWY","COURT":"CT","CIRCLE":"CIR","PLACE":"PL",
       "TRAIL":"TRL","TERRACE":"TER","EXPRESSWAY":"EXPY","SQUARE":"SQ","CROSSING":"XING","NORTH":"N","SOUTH":"S","EAST":"E","WEST":"W",
       "NORTHEAST":"NE","NORTHWEST":"NW","SOUTHEAST":"SE","SOUTHWEST":"SW"}
def norm(a):
    if not isinstance(a, str): return None
    a = re.sub(r"[.,#]", " ", a.upper())
    a = re.split(r"\b(APT|UNIT|STE|SUITE|BLDG)\b", a)[0]
    toks = [SUF.get(t, t) for t in a.split()]
    if not toks or not re.match(r"^\d+[A-Z]?$", toks[0]): return None
    return " ".join(toks)
def key(a):
    n = norm(a)
    if not n: return None
    toks = n.split()
    num, rest = toks[0], toks[1:]
    # drop a leading directional and trailing suffix so "W Pioneer Pkwy" == "Pioneer Pkwy W"
    rest = [t for t in rest if t not in {"N","S","E","W","NE","NW","SE","SW"}]
    if len(rest) > 1 and rest[-1] in set(SUF.values()): rest = rest[:-1]
    return num + " " + " ".join(rest)

ENTITY = re.compile(r"\b(LLC|L L C|LP|L P|LTD|INC|CORP|CORPORATION|CO|COMPANY|TRUST|PARTNERS|PARTNERSHIP|PTNRS|FUND|HOLDINGS|PROPERTIES|"
                    r"APARTMENTS|APTS|HOUSING|AUTHORITY|ASSOC|ASSOCIATION|VENTURE|REIT|GROUP|INVESTMENTS?|INVESTORS|OWNER|DEVELOPMENT|FOUNDATION|"
                    r"MINISTRIES|CHURCH|FACILITY|FINANCE|TITLEHOLDER|LIFESTYLE)\b")
MIN_VALUE_TO_UPB = 0.25   # value below 25% of loan balance = partial / under-construction assessment, not usable

# ---- Tarrant Appraisal District
t = pd.read_csv(tad_path, sep="|", dtype=str, keep_default_na=False)
print("TAD rows", len(t), "| appraisal year", list(t.Appraisal_Year.unique()))
t = t[t.State_Use_Code.str.startswith("BC") | t.Property_Class.str.startswith("BC")]
tad = pd.DataFrame({"k": t.Situs_Address.map(key), "val": pd.to_numeric(t.Appraised_Value, errors="coerce"),
                    "owner": t.Owner_Name.str.strip(), "acct": t.Account_Num, "situs": t.Situs_Address.str.replace(r"\s+", " ", regex=True)})
print("TAD apartment-class (BC) parcels", len(tad))

# ---- Dallas Central Appraisal District
ai = pd.read_csv(f"{dcad_dir}/ACCOUNT_INFO.CSV", dtype=str, keep_default_na=False,
                 usecols=["ACCOUNT_NUM", "DIVISION_CD", "OWNER_NAME1", "STREET_NUM", "FULL_STREET_NAME", "PROPERTY_CITY"])
ay = pd.read_csv(f"{dcad_dir}/ACCOUNT_APPRL_YEAR.CSV", dtype=str, keep_default_na=False,
                 usecols=["ACCOUNT_NUM", "APPRAISAL_YR", "TOT_VAL", "DIVISION_CD", "BLDG_CLASS_CD", "SPTD_CODE"])
print("DCAD appraisal years", list(ay.APPRAISAL_YR.unique()), "| COM SPTD sample", ay[ay.DIVISION_CD == "COM"].SPTD_CODE.value_counts().head(6).to_dict())
ay = ay[(ay.DIVISION_CD == "COM") & (ay.SPTD_CODE.str.startswith("B") | ay.BLDG_CLASS_CD.str.contains("APARTMENT", case=False))]
d = ay.merge(ai.drop(columns="DIVISION_CD"), on="ACCOUNT_NUM", how="left")
d["situs"] = (d.STREET_NUM.str.strip() + " " + d.FULL_STREET_NAME.str.strip()).str.replace(r"\s+", " ", regex=True)
dcad = pd.DataFrame({"k": d.situs.map(key), "val": pd.to_numeric(d.TOT_VAL, errors="coerce"), "owner": d.OWNER_NAME1.str.strip(),
                     "acct": d.ACCOUNT_NUM, "situs": d.situs})
print("DCAD apartment parcels (COM, SPTD B* or apartment class)", len(dcad), "| classes", d.BLDG_CLASS_CD.value_counts().head(4).to_dict())

def run(rows, parcels, src):
    idx = parcels.dropna(subset=["k"]).groupby("k")
    found, log = {}, []
    for r in rows:
        k = key(r.get("address"))
        if not k or k not in idx.groups: log.append((r["name"], r.get("address"), "no match")); continue
        g = idx.get_group(k)
        owners = [o for o in g.owner.unique().tolist() if o]
        owner = owners[0] if len(owners) == 1 and ENTITY.search(owners[0].upper()) else None   # entity names only
        mv = float(g.val.sum())
        if not r.get("upb") or mv < MIN_VALUE_TO_UPB * r["upb"]:
            log.append((r["name"], r["address"], f"REJECTED value ${mv:,.0f} vs upb ${r['upb']:,.0f}")); continue
        found[r["id"]] = {"marketValue": mv, "owner": owner, "cadSrc": src, "cadAccounts": g.acct.tolist(), "cadSitus": g.situs.iloc[0]}
        log.append((r["name"], r["address"], f"MATCH {g.situs.iloc[0]} | {len(g)} acct | ${mv:,.0f} vs upb ${r['upb']:,.0f} | {owners[:2]}"))
    for l in log: print("  ", src, l)
    return found

TARRANT_CITIES = {"FORT WORTH", "ARLINGTON", "MANSFIELD", "GRAND PRAIRIE"}
DALLAS_CITIES = {"DALLAS", "GRAND PRAIRIE", "MESQUITE", "GARLAND", "IRVING"}
multi = lambda r: (r["county"] or "").startswith("DFW")
t_rows = [r for r in rows if r["county"] == "Tarrant" or (multi(r) and (r["city"] or "").upper() in TARRANT_CITIES)]
d_rows = [r for r in rows if r["county"] == "Dallas" or (multi(r) and (r["city"] or "").upper() in DALLAS_CITIES)]
mt = run(t_rows, tad, "TAD"); md = run(d_rows, dcad, "DCAD")
matches = {**md, **mt}
# Loans that sit on the same parcel(s) share one appraised value: net all of their balances against it.
upb = {r["id"]: r["upb"] for r in rows}
groups = {}
for hid, m in matches.items(): groups.setdefault(tuple(sorted(m["cadAccounts"])), []).append(hid)
for ids in groups.values():
    for hid in ids:
        matches[hid]["lienTotal"] = sum(upb[i] for i in ids)
        matches[hid]["sharedWith"] = len(ids) - 1
        matches[hid].pop("cadAccounts", None)
print("shared-parcel groups:", [ids for ids in groups.values() if len(ids) > 1])
summary = {"tarrantCandidates": len(t_rows), "tarrantMatched": len(mt), "dallasCandidates": len(d_rows), "dallasMatched": len(md)}
print(summary)
json.dump({"matches": matches, "summary": summary}, open(out, "w"), indent=1)
