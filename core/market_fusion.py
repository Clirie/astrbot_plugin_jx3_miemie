"""Build one reference-price timeline from compatible, dated observations.

Daily listing statistics take precedence over limited listing snapshots. Quotes
fill older periods; confirmed transactions are the last fallback. Buy orders do
not enter the reference curve. Every output point keeps its actual provenance.
"""
from datetime import date
from math import isfinite
from statistics import median


KINDS = {
    "listing_mean": 0,
    "listing_snapshot": 1,
    "reference_quote": 2,
    "sell_quote": 2,
    "public_listing": 3,
    "transaction": 4,
}
LEGACY_KINDS = {
    "万宝楼在售均价": "listing_mean",
    "在售": "listing_snapshot",
    "爱剑三报价均价": "reference_quote",
    "出售报价": "sell_quote",
    "公示": "public_listing",
    "万宝楼上架均价": "public_listing",
    "成交": "transaction",
    "万宝楼成交均价": "transaction",
}


def fuse_history(series):
    """Use the best available price basis each day, never synthesize missing days."""
    daily = {}
    seen = set()
    for item in series:
        kind = item.get("kind") or LEGACY_KINDS.get(item.get("label"))
        if kind not in KINDS:
            continue
        provider = item.get("provider") or "JX3API"
        origin = item.get("origin") or provider
        for point in item.get("points") or []:
            try:
                day = date.fromisoformat(point["date"]).isoformat()
                value = float(point["value"])
            except (KeyError, ValueError, TypeError):
                continue
            if not isfinite(value) or value <= 0:
                continue
            # A retained historical average on a no-trade day is not a new trade.
            if kind == "transaction" and point.get("count") is not None:
                try:
                    if float(point["count"]) <= 0:
                        continue
                except (ValueError, TypeError):
                    continue
            identity = (day, origin, kind, value)
            if identity in seen:
                continue
            seen.add(identity)
            daily.setdefault(day, []).append({
                "date": day, "value": value, "kind": kind, "provider": provider,
                "origin": origin, "priority": KINDS[kind],
            })
    points = []
    providers = set()
    kinds = set()
    for day, rows in sorted(daily.items()):
        priority = min(row["priority"] for row in rows)
        selected = [row for row in rows if row["priority"] == priority]
        # One vote per distinct quote avoids repeated provider copies dominating.
        value = median(sorted({row["value"] for row in selected}))
        sources = sorted({row["provider"] for row in selected})
        bases = sorted({row["kind"] for row in selected})
        points.append({"date": day, "value": value, "sources": sources,
                       "basis": bases, "observations": selected})
        providers.update(sources)
        kinds.update(bases)
    return {"label": "市场参考价", "kind": "reference", "points": points,
            "color": "var(--cinnabar)", "dash": "",
            "providers": sorted(providers), "bases": sorted(kinds)}
