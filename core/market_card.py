"""A useful text result before the price image; all facts come from live data."""
import math
from datetime import date

from .market_choices import canonical_name, menu_lines
from .market_sources import enrich_product


def _price(value):
    try:
        number = float(value)
        return f'{number:,.2f}'.rstrip('0').rstrip('.') if math.isfinite(number) and number > 0 else ''
    except (TypeError, ValueError):
        return ''


def _line(value):
    return ' '.join(str(value or '').split())


def market_card(options, data=None, detail=None, prefix='', selectable=True):
    first = next(iter(options))
    name = canonical_name(options[first])
    # A provider may resolve an alias to another edition. Its facts must never
    # appear under the edition the user actually selected.
    raw = data if isinstance(data, dict) and data.get('name') == name else {}
    extra = detail if isinstance(detail, dict) and detail.get('goodsName') == name else {}
    product = enrich_product(raw, extra)
    lines = [f'默认 {first}：{name}']
    alias = options[first].partition('｜')[2]
    if alias:
        lines.append(f'别名：{_line(alias)}')
    basics = []
    retail = _price(product.get('retail'))
    if retail:
        basics.append(f'官方售价 ¥{retail}')
    if product.get('date'):
        basics.append(f"发售 {_line(product['date'])}")
    if basics:
        lines.append(' · '.join(basics))
    limits = [f'{label} {_line(product[key])}' for label, key in [
        ('不绑定限时', 'unbound_duration'), ('绑定限时', 'bound_duration'),
        ('不绑定限量', 'unbound_limit')
    ] if product.get(key) not in (None, '')]
    if limits:
        lines.append(' · '.join(limits))
    # Compare listings from the newest observation date, never an old low.
    for group in raw.get('list') or []:
        if not isinstance(group, dict) or group.get('name') != '在售期':
            continue
        rows = []
        for row in group.get('list') or []:
            if not isinstance(row, dict) or not _price(row.get('value')):
                continue
            try:
                day = date.fromisoformat(str(row.get('date') or '')[:10]).isoformat()
            except ValueError:
                continue
            rows.append((day, float(row['value'])))
        if rows:
            latest = max(day for day, _ in rows)
            low = min(value for day, value in rows if day == latest)
            lines.append(f'万宝楼在售 ¥{_price(low)} 起（{latest}）')
    lines += ['', '可选款式', *menu_lines(options), '']
    if selectable:
        lines.append('私聊回复序号，群聊 @我回复序号；2 分钟内有效。')
        lines.append(f'也可发送「{prefix}物价选2」换款。')
    else:
        lines.append(f'查询其他款请发送：{prefix}物价 完整外观名')
    lines.append('默认款完整物价图随后发出。')
    return '\n'.join(lines)
