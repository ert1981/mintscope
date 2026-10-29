"""Evaluate MintScope provider+analyzer against real OpenSea data (keyless cached endpoints).
Usage: python scripts/eval_live.py .   (set OPENSEA_API_KEY for full coverage; without it only CDN-cached collections answer)"""
import os, sys, json, time, statistics, requests
proj = sys.argv[1]; sys.path.insert(0, proj)
import providers, analyzer
BASE = "https://api.opensea.io/api/v2"
API_KEY = os.environ.get("OPENSEA_API_KEY", "")  # optional; never printed
HDR = {"accept": "application/json", **({"x-api-key": API_KEY} if API_KEY else {})}
SLUGS = ["boredapeyachtclub", "pudgypenguins", "azuki", "doodles-official", "cryptopunks", "mutant-ape-yacht-club",
         "chromie-squiggle-by-snowfro", "fidenza-by-tyler-hobbs", "ringers-by-dmitri-cherniak", "milady", "clonex", "moonbirds",
         "art-blocks-curated", "basepaint", "the-memes-by-6529"]
CONTRACTS = [("0xbc4ca0eda7647a8ab7c2061c2e118a18a936f13d", "ethereum"), ("0xbd3531da5cf5857e7cfaa92426877b022e612cf8", "ethereum")]
def raw(path):
    r = requests.get(BASE + path, headers=HDR, timeout=15); return r.status_code, (r.json() if r.headers.get("content-type","").startswith("application/json") else None)
rows, lat = [], []
for ident, chain in [(s, "ethereum") for s in SLUGS] + CONTRACTS:
    p = providers.OpenSeaProvider(API_KEY, BASE, 15)
    t = time.perf_counter()
    try:
        out = p.fetch(ident, chain); err = None
    except Exception as e:
        out, err = None, f"{type(e).__name__}: {e}"
    dt = time.perf_counter() - t
    if out is None:
        rows.append({"id": ident, "error": err, "latency_s": round(dt, 2)}); continue
    lat.append(dt)
    f = out["fields"]
    slug = ident if not ident.startswith("0x") else raw(f"/chain/{chain}/contract/{ident}")[1]["collection"]
    _, col = raw(f"/collections/{slug}"); _, st = raw(f"/collections/{slug}/stats")
    tot = st["total"]; sev = next((i for i in st["intervals"] if i["interval"] == "seven_day"), {})
    truth = {"name": col.get("name"), "floor_price": tot.get("floor_price"), "total_sales": tot.get("sales"),
             "total_volume": tot.get("volume"), "num_owners": tot.get("num_owners"), "total_supply": col.get("total_supply"),
             "sales_7d": sev.get("sales"), "volume_7d": sev.get("volume"), "safelist_status": col.get("safelist_status")}
    mism = {k: (f.get(k), v) for k, v in truth.items() if f.get(k) != v and not (isinstance(v, float) and f.get(k) is not None and abs(f[k]-v) < 1e-6*max(1,abs(v)))}
    rep = analyzer.build_report(out, ident, providers.classify_identifier(ident), chain, "live")
    rows.append({"id": ident, "latency_s": round(dt, 2), "calls": out["requests_made"], "mismatches": mism,
                 "avg_sale_price": f.get("average_sale_price"), "price_symbol": f.get("price_symbol"),
                 "floor_symbol": tot.get("floor_price_symbol"), "volume_symbol": tot.get("volume_symbol"),
                 "score": rep["score"]["value"], "coverage": rep["score"]["coverage"], "confidence": rep["confidence"]["level"],
                 "unknown": len(rep["missing_data"]), "flags": [x["code"] for x in rep["risk_flags"]]})
print(json.dumps(rows, indent=1))
ok = [r for r in rows if "error" not in r]
print("SUMMARY collections:", len(rows), "ok:", len(ok), "errors:", len(rows) - len(ok))
if lat: print("latency s: median %.2f max %.2f" % (statistics.median(lat), max(lat)))
print("field mismatches:", sum(len(r["mismatches"]) for r in ok), "avg_sale_price known:", sum(r["avg_sale_price"] is not None for r in ok), "/", len(ok))
