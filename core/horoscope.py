"""Dated source pages, shared 30-minute cache, and a twelve-sign overview."""
import asyncio
import hashlib
import html
import json
import re
import ssl
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from .template import load_template

SIGNS = dict(zip(('白羊座','金牛座','双子座','巨蟹座','狮子座','处女座','天秤座','天蝎座','射手座','摩羯座','水瓶座','双鱼座'), ('aries','taurus','gemini','cancer','leo','virgo','libra','scorpio','sagittarius','capricorn','aquarius','pisces')))
USAGE = '今日运势\n今日运势 星座名\n例如：今日运势 水瓶座\n留空查看十二星座；「今日塔罗」查看你的每日牌。'
SECTION_KEYS = [('all','综合','综合指数','综合运势'),('love','感情','爱情指数','爱情运势'),('work','事业与学业','工作学习指数','事业学业'),('money','财富','财富指数','财富运势'),('health','健康','健康指数','健康运势')]


def parse_sign(text):
    text = str(text).strip().lower()
    return next((key for name,key in SIGNS.items() if text in (name,name[:-1],key)),None)


def clean(text):
    text = html.unescape(re.sub(r'<[^>]*>','',text)).strip()
    return re.sub(r'星[A-Za-z0-9_\s]*座[A-Za-z0-9_\s]*屋','',text).strip()


def normalize_page(source, sign, day):
    name = next(name for name,key in SIGNS.items() if key==sign)
    headings = re.findall(r'<h4[^>]*>(.*?)</h4>',source,re.S)
    heading = next((clean(h) for h in headings if clean(h).startswith(name)), '')
    if not re.fullmatch(re.escape(name)+r'(?:今日|明日)运势0?'+str(day.month)+r'月0?'+str(day.day)+'日',heading):
        raise ValueError('wrong page date or sign')
    # Match the selected date on the source chart; never take the last point (tomorrow).
    axes = re.search(r"xname:\s*\[([^\]]+)\]",source)
    if not axes:raise ValueError('date axis unavailable')
    dates = [value.split('_',1)[1].replace('_','-').replace('.','-') for value in re.findall(r"'([^']+)'",axes[1])]
    if dates.count(day.isoformat())!=1:raise ValueError('target date unavailable')
    position = dates.index(day.isoformat())
    series = {}
    for label,raw in re.findall(r'name:\s*"([^\"]+)",\s*data:\s*(\[[\d.,\s]+\])',source):
        series[json.loads('"'+label+'"')] = json.loads(raw)
    body = re.search(r'<div class="c_cont">(.*?)<div class="z">',source,re.S)
    if not body:raise ValueError('missing readings')
    readings = {clean(label):clean(text) for label,text in re.findall(r'<p>\s*<strong[^>]*>(.*?)</strong>\s*<span>(.*?)</span>\s*</p>',body[1],re.S)}
    sections = []
    for key,label,series_name,reading_name in SECTION_KEYS:
        points = series.get(series_name,[])
        if len(points)<=position or not 0<=points[position]<=5:raise ValueError('invalid score')
        text = readings.get(reading_name,'')
        if not text or len(text)>1600:raise ValueError('incomplete reading')
        sections.append({'key':key,'label':label,'text':text,'score':round(points[position]*20)})
    values = {clean(label).rstrip('：:'):clean(value) for label,value in re.findall(r'<li[^>]*>\s*<label>(.*?)</label>(.*?)</li>',source,re.S)}
    lucky = [{'label':label,'value':values.get(origin,'')} for label,origin in [('幸运色','幸运颜色'),('幸运数字','幸运数字'),('速配星座','速配星座')] if values.get(origin)]
    result = {'name':name,'sign':sign,'date':day.strftime('%Y.%m.%d'),'comment':values.get('短评',''),'overview':sections[0],'sections':sections[1:],'lucky':lucky,'yi':'','ji':'','source':'星座屋','source_url':f'https://www.xzw.com/fortune/{sign}/{day:%Y%m%d}.html'}
    result['content_hash'] = hashlib.sha256(json.dumps({'sections':sections,'lucky':lucky},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return result


class HoroscopeService:
    def __init__(self, session_factory, clock=time.monotonic, today=None):
        self.session_factory=session_factory;self.clock=clock
        # This public source needs legacy initial TLS negotiation; certificates and hostname remain verified.
        self.source_tls=ssl.create_default_context();self.source_tls.options |= ssl.OP_LEGACY_SERVER_CONNECT
        self.today=today or (lambda:datetime.now(ZoneInfo('Asia/Shanghai')).date())
        self.cache={};self.failures={};self.locks={sign:asyncio.Lock() for sign in SIGNS.values()};self.requests=asyncio.Semaphore(4)

    async def _fetch(self, sign, day):
        session=await self.session_factory()
        url=f'https://www.xzw.com/fortune/{sign}/{day:%Y%m%d}.html'
        async with self.requests, asyncio.timeout(8):
            async with session.get(url,headers={'User-Agent':'Mozilla/5.0'},allow_redirects=False,ssl=self.source_tls) as response:
                if response.status!=200:raise ValueError('dated page unavailable')
                blob=bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    blob.extend(chunk)
                    if len(blob)>262144:raise ValueError('oversized page')
                return bytes(blob).decode('utf-8')

    async def data(self, sign):
        async with self.locks[sign]:
            day=self.today();cached=self.cache.get(sign);now=self.clock()
            if cached and cached[0]==day and now-cached[1]<1800:return cached[2]
            if self.failures.get(sign,0)>now:return None
            try:
                data=normalize_page(await self._fetch(sign,day),sign,day)
                if self.today()!=day:return None
            except Exception:
                self.failures[sign]=self.clock()+30;return None
            data['unchanged_since_previous_day']=bool(cached and cached[0]!=day and cached[2]['content_hash']==data['content_hash'])
            self.cache[sign]=(day,self.clock(),data);self.failures.pop(sign,None)
            return data

    async def reading(self, text=''):
        text=str(text).strip();sign=parse_sign(text)
        if text and not sign:return {'code':0,'msg':USAGE,'data':{},'temp':''}
        day=self.today()
        if sign:
            data=await self.data(sign)
            if data is None or self.today()!=day:return self.unavailable()
            return {'code':200,'msg':'ok','data':data,'temp':await load_template('horoscope.html')}
        rows=await asyncio.gather(*(self.data(key) for key in SIGNS.values()))
        if self.today()!=day:return self.unavailable()
        if not any(rows):return self.unavailable()
        rows=[row or {'name':name,'sign':key,'unavailable':True} for (name,key),row in zip(SIGNS.items(),rows)]
        return {'code':200,'msg':'ok','data':{'date':day.strftime('%Y.%m.%d'),'signs':rows,'missing':sum(bool(row.get('unavailable')) for row in rows)},'temp':await load_template('horoscope_overview.html')}

    @staticmethod
    def unavailable():
        return {'code':0,'msg':'当天运势尚未取到，请稍后再试。发送「今日塔罗」仍可查看每日牌。','data':{},'temp':''}
