"""Build the DFW Capital Event Radar dataset from HUD public files.
Usage: python3 -I build_data.py <download_dir> <out_json>
"""
import json, sys, warnings
import pandas as pd
warnings.filterwarnings("ignore")
D, OUT = sys.argv[1], sys.argv[2]

DFW_COUNTIES = ["Dallas", "Tarrant", "Collin", "Denton", "Johnson", "Ellis", "Rockwall", "Kaufman", "Parker"]
counts = []
def step(label, df):
    counts.append({"step": label, "rows": int(len(df))}); print(f"{len(df):>6}  {label}")
    return df

def clean(df):
    for c in df.columns:
        if df[c].dtype == object or str(df[c].dtype) == "str":
            df[c] = df[c].astype("string").str.strip().replace("", pd.NA)
    return df

m = clean(pd.read_excel(f"{D}/FHA-BF90-RM-A.xlsx", header=1, dtype={"HUD PROJECT NUMBER": str, "PROPERTY ZIP": str}))
a = clean(pd.read_excel(f"{D}/addr.xlsx", dtype=str))
p = clean(pd.read_excel(f"{D}/portfolio.xlsx", dtype=str))

step("All active HUD-insured multifamily mortgages (national)", m)
m = step("Texas (PROPERTY STATE = TX)", m[m["PROPERTY STATE"] == "TX"].copy())

# FHA number -> property_id (address file) -> county (portfolio file)
a1 = a[a.state_code == "TX"].sort_values("is_primary_fha_ind", ascending=False).drop_duplicates("fha_number")
m = m.merge(a1[["fha_number", "property_id", "address_line1_text", "zip_code"]],
            left_on="HUD PROJECT NUMBER", right_on="fha_number", how="left")
pc = p[p.state_code == "TX"][["property_id", "county_name_text", "msa_name_text"]].drop_duplicates("property_id")
m = m.merge(pc, on="property_id", how="left")
print("  TX loans with county via HUD join:", m.county_name_text.notna().sum(), "/", len(m))

# Fallback for loans with no county match: use the city. Build city -> set of counties from matched TX loans.
m["CITY_U"] = m["PROPERTY CITY"].str.upper().str.strip()
m["county_source"] = m.county_name_text.notna().map({True: "HUD portfolio file", False: None})
city_counties = m.dropna(subset=["county_name_text"]).groupby("CITY_U")["county_name_text"].agg(lambda s: set(s))
# Manual DFW city list for cities with no matched loan to learn from (Springtown is in Parker County).
MANUAL_DFW_CITY = {"SPRINGTOWN": "Parker", "MANSFIELD": "DFW (multi-county city)"}
def infer(city):
    cs = city_counties.get(city)
    if city in MANUAL_DFW_CITY: return MANUAL_DFW_CITY[city], "DFW city list"
    if cs:
        if len(cs) == 1: return next(iter(cs)), "inferred from city"
        if cs <= set(DFW_COUNTIES): return "DFW (multi-county city)", "inferred from city"
        return None, None
    if city in MANUAL_DFW_CITY: return MANUAL_DFW_CITY[city], "DFW city list"
    return None, None
miss = m.county_name_text.isna()
for i in m.index[miss]:
    c, src = infer(m.at[i, "CITY_U"])
    m.at[i, "county_name_text"], m.at[i, "county_source"] = c, src
print("  county filled from city:", int((miss & m.county_name_text.notna()).sum()), " still missing:", int(m.county_name_text.isna().sum()))
print("  Mansfield counties seen:", city_counties.get("MANSFIELD"))
print("  unresolved cities:", sorted(m.loc[m.county_name_text.isna(), "CITY_U"].dropna().unique().tolist()))
print("  inferred:", m.loc[miss & m.county_name_text.notna(), ["CITY_U","county_name_text"]].value_counts().to_dict())
DFW_COUNTIES_PLUS = DFW_COUNTIES + ["DFW (multi-county city)"]
m = step("DFW counties (" + ", ".join(DFW_COUNTIES) + ")", m[m.county_name_text.isin(DFW_COUNTIES_PLUS)].copy())
m = step("Residential only (BUSINESS_TYPE = MF Residential; drops hospitals, nursing, assisted living, board & care)",
         m[m.BUSINESS_TYPE == "MF Residential"].copy())
soa = m["SOA CATEGORY/SUB CATEGORY"].fillna("")
bad = soa.str.contains(r"COOP|MOBILE HOME|NURSING|HOSPITAL|ASSIST|BOARD|232|242", case=False, regex=True)
print("  dropped by SOA keyword:", m.loc[bad, "SOA CATEGORY/SUB CATEGORY"].value_counts().to_dict())
m = step("Apartments only (also drop co-ops, mobile-home courts, any remaining care SOA codes)", m[~bad].copy())
print(m.county_name_text.value_counts().to_dict())

def d(x): return None if pd.isna(x) else pd.Timestamp(x).strftime("%Y-%m-%d")
def num(x): return None if pd.isna(x) else float(x)
rows = []
for _, r in m.iterrows():
    rows.append({
        "id": r["HUD PROJECT NUMBER"], "name": r["PROPERTY NAME"], "city": r["PROPERTY CITY"],
        "county": r["county_name_text"], "countySrc": r["county_source"],
        "address": None if pd.isna(r["address_line1_text"]) else r["address_line1_text"],
        "zip": r["PROPERTY ZIP"], "units": None if pd.isna(r["UNITS"]) else int(r["UNITS"]),
        "endorsed": d(r["INITIAL ENDORSEMENT DATE"]), "finalEndorsed": d(r["FINAL ENDORSEMENT DATE"]),
        "firstPmt": d(r["FIRST PAYMENT DATE"]), "maturity": d(r["MATURITY DATE"]),
        "origAmt": num(r["ORIGINAL MORTGAGE AMOUNT"]), "rate": num(r["INTEREST RATE"]),
        "pi": num(r["CURRENT PRINCIPAL AND INTEREST"]), "upb": num(r["AMORITIZED PRINCIPAL BALANCE"]),
        "termMo": None if pd.isna(r["TERM IN MONTHS"]) else int(r["TERM IN MONTHS"]),
        "program": r["SOA CATEGORY/SUB CATEGORY"], "servicer": r["SERVICER NAME"],
        "taxExempt": bool(r["TE"] == "TE") if not pd.isna(r["TE"]) else False,
        "lihtc": bool(r["TC"] == "TC") if not pd.isna(r["TC"]) else False,
    })
print("TE values:", m.TE.value_counts(dropna=False).to_dict(), "TC values:", m.TC.value_counts(dropna=False).to_dict())
json.dump({"counts": counts, "rows": rows}, open(OUT, "w"), indent=0)
