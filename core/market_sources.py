"""Read-only public market history; credentials from JX3API never leave that client."""
import asyncio
import math
from datetime import datetime
from zoneinfo import ZoneInfo

from .request import APIClient

SHANGHAI = ZoneInfo("Asia/Shanghai")


def source_day(value):
    """Provider timestamps are UTC; display their actual China calendar day."""
    try:
        if isinstance(value, (int, float)):
            stamp = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(stamp, SHANGHAI).date().isoformat()
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo:
            stamp = stamp.astimezone(SHANGHAI)
        return stamp.date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def positive_price(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result > 0 else None
    except (TypeError, ValueError):
        return None


def history_series(reference, statistics, name):
    """Preserve price basis and sample counts for the reference-price merger."""
    definitions = [
        ("爱剑三报价均价", reference, "tradeTime", "price", "reference_quote", None),
        ("万宝楼在售均价", statistics, "countDate", "saleAvgPrice", "listing_mean", "saleAvgNum"),
        ("万宝楼上架均价", statistics, "countDate", "publicAvgPrice", "public_listing", "publicNum"),
        ("万宝楼成交均价", statistics, "countDate", "tradeAvgPrice", "transaction", "tradeAvgNum"),
    ]
    series = []
    for label, rows, date_key, price_key, kind, count_key in definitions:
        daily = {}
        for row in rows or []:
            if not isinstance(row, dict) or row.get("goodsName", name) != name:
                continue
            day, price = source_day(row.get(date_key)), positive_price(row.get(price_key))
            count = row.get(count_key) if count_key else None
            if count is not None and positive_price(count) is None:
                continue
            if day and price is not None:
                daily[day] = dict(date=day, value=price, count=count)
        if daily:
            series.append(dict(label=label, color="var(--cinnabar)", dash="",
                               kind=kind, provider="爱剑三", origin="万宝楼" if count_key else "爱剑三",
                               points=[point for _, point in sorted(daily.items())]))
    return series


def enrich_product(data, detail):
    """Add verified release facts while keeping the original full product image."""
    result = dict(data)
    for field, source in [("unbound_duration", "publishLimitTime"),
                          ("bound_duration", "limitTime"), ("unbound_limit", "publishNum")]:
        value = detail.get(source)
        if value not in (None, "") and not result.get(field):
            result[field] = str(value)
    if not result.get("retail") and positive_price(detail.get("priceNum")):
        result["retail"] = detail["priceNum"]
    if len(str(result.get("date") or "")) < 10:
        result["date"] = source_day(detail.get("publishTime")) or result.get("date", "")
    return result


class AiJX3MarketSource:
    def __init__(self, cache=None, ssl_verify=True):
        self.cache = cache
        self.client = APIClient(base_timeout=3, ssl_verify=ssl_verify)
        self.slots = asyncio.Semaphore(4)

    async def close(self):
        await self.client.close()

    async def _request(self, service, path, params):
        async def fetch():
            async with self.slots:
                return await self.client.post(
                    f"https://www.aijx3.cn/api2/{service}{path}",
                    data=params, out_key="data", success_codes=("0", 0),
                )
        if self.cache is None:
            return await fetch()
        data, _ = await self.cache.request_api(
            f"aijx3:{service}{path}", params, fetch,
            lambda value: isinstance(value, (list, dict)),
        )
        return data

    async def get(self, name):
        """An optional source has a total time budget and never blocks the primary result."""
        try:
            async with asyncio.timeout(2.5):
                return await self._get(name)
        except Exception:
            # No provider errors or internal diagnostics enter the finished image.
            return {}

    async def detail(self, name):
        detail = await self._request("aijx3-wj", "/goods/getGoodsDetail", {"goodsName": name})
        if not isinstance(detail, dict) or detail.get("goodsName") != name:
            return {}
        return detail

    async def _get(self, name):
        detail = await self.detail(name)
        if not detail:
            return {}

        async def statistics():
            goods_id = detail.get("goodsId")
            if not str(goods_id or "").isdigit():
                return []
            return await self._request("aijx3-wblwg", "/goodsStatistics",
                                       {"goodsId": int(goods_id), "countType": 1})

        # Omit start/end times exactly as the provider's "全部" selection does.
        results = await asyncio.gather(
            self._request("aijx3-wj", "/goods/getAvgGoodsPriceRecord",
                          {"goodsName": name, "belongQf3": ""}),
            statistics(), return_exceptions=True,
        )
        reference, stats = [r if isinstance(r, list) else [] for r in results]
        return {"product": detail, "series": history_series(reference, stats, name)}
