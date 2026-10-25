"""Data-driven market layout and SVG. No network or renderer dependencies."""
import copy, html, math
from statistics import median
from datetime import date, timedelta
from .market_fusion import fuse_history
def money(value):
 v=float(value)
 if not math.isfinite(v): raise ValueError('Price must be finite')
 return f'{v:g}'

def make_chart(raw):
 if raw.get('series') is not None:return make_history_chart(raw)
 points=sorted(copy.deepcopy(raw.get('points',[])),key=lambda r:r['date'])
 if not points:return None
 for p in points:date.fromisoformat(p['date']);money(p['value'])
 start=date.fromisoformat(points[0]['date']);end=date.fromisoformat(points[-1]['date']);span=(end-start).days
 values=[float(p['value']) for p in points];mn,mx=min(values),max(values)
 rough=(mx-mn)/2 if mx>mn else max(abs(mx)*.1,1)
 scale=10**math.floor(math.log10(rough));step=next(n for n in [1,2,5,10] if n*scale>=rough)*scale
 lo=math.floor(mn/step)*step;hi=math.ceil(mx/step)*step
 if hi==lo:lo=max(0,lo-step);hi+=step
 x=lambda d:8+(date.fromisoformat(d)-start).days/span*482 if span else 249
 y=lambda v:98-(float(v)-lo)/(hi-lo)*76
 pts=[(x(p['date']),y(p['value'])) for p in points]
 path=' '.join(('M' if i==0 else 'L')+f'{a:.2f},{b:.2f}' for i,(a,b) in enumerate(pts))
 aria=html.escape(f"{raw.get('title','价格走势')}，{start.isoformat()}至{end.isoformat()}，{len(points)}条记录，最低{money(mn)}元，最近{money(values[-1])}元",quote=True)
 svg=f'<svg class="chart" viewBox="0 0 560 146" role="img" aria-label="{aria}"><defs><linearGradient id="fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--color-trace)" stop-opacity=".13"/><stop offset="1" stop-color="var(--color-trace)" stop-opacity="0"/></linearGradient></defs>'
 for v in [lo,(lo+hi)/2,hi]:
  svg+=f'<line class="gridline" x1="8" x2="490" y1="{y(v)}" y2="{y(v)}"/><text class="axis" x="556" y="{y(v)+7}" text-anchor="end">{money(v)}</text>'
 if len(pts)>1:
  svg+=f'<path d="{path} L{pts[-1][0]},104 L{pts[0][0]},104 Z" fill="url(#fill)"/><path class="trace" d="{path}"/>'
 low=min(points,key=lambda p:p['value'])
 if low in [points[0],points[-1]]:
  troughs=[(min(points[i-1]['value'],points[i+1]['value'])-p['value'],p) for i,p in enumerate(points[1:-1],1) if p['value']<min(points[i-1]['value'],points[i+1]['value'])]
  if troughs:low=max(troughs,key=lambda pair:pair[0])[1]
 markers={(p['date'],p['value']) for p in [points[0],low,points[-1]]}
 for d,v in markers:svg+=f'<circle class="point" cx="{x(d)}" cy="{y(v)}" r="3"/>'
 if len(points)>1 and mn!=mx:
  anchor='start' if x(low['date'])<48 else 'end' if x(low['date'])>440 else 'middle'
  svg+=f'<text class="val" x="{x(low["date"])}" y="{y(low["value"])-12 if x(low["date"])<48 or x(low["date"])>440 else y(low["value"])+25}" text-anchor="{anchor}">{money(low["value"])}</text>'
 if len(points)>1 and points[-1]!=low:
  svg+=f'<text class="val" x="{x(points[-1]["date"])-6}" y="{y(points[-1]["value"])-12}" text-anchor="end">{money(points[-1]["value"])}</text>'
 ticks=[start,start+timedelta(days=span//2),end] if span>1 else [start,end] if span else [start]
 for i,d in enumerate(ticks):
  anchor='middle' if len(ticks)==1 or 0<i<len(ticks)-1 else 'start' if i==0 else 'end'
  label=d.strftime('%Y.%m') if start.year!=end.year else d.strftime('%m.%d')
  svg+=f'<text class="axis" x="{x(d.isoformat())}" y="140" text-anchor="{anchor}">{label}</text>'
 svg+='</svg>'
 period=start.strftime('%Y.%m.%d') if span==0 else f"{start:%Y.%m.%d} — {end:%m.%d}" if start.year==end.year else f"{start:%Y.%m} — {end:%Y.%m}"
 change=f'{(values[-1]/values[0]-1)*100:+.1f}%' if raw.get('comparable',False) and span and values[0]>0 else None
 return dict(title=raw.get('title','价格走势'),context=raw.get('context',''),latest_label=raw.get('latest_label','最近记录'),latest_value=points[-1]['value'],change_pct=change,period=period,svg=svg,points=points,axis_bounds=[lo,hi])

def _history_path(points, x, y, smooth=False):
    """Monotone cubic segments keep observed extrema and never overshoot them."""
    coords = [(x(p['date']), y(p['value'])) for p in points]
    if not coords:
        return ''
    parts = [f'M{coords[0][0]:.2f},{coords[0][1]:.2f}']
    if len(coords) < 2:
        return parts[0]
    slopes = [(b[1]-a[1])/(b[0]-a[0]) for a,b in zip(coords,coords[1:])]
    tangents = [slopes[0]] + [2*a*b/(a+b) if a*b>0 else 0 for a,b in zip(slopes,slopes[1:])] + [slopes[-1]]
    for i, ((ax,ay),(bx,by)) in enumerate(zip(coords,coords[1:])):
        gap = (date.fromisoformat(points[i+1]['date'])-date.fromisoformat(points[i]['date'])).days
        if not smooth or gap>30:
            parts.append(f'L{bx:.2f},{by:.2f}')
        else:
            third = (bx-ax)/3
            parts.append(f'C{ax+third:.2f},{ay+tangents[i]*third:.2f} {bx-third:.2f},{by-tangents[i+1]*third:.2f} {bx:.2f},{by:.2f}')
    return ' '.join(parts)


def make_fused_chart(series):
    fused = fuse_history(series)
    if not fused['points']:
        return None
    chart = make_history_chart({'series':[fused], 'filled':True,
                                'context':'市场参考价 / 元', 'title':'长期价格走势'})
    chart['fused'] = True
    chart['providers'] = fused['providers']
    chart['method'] = '在售日均价优先，历史报价补充' if 'listing_mean' in fused['bases'] else '按日整理有效报价'
    chart['sources_text'] = '、'.join(fused['providers'])
    return chart


def make_history_chart(raw):
    """共享完整时间轴，按价格类型画线；报价表的截取不参与走势图计算。"""
    series=copy.deepcopy([s for s in raw['series'] if s.get('points')])
    if not series:return None
    for s in series:
        s['points'].sort(key=lambda p:p['date'])
        for p in s['points']:date.fromisoformat(p['date']);money(p['value'])
    points=sorted([p for s in series for p in s['points']],key=lambda p:p['date'])
    start=date.fromisoformat(points[0]['date']);end=date.fromisoformat(points[-1]['date']);span=(end-start).days
    values=[float(p['value']) for p in points];mn,mx=min(values),max(values)
    rough=(mx-mn)/3 if mx>mn else max(abs(mx)*.1,1)
    scale=10**math.floor(math.log10(rough));step=next(n for n in [1,2,5,10] if n*scale>=rough)*scale
    lo=0 if raw.get('filled') else max(0,math.floor(mn/step)*step);hi=math.ceil(mx/step)*step
    if hi==lo:lo=max(0,lo-step);hi+=step
    x=lambda d:10+(date.fromisoformat(d)-start).days/span*492 if span else 256
    y=lambda v:124-(float(v)-lo)/(hi-lo)*102
    aria=html.escape(f"历史价格走势，{start.isoformat()}至{end.isoformat()}，{len(points)}个日价格点，按记录日期连线",quote=True)
    svg=f'<svg class="chart history-chart" viewBox="0 0 580 168" role="img" aria-label="{aria}">'
    if raw.get('filled'):
        svg+='<defs><linearGradient id="marketHistoryFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--cinnabar)" stop-opacity=".28"/><stop offset="1" stop-color="var(--cinnabar)" stop-opacity=".045"/></linearGradient></defs>'
    for v in [lo,(lo+hi)/2,hi]:
        svg+=f'<line class="gridline" x1="10" x2="502" y1="{y(v):.2f}" y2="{y(v):.2f}"/><text class="axis" x="576" y="{y(v)+6:.2f}" text-anchor="end">{money(v)}</text>'
    has_gaps=False
    for s in series:
        color=s['color'];dash=s.get('dash','');ps=s['points']
        svg+=f'<g style="color:{color}"><title>{html.escape(s["label"])}</title>'
        # A fine dotted connector distinguishes long unobserved intervals from
        # densely sampled periods without manufacturing daily observations.
        if raw.get('filled'):
            path=_history_path(ps,x,y,smooth=True)
            if len(ps)>1:
                svg+=f'<path class="history-area" d="{path} L{x(ps[-1]["date"]):.2f},124 L{x(ps[0]["date"]):.2f},124 Z" fill="url(#marketHistoryFill)"/>'
                svg+=f'<path class="history-line" d="{path}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        else:
            paths=[];part=[]
            for i,p in enumerate(ps):
                position=f'{x(p["date"]):.2f},{y(p["value"]):.2f}'
                if i and (date.fromisoformat(p['date'])-date.fromisoformat(ps[i-1]['date'])).days>30:
                    previous=ps[i-1];has_gaps=True
                    if len(part)>1:paths.append(' '.join(part))
                    part=[]
                    svg+=f'<path d="M{x(previous["date"]):.2f},{y(previous["value"]):.2f} L{position}" fill="none" stroke="currentColor" stroke-width="1" stroke-opacity=".55" stroke-dasharray="2 4"/>'
                part.append(('L' if part else 'M')+position)
            if len(part)>1:paths.append(' '.join(part))
            for path in paths:svg+=f'<path d="{path}" fill="none" stroke="currentColor" stroke-width="2" stroke-dasharray="{dash}" stroke-linecap="round" stroke-linejoin="round"/>'
        markers = ps if len(ps) <= 24 else [ps[0], ps[-1]]
        for p in markers:
            # Dots show actual observations, including isolated and flat-price records.
            r=3.3 if len(ps)==1 else 2.1
            svg+=f'<circle cx="{x(p["date"]):.2f}" cy="{y(p["value"]):.2f}" r="{r}" fill="currentColor"><title>{p["date"]} · {money(p["value"])} 元</title></circle>'
        svg+='</g>'
    # Label the latest prices first, then the start and overall peak. Place
    # labels in free space and connect displaced labels back to their observation.
    candidates=[(s,s['points'][-1]) for s in series]
    for target in [points[0],max(points,key=lambda p:p['value'])]:
        owner=next(s for s in series if target in s['points']);candidates.append((owner,target))
    if raw.get('filled'):
        for target in [min(points,key=lambda p:p['value']),points[len(points)//3],points[len(points)*2//3]]:
            candidates.append((series[0],target))
    occupied=[];labelled=set();labels=[]
    for s,p in candidates:
        key=(p['date'],p['value'])
        if key in labelled:continue
        labelled.add(key);text=money(round(p['value'],2));px,py=x(p['date']),y(p['value'])
        width=len(text)*10+6;chosen=None
        for dy in [-24,9,-47,32,-70,55,-93,78]:
            for dx in [-width/2,-width-9,9]:
                left=min(504-width,max(8,px+dx));top=py+dy
                box=(left,top,left+width,top+21)
                if top<0 or top+21>143:continue
                if any(box[0]<b[2]+5 and box[2]>b[0]-5 and box[1]<b[3]+4 and box[3]>b[1]-4 for b in occupied):continue
                chosen=box;break
            if chosen:break
        if not chosen:continue
        occupied.append(chosen);left,top,right,bottom=chosen;cx=(left+right)/2
        if abs(cx-px)>18 or abs((top+bottom)/2-py)>32:
            edge=bottom if bottom<py else top
            svg+=f'<path d="M{px:.2f},{py:.2f} L{cx:.2f},{edge:.2f}" fill="none" stroke="{s["color"]}" stroke-opacity=".5" stroke-width=".8"/>'
        svg+=f'<text class="history-value" x="{cx:.2f}" y="{top+17:.2f}" text-anchor="middle" fill="{s["color"]}">{text}</text>'
        labels.append(dict(date=p['date'],value=p['value'],label=s['label'],box=chosen))
    ticks=[start,start+timedelta(days=span//3),start+timedelta(days=span*2//3),end] if span>120 else [start,start+timedelta(days=span//2),end] if span>1 else [start,end] if span else [start]
    for i,d in enumerate(ticks):
        anchor='middle' if len(ticks)==1 or 0<i<len(ticks)-1 else 'start' if i==0 else 'end'
        label=d.strftime('%Y.%m') if start.year!=end.year else d.strftime('%m.%d')
        svg+=f'<text class="axis" x="{x(d.isoformat()):.2f}" y="162" text-anchor="{anchor}">{label}</text>'
    svg+='</svg>'
    period=f'{start:%Y.%m.%d}' if not span else f'{start:%Y.%m.%d} — {end:%m.%d}' if start.year==end.year else f'{start:%Y.%m} — {end:%Y.%m}'
    return dict(title=raw.get('title','历史价格走势'),context=raw.get('context',''),period=period,svg=svg,change_pct=None,points=points,series=series,labels=labels,axis_bounds=[lo,hi],latest_value=points[-1]['value'],has_gaps=has_gaps)

def prepare(fixture):
 d=copy.deepcopy(fixture)
 product=d['product'];product.setdefault('alias','');product.setdefault('retail',None);product.setdefault('description','');product.setdefault('image',None)
 product['facts']=[f for f in product.get('facts',[]) if f.get('value') not in [None,'']]
 if product['image']:product['image'].setdefault('viewbox',None);product['image'].setdefault('alt',product['name'])
 markets=d.get('markets',[])
 for m in markets:
  m['rows']=sorted(m.get('rows',[]),key=lambda r:r['date'])
  for row in m['rows']:date.fromisoformat(row['date']);money(row['value'])
  m['asof']=max((r['date'] for r in m['rows']),default=None)
  m['minimum']=min((r['value'] for r in m['rows'] if r['date']==m['asof']),default=None)
  m['has_remaining']=any(r.get('remaining') for r in m['rows'])
  years={r['date'][:4] for r in m['rows']};multi=len(years)>1
  m['date_heading']='日期' if m['has_remaining'] or multi or not years else f'日期 · {next(iter(years))}'
  for row in m['rows']:row['display_date']=row['date'][2:].replace('-','.') if multi else row['date'][5:].replace('-','.')
 all_players=sorted(d.get('players',[]),key=lambda r:r['date'])
 # Keep the newest records, then preserve chronological reading within the visible set.
 player_limit=max(1,int(d.get('player_limit',6)))
 players=all_players[-player_limit:]
 for p in players:date.fromisoformat(p['date']);money(p['value'])
 return dict(product=product,fact_columns=2 if len(product['facts'])==4 else min(3,max(1,len(product['facts']))),markets=markets,players=players,player_total=len(all_players),player_asof=players[-1]['date'].replace('-','.') if players else None,has_source=any(p.get('source') for p in players),cross_server=len({p.get('server') for p in players})>1,trend=make_chart(d.get('trend',{})))


def _number(value):
    try:
        n=float(value)
        return n if math.isfinite(n) and n>=0 else None
    except (TypeError,ValueError):return None

def _day(value):
    value=str(value or '')[:10].replace('/','-')
    try:return date.fromisoformat(value).isoformat()
    except ValueError:return ''

def market_page(data, notice, sale, deals, tieba):
    """Adapt the existing API contract; never inject reference-fixture product facts."""
    def rows(items):
        out=[]
        for item in items:
            value=_number(item.get('value'));day=_day(item.get('date'))
            if value is None or not day:continue
            r=dict(date=day,value=value,server=str(item.get('server') or ''),status=item.get('status','记录'))
            remaining=item.get('remaining') or item.get('remaining_time')
            if remaining:r['remaining']=str(remaining).removeprefix('剩余')
            elif _number(item.get('remainingTime')) is not None:
                seconds=int(float(item['remainingTime']));days,seconds=divmod(seconds,86400);hours,seconds=divmod(seconds,3600);minutes=seconds//60
                r['remaining']=(f'{days}天{hours}时' if days else f'{hours}时{minutes}分')
            out.append(r)
        return out
    markets=[dict(title='万宝楼公示',rows=rows(notice)),dict(title='万宝楼在售',rows=rows(sale))]
    players=[];seen=set()
    for source,items in [('交易',deals),('贴吧',tieba)]:
        for r in rows(items):
            status=r.get('status','记录')
            kind='done' if status=='成交' else 'buy' if status in ['收购','想收'] else 'sell' if status in ['出售','想出','正出'] else 'other'
            label={'done':'成交','buy':'收购报价','sell':'出售报价'}.get(kind,status)
            key=(r['date'],r.get('server',''),r['value'],kind,source)
            if key in seen:continue
            seen.add(key);players.append(dict(r,source=source,kind=kind,label=label))
    facts=[]
    for label,key in [('分类','category'),('售卖日期','date'),('获取方式','method'),('不绑定限时','unbound_duration'),('不绑定限量','unbound_limit'),('绑定限时','bound_duration')]:
        if data.get(key) not in [None,'']:facts.append(dict(label=label,value=str(data[key])))
    # Use every valid date before the visible quote table is limited. Different
    # transaction types remain separate; daily aggregation keeps the chart legible.
    history=[]
    def add_series(label,items,color,dash='',aggregate=median):
        daily={}
        for r in items:daily.setdefault(r['date'],[]).append(r['value'])
        if daily:history.append(dict(label=label,color=color,dash=dash,points=[dict(date=d,value=aggregate(v)) for d,v in sorted(daily.items())]))
    for kind,label,color,dash in [('sell','出售报价','#965444',''),('buy','收购报价','#947849','5 4'),('done','成交','#49685c',''),('other','其他记录','#77716a','2 4')]:
        add_series(label,[r for r in players if r['kind']==kind],color,dash)
    add_series('公示',markets[0]['rows'],'#918577','2 3',min)
    add_series('在售',markets[1]['rows'],'#354551','',min)
    context='玩家日中位 · 万宝楼日最低 / 元' if players and any(m['rows'] for m in markets) else '玩家同类报价日中位 / 元' if players else '万宝楼日最低 / 元'
    fixture=dict(product=dict(name=str(data.get('name') or '外观价格'),alias=str(data.get('alias') or ''),retail=_number(data.get('retail')),description=str(data.get('desc') or ''),facts=facts,image=dict(src=data['view'],alt=str(data.get('name') or '商品图')) if data.get('view') else None),markets=markets,players=players,trend=dict(context=context,series=history))
    result=prepare(fixture)
    result['_history_series']=history
    result['trend']=make_fused_chart(history)
    return result
