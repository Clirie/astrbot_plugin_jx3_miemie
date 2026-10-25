"""Local birth charts; input is used in memory and never sent to a content API."""
import asyncio
import json
import re
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..vendor.lunar_python import Solar
from .template import load_template

ROOT=Path(__file__).resolve().parents[1]
USAGE='生辰八字 公历出生日期 出生时间\n例如：生辰八字 2000-08-16 04:30\n采用北京时间；需填写准确时间。直接发「命格签」可随机抽签。'
ZIWEI_USAGE='紫微斗数 公历出生日期 出生时间 性别\n例如：紫微斗数 2000-08-16 04:30 女\n性别填男或女，用于传统排盘规则；采用北京时间。'


def parse_birth(arguments, gender_required=False):
    text=str(arguments).strip()
    if text.startswith(('农历','阴历')):
        raise ValueError('请先换算为公历日期，并按公历日期填写。')
    text=re.sub(r'^(公历|阳历)\s*','',text)
    m=re.fullmatch(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?\s+(\d{1,2})[:：](\d{2})(?:\s+(男|女))?',text)
    if not m:
        raise ValueError(ZIWEI_USAGE if gender_required else USAGE)
    try: birth=datetime(*map(int,m.groups()[:5]))
    except ValueError: raise ValueError('日期或时间无效，请检查月份、日期、小时和分钟。') from None
    if not 1900<=birth.year or birth>datetime.now(ZoneInfo('Asia/Shanghai')).replace(tzinfo=None):
        raise ValueError('请填写 1900 年至今的有效出生日期。')
    gender=m[6]
    if gender_required and not gender:
        raise ValueError(ZIWEI_USAGE)
    return birth,gender


def make_bazi(birth, gender=None):
    lunar=Solar.fromDate(birth).getLunar();eight=lunar.getEightChar();eight.setSect(2)
    pillars=[]
    for key,label in [('Year','年柱'),('Month','月柱'),('Day','日柱'),('Time','时柱')]:
        get=lambda suffix:getattr(eight,'get'+key+suffix)()
        pillars.append({'label':label,'text':get(''),'elements':get('WuXing'),'nayin':get('NaYin'),
                        'hidden':' '.join(get('HideGan')),'god':'日主' if key=='Day' else get('ShiShenGan'),
                        'hidden_gods':' · '.join(get('ShiShenZhi'))})
    counts=Counter(''.join(p['elements'] for p in pillars))
    result={'birth':birth.strftime('%Y.%m.%d %H:%M'),'lunar':lunar.toString(),'day_master':eight.getDayGan(),
            'pillars':pillars,'elements':[{'name':k,'count':counts[k]} for k in '木火土金水'],
            'date':datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y.%m.%d'),'gender':gender,'decades':[]}
    if gender:
        yun=eight.getYun(1 if gender=='男' else 0,2)
        result['yun_start']=yun.getStartSolar().toYmd()
        result['decades']=[{'start':d.getStartYear(),'end':d.getEndYear(),'text':d.getGanZhi()} for d in yun.getDaYun(9)[1:]]
    return result


async def bazi_reading(arguments):
    try:birth,gender=parse_birth(arguments)
    except ValueError as e:return {'code':0,'msg':str(e),'data':{},'temp':''}
    data=await asyncio.to_thread(make_bazi,birth,gender)
    return {'code':200,'msg':'ok','data':data,'temp':await load_template('bazi.html')}


def node_runtime():
    node=shutil.which('node')
    if node:return node
    try:
        from playwright._impl._driver import compute_driver_executable
        path=str(compute_driver_executable()[0])
        if Path(path).is_file():return path
    except (ImportError, OSError):pass
    raise RuntimeError('排盘运行环境暂不可用。')


ZIWEI_LIMIT=asyncio.Semaphore(2)


async def calculate_ziwei(birth, gender):
    # 0 = early Zi; 12 = late Zi. Keep both sides of midnight explicit.
    index=(birth.hour+1)//2
    payload={'date':birth.strftime('%Y-%m-%d'),'timeIndex':index,'gender':gender}
    async with ZIWEI_LIMIT:
        proc=await asyncio.create_subprocess_exec(node_runtime(),str(ROOT/'vendor/iztro/runner.cjs'),
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        try:
            stdout,_=await asyncio.wait_for(proc.communicate(json.dumps(payload).encode()),8)
        except BaseException:
            if proc.returncode is None:proc.kill()
            await proc.wait();raise
    if proc.returncode or len(stdout)>250000:raise RuntimeError('排盘计算未完成，请稍后再试。')
    data=json.loads(stdout)
    if len(data.get('palaces',[]))!=12:raise RuntimeError('排盘结果不完整。')
    positions={'巳':(1,1),'午':(1,2),'未':(1,3),'申':(1,4),'辰':(2,1),'酉':(2,4),
               '卯':(3,1),'戌':(3,4),'寅':(4,1),'丑':(4,2),'子':(4,3),'亥':(4,4)}
    for palace in data['palaces']:
        palace['row'],palace['col']=positions[palace['earthlyBranch']]
    data.update({'birth':birth.strftime('%Y.%m.%d %H:%M'),'date':datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y.%m.%d')})
    return data


async def ziwei_reading(arguments):
    try:
        birth,gender=parse_birth(arguments,True)
        data=await calculate_ziwei(birth,gender)
    except ValueError as e:return {'code':0,'msg':str(e),'data':{},'temp':''}
    except (RuntimeError, TimeoutError):return {'code':0,'msg':'紫微排盘暂时无法完成，请稍后再试。','data':{},'temp':''}
    return {'code':200,'msg':'ok','data':data,'temp':await load_template('ziwei.html')}
