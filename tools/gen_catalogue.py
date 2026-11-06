"""Generates the SYNTHETIC product catalogue (data/catalogue/products.json).

Nothing here is any real firm's product list. A curated "core" set of families (stable product ids, used by
the golden-set calls) is followed by deterministic filler families so the matcher has to find the right fund among
~1,500 look-alikes, as it would in the real catalogue.

Run:  python tools\\gen_catalogue.py            (from the repo root, with the venv active)
"""
from __future__ import annotations

import itertools
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "catalogue" / "products.json"

CLASS_NAMES = {
    "A": "Class A", "C": "Class C", "Advisor": "Advisor Class", "R6": "Class R6", "R": "Class R", "I": "Class I",
    "ETF": "ETF", "W": "Class W",
}
# Variants as spoken / written (lower-case, EN + ES). Includes the usual speech-recogniser slips for the class letter
# ("class see") because a real catalogue phrase list would carry them. Bare "advisor" is deliberately NOT a variant.
SHARE_CLASSES = {
    "A": ["class a", "a shares", "a share", "class a shares", "series a", "clase a", "acciones clase a", "class eh",
          "clase ah", "a class"],
    "C": ["class c", "c shares", "c share", "class c shares", "series c", "clase c", "acciones clase c", "class see",
          "class sea", "see shares", "sea shares", "clase ce", "clase se", "c class"],
    "Advisor": ["advisor class", "class advisor", "advisor shares", "advisor share", "adviser class", "class adviser",
                "adviser shares", "clase advisor", "clase asesor", "acciones advisor"],
    "R6": ["class r6", "r6 shares", "r6 class", "r6", "r 6", "class r six", "class are six", "clase r6",
           "clase erre seis", "r six"],
    "R": ["class r", "r shares", "class are", "clase r", "clase erre", "retirement class", "r class"],
    "I": ["class i", "i shares", "institutional class", "institutional shares", "class eye", "clase i",
          "clase institucional", "acciones institucionales", "i class"],
    "ETF": ["etf", "e t f", "etf shares", "exchange traded fund", "exchange traded", "fondo cotizado"],
    "W": ["class w", "w shares", "w class", "clase w"],
}

EQ = ["A", "C", "Advisor", "R6", "I"]
EQ_RETAIL = ["A", "C", "Advisor", "R6"]
FI = ["A", "C", "Advisor", "R6"]
UCITS = ["A", "I", "W"]

# (family_id, fund_name, asset_class, strategy, region, vehicle, classes, extra aliases EN+ES)
CORE: list[tuple] = [
    ("FEQ", "Contoso Equity Fund", "Equity", "Large-cap core equity", "US", "Mutual Fund", EQ,
     ["Contoso Equity", "Contoso Large Cap Core", "Fondo Contoso Equity", "Fondo Contoso de Acciones"]),
    ("FEI", "Contoso Equity Income Fund", "Equity", "Large-cap value, income-oriented", "US", "Mutual Fund", EQ_RETAIL,
     ["Contoso Equity Income", "Fondo Contoso Equity Income", "Fondo Contoso de Acciones con Ingresos"]),
    ("FEO", "Contoso Equity Opportunities Fund", "Equity", "All-cap opportunistic equity", "US", "Mutual Fund", EQ_RETAIL,
     ["Contoso Equity Opportunities"]),
    ("FGR", "Contoso Growth Fund", "Equity", "Large-cap growth", "US", "Mutual Fund", EQ, ["Contoso Growth"]),
    ("FGO", "Contoso Growth Opportunities Fund", "Equity", "Mid/large-cap growth", "US", "Mutual Fund", EQ_RETAIL,
     ["Contoso Growth Opportunities"]),
    ("FGA", "Contoso Growth Allocation Fund", "Multi-Asset", "Growth-oriented asset allocation", "US", "Mutual Fund",
     EQ_RETAIL, ["Contoso Growth Allocation"]),
    ("FDT", "Contoso Dynamic Technology Fund", "Equity", "Technology sector equity", "US", "Mutual Fund", EQ_RETAIL,
     ["Contoso Dynamic Tech"]),
    ("FSV", "Contoso Small Cap Value Fund", "Equity", "Small-cap value", "US", "Mutual Fund", EQ, ["Contoso Small Cap Value"]),
    ("FSG", "Contoso Small-Mid Cap Growth Fund", "Equity", "Small/mid-cap growth", "US", "Mutual Fund", EQ,
     ["Contoso Small Mid Cap Growth", "Contoso SMID Growth"]),
    ("FMV", "Contoso Mid Cap Value Fund", "Equity", "Mid-cap value", "US", "Mutual Fund", EQ, ["Contoso Mid Cap Value"]),
    ("FDG", "Contoso Dividend Growth Fund", "Equity", "Dividend growers", "US", "Mutual Fund", EQ_RETAIL,
     ["Contoso Dividend Growth"]),
    ("FSE", "Contoso Sustainable Equity Fund", "Equity", "ESG-integrated large-cap equity", "US", "Mutual Fund", EQ_RETAIL,
     ["Contoso Sustainable Equity", "Contoso ESG Equity"]),
    ("FCE", "Contoso U.S. Core Equity ETF", "Equity", "Broad US equity, rules-based", "US", "ETF", ["ETF"],
     ["Contoso US Core Equity ETF", "Contoso Core Equity ETF"]),
    ("TGE", "Woodgrove Global Equity Fund", "Equity", "Global value equity", "Global", "Mutual Fund", EQ_RETAIL,
     ["Woodgrove Global Equity", "Fondo Woodgrove de Acciones Globales"]),
    ("TEM", "Woodgrove Emerging Markets Equity Fund", "Equity", "Emerging markets equity", "Emerging Markets", "Mutual Fund",
     EQ, ["Woodgrove Emerging Markets Equity", "Woodgrove EM Equity", "Fondo Woodgrove de Mercados Emergentes"]),
    ("TFO", "Woodgrove Foreign Fund", "Equity", "Non-US developed and emerging equity", "International", "Mutual Fund",
     EQ_RETAIL, ["Woodgrove Foreign"]),
    ("FIE", "Contoso International Equity Fund", "Equity", "International developed equity", "International", "Mutual Fund",
     EQ_RETAIL, ["Contoso International Equity", "Fondo Contoso de Acciones Internacionales"]),
    ("FIG", "Contoso International Growth Fund", "Equity", "International growth equity", "International", "Mutual Fund",
     EQ_RETAIL, ["Contoso International Growth"]),
    ("FGL", "Contoso Global Opportunities Fund", "Equity", "Global opportunistic equity", "Global", "Mutual Fund", EQ_RETAIL,
     ["Contoso Global Opportunities"]),
    ("FAP", "Contoso Asia Pacific Equity Fund", "Equity", "Asia-Pacific equity", "Asia Pacific", "Mutual Fund", EQ_RETAIL,
     ["Contoso Asia Pacific Equity", "Contoso APAC Equity"]),
    ("FIN", "Contoso Income Fund", "Multi-Asset", "Multi-asset income", "US", "Mutual Fund", EQ,
     ["Contoso Income", "Fondo Contoso de Ingresos"]),
    ("FSI", "Contoso Strategic Income Fund", "Fixed Income", "Multi-sector strategic income", "Global", "Mutual Fund", EQ,
     ["Contoso Strategic Income"]),
    ("FTR", "Contoso Total Return Fund", "Fixed Income", "Core plus bond", "US", "Mutual Fund", EQ, ["Contoso Total Return"]),
    ("FLD", "Contoso Low Duration Bond Fund", "Fixed Income", "Low-duration investment-grade", "US", "Mutual Fund", FI,
     ["Contoso Low Duration Bond", "Contoso Low Duration"]),
    ("FSD", "Contoso Short Duration Income Fund", "Fixed Income", "Short-duration income", "US", "Mutual Fund", FI,
     ["Contoso Short Duration Income", "Contoso Short Duration"]),
    ("FHY", "Contoso High Yield Fund", "Fixed Income", "High-yield corporate credit", "US", "Mutual Fund", EQ,
     ["Contoso High Yield", "Fondo Contoso de Alto Rendimiento"]),
    ("FFR", "Contoso Floating Rate Fund", "Fixed Income", "Senior loans", "US", "Mutual Fund", FI, ["Contoso Floating Rate"]),
    ("FTX", "Contoso Federal Tax-Free Income Fund", "Fixed Income", "National municipal bonds", "US", "Mutual Fund", FI,
     ["Contoso Federal Tax Free Income", "Contoso Federal Tax-Free"]),
    ("FCA", "Contoso California Tax-Free Income Fund", "Fixed Income", "California municipal bonds", "US", "Mutual Fund", FI,
     ["Contoso California Tax Free Income", "Contoso California Tax-Free"]),
    ("FMU", "Contoso Municipal Bond Fund", "Fixed Income", "Intermediate municipal bonds", "US", "Mutual Fund", FI,
     ["Contoso Municipal Bond", "Contoso Muni Bond"]),
    ("TGB", "Woodgrove Global Bond Fund", "Fixed Income", "Global unconstrained bonds", "Global", "Mutual Fund", EQ,
     ["Woodgrove Global Bond", "Fondo Woodgrove de Bonos Globales"]),
    ("TEB", "Woodgrove Emerging Markets Bond Fund", "Fixed Income", "Emerging-markets debt", "Emerging Markets", "Mutual Fund",
     EQ_RETAIL, ["Woodgrove Emerging Markets Bond", "Woodgrove EM Bond"]),
    ("FGS", "Contoso U.S. Government Securities Fund", "Fixed Income", "US government and agency securities", "US",
     "Mutual Fund", FI, ["Contoso US Government Securities", "Contoso Government Securities"]),
    ("FIP", "Contoso Inflation-Protected Bond Fund", "Fixed Income", "TIPS and inflation-linked", "US", "Mutual Fund", FI,
     ["Contoso Inflation Protected Bond", "Contoso TIPS Fund"]),
    ("FMA", "Contoso Moderate Allocation Fund", "Multi-Asset", "Moderate risk asset allocation", "US", "Mutual Fund", EQ_RETAIL,
     ["Contoso Moderate Allocation"]),
    ("FCO", "Contoso Conservative Allocation Fund", "Multi-Asset", "Conservative asset allocation", "US", "Mutual Fund",
     EQ_RETAIL, ["Contoso Conservative Allocation"]),
    ("FAG", "Contoso Aggressive Allocation Fund", "Multi-Asset", "Aggressive asset allocation", "US", "Mutual Fund", EQ_RETAIL,
     ["Contoso Aggressive Allocation"]),
    ("FT40", "Contoso Target Retirement 2040 Fund", "Multi-Asset", "Target-date 2040", "US", "Mutual Fund", ["A", "R", "R6", "I"],
     ["Contoso Target 2040", "Contoso 2040 Target Date"]),
    ("FT45", "Contoso Target Retirement 2045 Fund", "Multi-Asset", "Target-date 2045", "US", "Mutual Fund", ["A", "R", "R6", "I"],
     ["Contoso Target 2045", "Contoso 2045 Target Date"]),
    ("FT50", "Contoso Target Retirement 2050 Fund", "Multi-Asset", "Target-date 2050", "US", "Mutual Fund", ["A", "R", "R6", "I"],
     ["Contoso Target 2050", "Contoso 2050 Target Date"]),
    ("FMN", "Contoso Market Neutral Fund", "Alternatives", "Equity market neutral", "US", "Mutual Fund", EQ_RETAIL,
     ["Contoso Market Neutral"]),
    ("FRA", "Contoso Real Assets Fund", "Alternatives", "Real assets and inflation hedges", "Global", "Mutual Fund", EQ_RETAIL,
     ["Contoso Real Assets"]),
    ("FLS", "Contoso Long/Short Equity Fund", "Alternatives", "Equity long/short", "US", "Mutual Fund", EQ_RETAIL,
     ["Contoso Long Short Equity", "Contoso Long-Short Equity"]),
    ("UGE", "Contoso Global Equity UCITS Fund", "Equity", "Global equity (Luxembourg UCITS)", "Global", "UCITS", UCITS,
     ["Contoso Global Equity UCITS"]),
    ("UEB", "Contoso Euro Bond UCITS Fund", "Fixed Income", "Euro investment-grade bonds (Luxembourg UCITS)", "Europe", "UCITS",
     UCITS, ["Contoso Euro Bond UCITS"]),
    ("UEM", "Contoso Emerging Markets Equity UCITS Fund", "Equity", "Emerging markets equity (Luxembourg UCITS)",
     "Emerging Markets", "UCITS", UCITS, ["Contoso Emerging Markets Equity UCITS"]),
]

FILLER_EQUITY_UNIVERSE = [
    "Large Cap", "Mid Cap", "Small Cap", "All Cap", "SMID Cap", "Global", "International", "Emerging Markets", "Europe",
    "Japan", "Asia Pacific", "Latin America", "China", "India", "Healthcare", "Technology", "Financial Services",
    "Energy Transition", "Infrastructure", "Real Estate", "Consumer", "Industrials", "Utilities",
]
FILLER_STYLES = ["Growth", "Value", "Core", "Opportunities", "Select", "Quality", "Focus", "Dividend", "Momentum",
                 "Sustainable", "Dynamic", "Income", "Blend"]
FILLER_FI = [
    "Core Bond", "Short-Term Bond", "Intermediate Bond", "Long-Term Bond", "High Yield Municipal", "Investment Grade Credit",
    "Senior Loan", "New York Municipal", "Treasury", "Mortgage", "Global Bond", "Emerging Markets Debt", "Convertible",
    "Corporate Income", "Securitized Income", "Ultra-Short Duration", "Credit Opportunities", "Tax-Exempt Income",
]
FILLER_MA = ["Conservative Growth Allocation", "Balanced", "Income Allocation", "Target Retirement 2030",
             "Target Retirement 2035", "Target Retirement 2055", "Target Retirement 2060", "Target Retirement 2065",
             "Multi-Asset Real Return", "Global Allocation"]
FILLER_ALT = ["Managed Futures", "Merger Arbitrage", "Alternative Income", "Global Macro", "Absolute Return Credit"]

FILLER_FAMILIES = 280


def _brand(rng: random.Random, universe: str) -> str:
    international = universe in {"Global", "International", "Emerging Markets", "Europe", "Japan", "Asia Pacific",
                                 "Latin America", "China", "India", "Global Bond", "Emerging Markets Debt"}
    return "Woodgrove" if international and rng.random() < 0.55 else "Contoso"


def _filler(rng: random.Random, taken: set[str]) -> list[tuple]:
    out: list[tuple] = []
    candidates: list[tuple[str, str, str, str, list[str]]] = []
    for universe, style in itertools.product(FILLER_EQUITY_UNIVERSE, FILLER_STYLES):
        candidates.append(("Equity", universe, style, f"{universe} {style}", rng.choice([EQ, EQ_RETAIL, EQ_RETAIL])))
    for name in FILLER_FI:
        candidates.append(("Fixed Income", name, "", name, rng.choice([FI, EQ, FI])))
    for name in FILLER_MA:
        candidates.append(("Multi-Asset", name, "", name, rng.choice([["A", "R", "R6", "I"], EQ_RETAIL])))
    for name in FILLER_ALT:
        candidates.append(("Alternatives", name, "", name, EQ_RETAIL))
    rng.shuffle(candidates)
    for asset_class, universe, style, label, classes in candidates:
        name = f"{_brand(rng, universe)} {label} Fund"
        if name in taken:
            continue
        taken.add(name)
        region = ("Emerging Markets" if "Emerging" in universe else "Global" if "Global" in universe
                  else "International" if universe in {"International", "Europe", "Japan", "Asia Pacific", "Latin America",
                                                       "China", "India"} else "US")
        fid = "X%03d" % (len(out) + 1)
        out.append((fid, name, asset_class, label if not style else f"{universe} equity, {style.lower()} style", region,
                    "Mutual Fund", classes, []))
        if len(out) >= FILLER_FAMILIES:
            break
    return out


def build() -> dict:
    rng = random.Random(20261103)
    taken = {row[1] for row in CORE}
    families = [*CORE, *_filler(rng, taken)]
    products: list[dict] = []
    seq = 1
    for fid, fund, asset_class, strategy, region, vehicle, classes, aliases in families:
        business = ["international", "professional_buyer"] if vehicle == "UCITS" else ["professional_buyer", "retail"]
        for cls in classes:
            display = f"{fund} {CLASS_NAMES[cls]}" if cls != "ETF" else fund
            products.append({
                "product_id": f"SYN-{seq:06d}", "family_id": fid, "fund_name": fund, "share_class": cls,
                "display_name": display, "asset_class": asset_class, "strategy": strategy, "region": region,
                "vehicle": vehicle, "business_lines": business, "aliases": aliases, "status": "active", "synthetic": True,
            })
            seq += 1
    return {
        "version": "2026.11-synthetic", "synthetic": True,
        "note": "SYNTHETIC illustrative catalogue - not any real firm's product list. Regenerate with tools/gen_catalogue.py.",
        "share_classes": SHARE_CLASSES, "products": products,
    }


def main() -> None:
    data = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({k: v for k, v in data.items() if k != "products"}, ensure_ascii=False)[:-1] + ', "products": [']
    lines.extend(json.dumps(p, ensure_ascii=False) + ("," if i < len(data["products"]) - 1 else "")
                 for i, p in enumerate(data["products"]))
    lines.append("]}")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    fams = {p["family_id"] for p in data["products"]}
    print(f"wrote {OUT} - {len(data['products'])} products in {len(fams)} families")


if __name__ == "__main__":
    main()
