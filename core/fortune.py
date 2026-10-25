"""Offline entertainment: coherent whole pages and explicitly virtual births."""
import asyncio
import json
import secrets
import time
from collections import Counter, OrderedDict, deque
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from ..vendor.lunar_python import Solar
from .template import load_template

ROOT = Path(__file__).resolve().parents[1]
THEMES = ('相遇', '暧昧', '相处', '争执', '告别', '自己')
ALIASES = {'相识': '相遇', '单身': '相遇', '暗恋': '暧昧', '恋爱': '相处',
           '吵架': '争执', '分手': '告别', '失恋': '告别', '自我': '自己'}
NAYIN_ALIASES = {'井泉水': '泉中水', '砂中金': '沙中金', '砂中土': '沙中土',
                '白鑞金': '白蜡金', '佛灯火': '覆灯火', '复灯火': '覆灯火', '桑拓木': '桑柘木'}


@lru_cache(maxsize=2)
def library(name):
    expected = {'nayin': 30, 'love': 48, 'love_answers': 60}
    if name not in expected:
        raise ValueError('未知内容库')
    rows = json.loads((ROOT / 'data/fortune' / (name + '.json')).read_text(encoding='utf-8'))
    if len(rows) != expected[name]:
        raise ValueError('内容库不完整')
    key = 'name' if name == 'nayin' else 'id'
    if len({row[key] for row in rows}) != len(rows):
        raise ValueError('内容编号重复')
    return tuple(rows)


class LoveBook:
    """Only recent page IDs are kept; no questions, birthdays or persistent history."""
    def __init__(self, rng=None, clock=time.monotonic, library_name='love'):
        self.library_name = library_name
        self.rng = rng or secrets.SystemRandom()
        self.clock = clock
        self.recent = OrderedDict()

    def draw(self, theme='', owner=''):
        candidates = [row for row in library(self.library_name) if not theme or row['theme'] == theme]
        if not candidates:
            raise ValueError('未知主题')
        now = self.clock()
        # LRU order is also expiry order; memory use stays bounded on busy bots.
        while self.recent and next(iter(self.recent.values()))[0] <= now:
            self.recent.popitem(last=False)
        key = str(owner)
        history = self.recent.pop(key, (0, deque(maxlen=12)))[1] if key else deque(maxlen=12)
        available = [row for row in candidates if row['id'] not in history]
        while not available:
            history.popleft()
            available = [row for row in candidates if row['id'] not in history]
        row = self.rng.choice(available)
        if key:
            history.append(row['id'])
            self.recent[key] = (now + 1800, history)
            while len(self.recent) > 512:
                self.recent.popitem(last=False)
        return dict(row)


def make_fortune(rng=None, instant=None):
    random = rng or secrets.SystemRandom()
    start, end = datetime(1980, 1, 1), datetime(2006, 1, 1)
    # Generate one real calendar instant; four pillars remain mutually consistent.
    virtual = instant or (start + timedelta(minutes=random.randrange(int((end-start).total_seconds() // 60))))
    lunar = Solar.fromDate(virtual).getLunar()
    eight = lunar.getEightChar()
    eight.setSect(2)
    pillars = []
    for part, label in [('Year', '年柱'), ('Month', '月柱'), ('Day', '日柱'), ('Time', '时柱')]:
        nayin = getattr(eight, 'get' + part + 'NaYin')()
        pillars.append({'label': label, 'text': getattr(eight, 'get' + part)(),
                        'elements': getattr(eight, 'get' + part + 'WuXing')(),
                        'nayin': NAYIN_ALIASES.get(nayin, nayin)})
    name = eight.getDayNaYin()
    name = NAYIN_ALIASES.get(name, name)
    entry = next(row for row in library('nayin') if row['name'] == name)
    counts = Counter(''.join(p['elements'] for p in pillars))
    return {**entry, 'pillars': pillars, 'virtual_birth': virtual.strftime('%Y.%m.%d %H:%M'),
            'day_master': eight.getDayGan(), 'lunar_date': lunar.toString(),
            'elements': [{'name': name, 'count': counts[name]} for name in '木火土金水'],
            'date': datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y.%m.%d')}


async def fortune_reading(arguments=''):
    if str(arguments).strip():
        from .birth_chart import bazi_reading
        return await bazi_reading(arguments)
    data = await asyncio.to_thread(make_fortune)
    return {'code': 200, 'msg': 'ok', 'data': data, 'temp': await load_template('fortune.html')}


async def love_reading(book, question='', owner=''):
    question = str(question or '').strip()
    if question in ('帮助', '主题', '目录'):
        return {'code':0,'msg':'恋爱书：在心里默念一个问题，然后发送「恋爱书」，随机翻出一句回应。\n再次发送即可再翻一页。问题可以留在心里，结果为随机抽取。','data':{},'temp':''}
    if len(question)>120:
        return {'code':0,'msg':'把问题留在心里，直接发送「恋爱书」即可翻页。','data':{},'temp':''}
    await asyncio.to_thread(library, 'love_answers')
    row = book.draw('', owner)
    data = {**row, 'page':int(row['id'].split('-')[-1]),
            'date': datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y.%m.%d')}
    return {'code':200,'msg':'ok','data':data,'temp':await load_template('love_book.html')}
