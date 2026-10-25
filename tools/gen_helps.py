"""Build the static help page after editing commands. No work is done per query."""
import ast,base64,html,json,re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
source=(ROOT/'main.py').read_text();tree=ast.parse(source)
command_map=next(n.value for n in ast.walk(tree) if isinstance(n,ast.Assign) and isinstance(n.value,ast.Dict) and n.value.keys and any(isinstance(t,ast.Attribute) and t.attr=='command_map' for t in n.targets))
commands=[(k.value,v.attr) for k,v in zip(command_map.keys,command_map.values)]
docs={}
for file in [ROOT/'main.py',ROOT/'core/message.py']:
    docs.update({n.name:(ast.get_docstring(n) or '').splitlines()[0] if ast.get_docstring(n) else '' for n in ast.walk(ast.parse(file.read_text())) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))})
data=json.loads((ROOT/'data/help_categories.json').read_text())
categories={'今日与占卜':['趣味互动'],'日常与活动':['活动情报'],'角色与奇遇':['角色名片','奇遇相关','百战记录','角色信息'],'名剑与排行':['名剑相关','排行榜单','资历相关'],'物价与交易':['物价交易','百度贴吧'],'心法与配装':['心法配装'],'江湖与家园':['阵营战场','游戏社区','家园杂项'],'团队与报名':['团队管理'],'管理与订阅':['插件与推送','避雷功能'],'资讯与工具':['系统工具']}
extra={'今日运势':'今日与占卜','今日塔罗':'今日与占卜','角色状态':'角色与奇遇','角色在线':'角色与奇遇','副本':'角色与奇遇','物价选':'物价与交易','隐私说明':'管理与订阅','关闭报警':'团队与报名'}
primary={'horoscope':'今日运势','tarot_single':'塔罗','ziwei':'紫微斗数','fortune_lot':'生辰八字','love_book':'恋爱书','fubeng':'副本CD','qiyugonglue':'奇遇攻略'}
usage={'今日运势':'今日运势 [星座]','今日塔罗':'今日塔罗','塔罗':'塔罗 [问题]','塔罗三张':'塔罗三张 [问题]','恋爱书':'恋爱书','生辰八字':'生辰八字 公历日期 时间 [性别]','紫微斗数':'紫微斗数 公历日期 时间 性别','功能':'功能','绑定区服':'绑定区服 服务器','解绑区服':'解绑区服','事件推送':'事件推送 订阅名 开启/关闭','战绩':'战绩 服务器 角色名 [模式]','名剑排行':'名剑排行 [模式]','物价':'物价 外观名','物价选':'物价选 序号','副本CD':'副本CD 服务器 角色名','查看限制':'查看限制 规则范围','添加限制':'添加限制 规则范围 类型 目标 数量','修改限制':'修改限制 规则范围 规则编号 数量','删除限制':'删除限制 规则范围 规则编号','恢复默认限制':'恢复默认限制 规则范围','修改报名':'修改报名 团编号 角色名 [心法]','交换位置':'交换位置 团编号 位置1 位置2','报名':'报名 团编号 心法 角色名 [老板]'}
notes={'今日运势':'留空看十二星座；附今日牌','今日塔罗':'每人每天一张，跨群相同','塔罗':'随时抽牌，解读当前的问题','塔罗三张':'三张展开：处境、阻碍、建议','恋爱书':'心里想好问题，再随机翻页','生辰八字':'留空抽签；填生日排盘','紫微斗数':'例：2000-08-16 04:30 女','物价':'默认外观 + 相关款式选择','物价选':'沿用上次搜索的候选列表','名剑排行':'模式可选 22 / 33 / 55','事件推送':'日常、新闻、开服等订阅','绑定区服':'绑定后查询可省略服务器'}
# Small original line icons: consistent stroke and bounding box, no icon font.
paths=['M12 3v3m0 12v3M3 12h3m12 0h3M5.6 5.6l2.1 2.1m8.6 8.6 2.1 2.1M5.6 18.4l2.1-2.1m8.6-8.6 2.1-2.1M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0', 'M5 5h14v15H5zM8 3v4m8-4v4M5 10h14M8 14h2m4 0h2m-8 3h2', 'M8 7a4 4 0 1 0 8 0 4 4 0 1 0-8 0M4 21v-3a8 8 0 0 1 16 0v3', 'M5 21V12h4v9m2 0V4h4v17m2 0V9h4v12', 'M3 7h18v13H3zM3 7l14-4v4M16 13h5v3h-5z', 'M5 3l14 16m-9-1 4-4M3 5l16 14m-1-9-4 4M3 21l5-5M16 8l5-5', 'M3 20h18M5 20V9l7-6 7 6v11M9 20v-7h6v7', 'M3 9h18v12H3zM6 3v8m12-8v8M3 6h18M8 15h2m4 0h2', 'M12 3l8 4v6c0 4-8 8-8 8s-8-4-8-8V7zM8 12l3 3 5-6', 'M3 4h8v17H3zM13 4h8v17h-8M6 8h2m8 0h2M6 12h2m8 0h2']
by_handler={}
for name,handler in commands:by_handler.setdefault(handler,[]).append(name)
buckets={name:[] for name in categories}
for handler,aliases in by_handler.items():
    name=primary.get(handler,aliases[0]);origin=data['cmd2sec'].get(aliases[0],'系统工具')
    section=extra.get(name) or extra.get(aliases[0]) or next((k for k,v in categories.items() if origin in v),'资讯与工具')
    doc=docs.get(handler,'');rest=' '.join(doc.split()[1:]);example=usage.get(name,(name+' '+rest).strip())
    # Long optional explanations belong to command hints, not to the directory grid.
    example=re.sub(r'（.*?）','',example).replace('角色 ','角色名 ').replace('服务器名','服务器')
    others=[x for x in aliases if x!=name and x.lower()!=name.lower()]
    alias=(' / '.join(others)) if others else ''
    buckets[section].append({'name':name,'use':example,'note':notes.get(name,''),'aliases':alias,'commands':aliases})
order=['今日运势','今日塔罗','塔罗','塔罗三张','恋爱书','生辰八字','紫微斗数']
buckets['今日与占卜'].sort(key=lambda x:order.index(x['name']) if x['name'] in order else 99)
sections=[];escape=html.escape
for i,(title,items) in enumerate(buckets.items()):
    icon='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="'+paths[i]+'"/></svg>'
    rows=[]
    for item in items:
        aliases=''
        note=f'<p class="note">{escape(item["note"])}</p>' if item['note'] else ''
        rows.append(f'<article class="command" data-commands="{escape(json.dumps(item["commands"],ensure_ascii=False))}"><span class="icon">{icon}</span><div><h3>{escape(item["name"])}</h3><p class="usage">{escape(item["use"])}</p>{note}{aliases}</div></article>')
    sections.append(f'<section class="category"><header><h2>{title}</h2></header><div class="grid">'+''.join(rows)+'</div></section>')
logo='data:image/png;base64,'+base64.b64encode((ROOT/'logo.png').read_bytes()).decode()
css=(ROOT/'tools/helps_page.css').read_text()
page='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>咩咩功能</title><style>/*__DAILY_CSS__*/\n'''+css+'''</style></head><body><main><header class="cover"><div><p class="brand">剑网三咩咩</p><h1>江湖事，问咩咩。</h1><p class="intro">查询 · 记录 · 日常小惊喜</p></div><img src="'''+logo+'''" alt="咩咩"></header><div class="quick"><span>直接发送对应指令</span><span>[方括号] 内可不填</span><span>绑定区服后可省略服务器</span></div>'''+''.join(sections)+'''<footer data-jx3-data-time><span>剑网三咩咩 · 功能目录</span><span>隐私与数据使用：隐私说明</span></footer></main></body></html>'''
(ROOT/'templates/pages/helps.html').write_text(page)
(ROOT/'data/help_manifest.json').write_text(json.dumps({'commands':[x[0] for x in commands],'sections':buckets},ensure_ascii=False,indent=2))
print('Built',len(commands),'commands /',len(by_handler),'entries /',len(categories),'sections')
