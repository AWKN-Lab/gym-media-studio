#!/usr/bin/env python3
"""抖音候选抓取：抓取候选 -> 渲染可点选清单 -> 登记基线。

口径（2026-11-08 二次纠偏，动手前先读）：
- 产出内容的受众 = 门店周边潜在会员（C 端消费者）。
- 「健身房 获客」那批 B 端方法论视频是**参考素材**：用来学习「如何把本地人
  变成到店客户」的文案逻辑，不是要播给消费者看，也不该按受众被剔除。
  素材库与六条逻辑见 data/baseline/method_samples.json / method_logic.json。
- 本文件的 audience 分类器只用于「找同城对标视频」时标注卡片，
  **不得**作为获客方法论素材的筛选 / 剔除依据，也不参与平台文字生成。
  上一轮把参考素材受众误当成产出受众，据此把方法论标红剔除，方向是反的。

与 tools/douyin_bridge.mjs 的文件队列通信，不依赖任何网络端口。
Cookie / 登录态始终留在 Chrome 独立 profile 内，本脚本不读取、不导出。
"""
import argparse
import html
import json
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote

# 找「同城对标视频」时的消费者视角词；不是获客方法论素材的检索词
KEYWORDS_LOCAL = [
    '健身房 探店',
    '附近健身房 推荐',
    '健身房 体验课',
    '第一次去健身房',
    '健身房 团课',
    '健身房 办卡 多少钱',
]

ROOT = Path(__file__).resolve().parent.parent
BR = ROOT / 'data' / 'browser'
RESULTS = BR / 'results'
CMD = BR / 'cmd.json'
BASE = ROOT / 'data' / 'baseline'
CANDIDATES = BASE / 'candidates.json'
CANDIDATES_HTML = BASE / 'candidates.html'
GUIDE_HTML = BASE / 'guide.html'
BASELINE_JSON = BASE / 'baseline.json'

SCRAPE_JS = r"""(() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const norm = h => {
    if (!h) return '';
    if (h.startsWith('//')) return 'https:' + h;
    if (h.startsWith('/')) return 'https://www.douyin.com' + h;
    return h;
  };
  const seen = new Set();
  const out = [];
  const push = (url, text, cover, kind) => {
    if (!/\/video\/\d{6,}/.test(url) || seen.has(url)) return;
    seen.add(url);
    out.push({url: url, card_text: text.slice(0, 400), cover: cover, source_kind: kind});
  };
  // Real search results: div.search-result-card, aweme id lives in the parent
  // element id="waterfall_item_<id>"; no anchor/href is rendered for these.
  document.querySelectorAll('.search-result-card').forEach(card => {
    const pid = card.parentElement ? (card.parentElement.id || '') : '';
    const m = pid.match(/waterfall_item_(\d{10,25})/);
    if (!m) return;
    const text = clean(card.innerText || card.textContent || '');
    // The same class also wraps the AI answer block and "related searches".
    // Real video cards start with mm:ss duration (and carry an @author).
    const isVideo = /^\d{1,2}:\d{2}/.test(text) ||
                    (/\d{1,2}:\d{2}/.test(text) && text.indexOf('@') >= 0);
    if (!isVideo) return;
    const img = card.querySelector('img');
    push('https://www.douyin.com/video/' + m[1], text,
         img ? (img.src || img.getAttribute('data-src') || '') : '',
         'search_result');
  });
  // The same page also injects a "jingxuan" recommendation feed. Keep it, but
  // label it so it can never be mistaken for a search result.
  document.querySelectorAll('[href*="/video/"]').forEach(el => {
    let href = (norm(el.getAttribute('href')) || '').split('?')[0];
    const img = el.querySelector('img');
    push(href, clean(el.innerText || el.textContent || ''),
         img ? (img.src || img.getAttribute('data-src') || '') : '',
         'recommend');
  });
  if (!out.length) {
    const raw = document.documentElement.outerHTML || '';
    const hits = [...new Set(raw.match(/\/video\/\d{10,25}/g) || [])];
    hits.slice(0, 40).forEach(p => push('https://www.douyin.com' + p, '', '', 'embedded_html'));
  }
  // 2026-11-08: 风控有时不整页跳转到「验证码中间页」，而是在当前页内
  // 注入 captcha/v2 iframe，结果区为空。只判 title 会把这种读数误报为 EMPTY。
  const captcha = [...document.querySelectorAll('iframe')].filter(f => {
    const s = f.src || '';
    return /verifycenter\/captcha|captcha\/v2/i.test(s) &&
           (f.offsetWidth > 0 || f.offsetHeight > 0);
  }).length;
  return {
    url: location.href,
    title: document.title,
    body_head: clean(document.body ? document.body.innerText : '').slice(0, 600),
    count: out.length,
    captcha_iframes: captcha,
    items: out.slice(0, 80)
  };
})()"""


# SPA 直接导航 /search/<kw> 时只渲染壳、结果区为空；需要点一次搜索按钮才拉数据。
# 这一步是页面内真实点击，不是绕过风控（风控仍由 is_blocked 拦截）。
ACTIVATE_JS = r"""(() => {
  const cards = () => document.querySelectorAll('.search-result-card').length;
  if (cards() > 0) return {cards: cards(), activated: false};
  const inp = document.querySelector('input[data-e2e="searchbar-input"]');
  const btn = document.querySelector('[data-e2e="searchbar-button"]');
  if (inp && btn && inp.value) btn.click();
  return new Promise(resolve => setTimeout(() => resolve({
    cards: cards(), activated: true, url: location.href,
    value: inp ? inp.value : ''
  }), 6500));
})()"""

# 只看可见的验证框：页面常驻一个 0x0 / display:none 的 rmc-nocaptcha iframe，
# 把它算进去会误报。真滑块是 captcha/v2 且尺寸可见。
CHECK_CAPTCHA_JS = r"""(() => [...document.querySelectorAll('iframe')]
  .filter(f => /verifycenter\/captcha|captcha\/v2/i.test(f.src || '') &&
               (f.offsetWidth > 0 || f.offsetHeight > 0)).length)()"""

# 同城锚点：由 --place 注入，命中即视为"本地相关"信号（启发式，非事实断言）。
PLACE_TERMS = []
PLACE_NOTE = ''


def bridge_call(op, timeout=45, **kwargs):
    cid = f"{op}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    payload = {"id": cid, "op": op, **kwargs}
    CMD.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    out = RESULTS / f"{cid}.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if out.exists():
            data = json.loads(out.read_text(encoding='utf-8'))
            if not data.get('ok'):
                raise RuntimeError(f"bridge error: {data.get('error')}")
            return data.get('value')
        time.sleep(0.3)
    raise TimeoutError(f"bridge did not answer within {timeout}s (is tools/douyin_bridge.mjs running?)")


def is_blocked(value):
    title = value.get('title') or ''
    body = value.get('body_head') or ''
    if ('验证码' in title) or ('captcha' in title.lower()):
        return True
    if '验证' in body[:80] and not value.get('count'):
        return True
    # 页内 iframe 风控：有验证框且一个视频卡片都没有 -> 无效读数，不作 EMPTY 结论
    if (value.get('captcha_iframes') or 0) > 0 and not value.get('count'):
        return True
    return False


# 同行经营教学（B 端）：给健身房老板看的，不是我们要对标的内容。
PEER_B2B = re.compile(
    r'获客|引流|拓客|经营|盈利|加盟|标准化|地推|业绩|成交|转化率|怎么开|开店|'
    r'营销|起号|涨粉|同城号|IP打造|拆解一个行业|甩手掌柜|做账号|拍抖音|文案|投流|'
    r'私域|门店管理|会籍顾问|销售话术|营收|续卡率|老板娘|实体店|做生意|创业|'
    r'当老板|门店业绩|流量密码|变现')
# 本地消费者视角（C 端）：潜在会员会搜、会看的内容。
LOCAL_C2C = re.compile(
    r'探店|体验课|免费体验|试练|办卡|多少钱|价格|月卡|年卡|团购|优惠|开业|'
    r'环境|器械|团课|操课|第一次|新手|小白|打卡|日常|vlog|氛围|教练带|'
    r'附近|周边|推荐|测评|避坑|值不值|实拍|减脂|塑形|增肌|瘦|练|'
    r'泳池|洗澡|更衣|24小时|地址|几楼|地铁|会员')


# 出现即判同行的强信号（避免"AI赋能降本增效"这类被泛 C 端词带偏）。
# 注意不收「老板」：C 端促销常见"老板疯了，99 元月卡"，会误杀。
STRONG_PEER = re.compile(
    r'获客|引流|拓客|甩手掌柜|降本增效|盈利揭秘|加盟|起号|涨粉|'
    r'会籍顾问|做生意|拆解一个行业|怎么拍短视频|线上获客|短视频获客|'
    r'IP打造|转化率|销售话术|续卡率|投流|私域|门店业绩|流量密码|变现')


# 自家品牌：清单里可能混入自己发过的视频，必须单独标注，避免被当成竞品对标。
OWN_BRAND = re.compile(r'凌扬|LYNVOS|前浯', re.I)


def classify_audience(text):
    """按卡片文本判定受众，返回 local / peer / neutral。启发式，不是事实断言。"""
    t = text or ''
    if STRONG_PEER.search(t):
        return 'peer'
    peer = len(PEER_B2B.findall(t))
    local = len(LOCAL_C2C.findall(t))
    local += sum(t.count(w) for w in PLACE_TERMS)
    if peer and not local:
        return 'peer'
    if local and not peer:
        return 'local'
    if local and peer:
        return 'local' if local > peer else 'peer'
    return 'neutral'


def parse_card(text):
    """卡片文本形如 '04:02922标题...@作者 · 2天前'，拆出结构化字段。"""
    m = re.match(r'^(\d{1,2}:\d{2})\s*([\d.]+\s*万?)?\s*(.*)$', text, re.S)
    duration = m.group(1) if m else ''
    likes = (m.group(2) or '').replace(' ', '') if m else ''
    rest = (m.group(3) if m else text).strip()
    author, date, title = '', '', rest
    am = re.search(r'@([^@·]+?)\s*·\s*([^@]{1,24})$', rest)
    if am:
        author = am.group(1).strip()
        date = am.group(2).strip()
        title = rest[:am.start()].strip()
    return {'title': title[:200], 'author': author, 'date': date,
            'duration': duration, 'likes': likes}


def normalize(value):
    seen, items = set(), []
    for raw in value.get('items') or []:
        url = (raw.get('url') or '').strip()
        if not url or url in seen:
            continue
        seen.add(url)
        text = (raw.get('card_text') or '').strip()
        kind = raw.get('source_kind') or raw.get('extracted_from') or 'dom'
        item = {
            'url': url,
            'keyword': raw.get('keyword') or '',
            'card_text': text,
            'cover': (raw.get('cover') or '').strip(),
            'source': kind,
            'source_kind': kind,
            **parse_card(text),
        }
        # 用 title 而非 card_text：作者昵称（如「@加减健身小老板」）会带进
        # 「老板」这类词造成误判；parse_card 已把作者/日期从 title 里剥掉。
        item['audience'] = classify_audience(item['title'] or text)
        item['is_own'] = bool(OWN_BRAND.search(text))
        # 默认视图：本次搜索结果中面向本地消费者的；同行经营教学与推荐流单独标注。
        item['relevant'] = (kind == 'search_result' and item['audience'] != 'peer')
        items.append(item)
    # 本地消费者 -> 未判定 -> 同行经营 -> 推荐流；一条都不丢弃，不擅自替用户删。
    rank = {'local': 0, 'neutral': 1, 'peer': 2}
    items.sort(key=lambda x: (x['source_kind'] != 'search_result',
                              rank.get(x['audience'], 1)))
    for i, item in enumerate(items, 1):
        item['no'] = i
    return items


def render_candidates(items, meta):
    cards = []
    rel_count = sum(1 for i in items if i['relevant'])
    search_count = sum(1 for i in items if i.get('source_kind') == 'search_result')
    local_count = sum(1 for i in items if i.get('audience') == 'local' and i.get('source_kind') == 'search_result')
    peer_count = sum(1 for i in items if i.get('audience') == 'peer' and i.get('source_kind') == 'search_result')
    place = (meta.get('place') or '').strip()
    note = (meta.get('place_note') or '').strip()
    geo_banner = (f'<div class="banner geo">门店锚点：<b>{html.escape(place)}</b>'
                  + (f' · {html.escape(note)}' if note else '')
                  + '；命中地名视为本地相关（启发式，非事实断言）</div>') if place else ''
    for it in items:
        title = html.escape(it['title'] or it['card_text'][:80] or '(无标题，点开链接查看)')
        bits = [b for b in [
            ('@' + it['author']) if it['author'] else '',
            it['date'],
            it['duration'],
            (it['likes'] + ' 赞') if it['likes'] else '',
        ] if b]
        meta_line = html.escape(' · '.join(bits) or it['card_text'][:100])
        cover = html.escape(it['cover'], quote=True)
        img = (f'<img src="{cover}" referrerpolicy="no-referrer" loading="lazy" alt="">'
               if cover else '<div class="noimg">无封面</div>')
        cls = 'rel' if it['relevant'] else 'other'
        a = it.get('audience')
        if it.get('is_own'):
            tag = '<span class="tag own-tag">自家内容</span>'
        elif it.get('source_kind') != 'search_result':
            tag = '<span class="tag other-tag">推荐流</span>'
        elif a == 'local':
            tag = '<span class="tag rel-tag">本地消费者</span>'
        elif a == 'peer':
            tag = '<span class="tag peer-tag">同行经营</span>'
        else:
            tag = '<span class="tag rel-tag">搜索结果·未判定</span>'
        cards.append(f'''<article class="card {cls}" data-no="{it['no']}" onclick="pick({it['no']})">
  <div class="badge">{it['no']}</div>{tag}
  <div class="thumb">{img}</div>
  <div class="body">
    <h3>{title}</h3>
    <p class="meta">{meta_line}</p>
    {('<div class="kw">搜索词：' + html.escape(it['keyword']) + '</div>') if it.get('keyword') else ''}
    <a href="{html.escape(it['url'], quote=True)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">打开原视频 ↗</a>
  </div>
</article>''')
    kw = html.escape(meta.get('keyword') or '')
    ts = html.escape(meta.get('captured_at') or '')
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>抖音基线候选清单</title>
<style>
:root{{color-scheme:light}}
*{{box-sizing:border-box}}
body{{margin:0;background:#f6f7f9;color:#1b1f24;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
header{{background:#fff;border-bottom:1px solid #e4e7ec;padding:20px 28px;position:sticky;top:0;z-index:5}}
h1{{margin:0 0 6px;font-size:20px}}
.sub{{color:#667085;font-size:13px}}
.small{{color:#667085;font-size:12px}}
#sel{{margin-top:10px;padding:8px 12px;border-radius:8px;background:#fff4e5;color:#8a4b00;display:none;font-weight:600}}
.toolbar{{margin-top:10px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
button{{border:1px solid #d0d5dd;background:#fff;color:#344054;border-radius:8px;padding:5px 12px;font-size:12px;cursor:pointer}}
button:hover{{border-color:#e2601a;color:#b54708}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px;padding:24px 28px}}
.card{{background:#fff;border:2px solid #e4e7ec;border-radius:12px;overflow:hidden;cursor:pointer;position:relative;transition:.15s}}
body:not(.show-all) .card.other{{display:none}}
.card:hover{{border-color:#f0a03c;transform:translateY(-2px)}}
.card.picked{{border-color:#e2601a;box-shadow:0 0 0 3px rgba(226,96,26,.15)}}
.badge{{position:absolute;top:8px;left:8px;z-index:2;background:#1b1f24;color:#fff;border-radius:6px;padding:2px 9px;font-weight:700;font-size:13px}}
.tag{{position:absolute;top:8px;right:8px;z-index:2;font-size:11px;padding:3px 8px;border-radius:6px}}
.rel-tag{{background:#fff1e6;color:#b45309}}
.peer-tag{{background:#fde8e8;color:#9f2b2b}}
.own-tag{{background:#e8f4ff;color:#17558a}}
.other-tag{{background:#eef0f3;color:#667085}}
.banner{{margin-top:10px;padding:9px 13px;background:#eef6ff;border:1px solid #cfe3fb;border-radius:8px;color:#17457a;font-size:13px}}
.kw{{color:#8a6d3b;font-size:11px;margin:-4px 0 8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.geo{{background:#eefaf3;border-color:#c9eddc;color:#12664a;margin-top:6px}}
.thumb{{height:158px;background:#eceef1;overflow:hidden}}
.thumb img{{width:100%;height:100%;object-fit:cover;display:block}}
.noimg{{height:100%;display:grid;place-items:center;color:#98a2b3}}
.body{{padding:12px 14px 16px}}
h3{{margin:0 0 8px;font-size:14px;line-height:1.45;min-height:40px;max-height:60px;overflow:hidden}}
.meta{{margin:0 0 10px;color:#667085;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
a{{color:#c2410c;text-decoration:none;font-size:12px;font-weight:600}}
</style></head><body>
<header><h1>抖音候选视频 · 选一条作为基线</h1>
<div class="sub">搜索词：{kw or '（当前页面手动搜索）'} · 抓取时间：{ts}</div>
{geo_banner}
<div class="banner">对标对象：<b>本地周边潜在会员</b>（会来办卡、上体验课的消费者），不是健身房老板。默认只显示面向 C 端的搜索结果；<b>「同行经营」类教老板获客的视频已排除</b>，点「显示全部」可见，一条都没丢弃。</div>
<div class="sub">共 {len(items)} 条 · 搜索结果 {search_count} 条（本地消费者 {local_count} / 同行经营 {peer_count}）· 默认显示 {rel_count} 条</div>
<div class="toolbar"><button id="tgl" onclick="toggle()">显示全部</button>
<span class="small">「推荐流」是抖音在该搜索页混入的精选内容，与本次搜索无关，同样可选。</span></div>
<div id="sel"></div></header>
<div class="grid">{''.join(cards) or '<p style="padding:24px">未抓到候选，请先在浏览器窗口完成验证并进入搜索结果页。</p>'}</div>
<script>
let all=false;
function toggle(){{
  all=!all;
  document.body.classList.toggle('show-all',all);
  document.getElementById('tgl').textContent=all?'只看相关':'显示全部';
}}
function pick(n){{
  document.querySelectorAll('.card').forEach(c=>c.classList.toggle('picked', +c.dataset.no===n));
  const s=document.getElementById('sel');
  s.style.display='block';
  s.textContent='已选 #'+n+' —— 回到对话里回复「选 '+n+'」即可完成基线登记';
}}
</script></body></html>'''


def cmd_collect(args):
    global PLACE_TERMS, PLACE_NOTE
    if getattr(args, 'place', ''):
        PLACE_TERMS = [w.strip() for w in re.split(r'[\s,，/、]+', args.place) if w.strip()]
    PLACE_NOTE = getattr(args, 'place_note', '') or ''
    target_url = ''
    if args.goto:
        target_url = args.goto
    elif args.keyword:
        # 与用户手动搜索时同一套搜索页路径，登录态下程序导航不再撞验证码。
        target_url = ('https://www.douyin.com/jingxuan/search/'
                      + quote(args.keyword, safe='') + '?type=general')
    if target_url:
        pre = bridge_call('goto', url=target_url, wait=args.wait,
                          timeout=args.wait // 1000 + 30)
        if is_blocked(pre):
            print(f"BLOCKED: 导航后落在「{pre.get('title')}」，平台风控未完成。请在 Chrome 窗口里过验证。")
            print("body_head:", (pre.get('body_head') or pre.get('text') or '')[:200])
            return 2
        bridge_call('eval', expr=ACTIVATE_JS, timeout=30)
    value = bridge_call('eval', expr=SCRAPE_JS)
    title = value.get('title') or ''
    if is_blocked(value):
        print(f"BLOCKED: 页面为「{title}」，平台风控未完成。请在 Chrome 窗口里过验证/登录后重试。")
        print("body_head:", (value.get('body_head') or '')[:200])
        return 2
    items = normalize(value)
    if not items:
        print("EMPTY: 页面已加载但没抓到视频卡片。请确认窗口当前是抖音搜索结果页。")
        print("url:", value.get('url'), "title:", title)
        print("body_head:", (value.get('body_head') or '')[:300])
        return 3
    BASE.mkdir(parents=True, exist_ok=True)
    # 手动搜索时没传 --keyword，也要从页面 URL 还原真实搜索词，别在清单上留空。
    kw_label = args.keyword or ''
    if not kw_label and value.get('url'):
        m = re.search(r'/search/([^?/#]+)', value['url'])
        if m:
            kw_label = unquote(m.group(1))
    meta = {
        'keyword': kw_label,
        'page_url': value.get('url'),
        'page_title': title,
        'captured_at': datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'),
        'source': 'douyin_web_search_via_logged_in_browser',
        'place': getattr(args, 'place', '') or '',
        'place_note': PLACE_NOTE,
        'place_source': '用户口述「厦门，翔安，浯家」；街道/户数/周边信息来自公开地图与翔安区政府公开资料，非用户逐项确认',
        'audience_definition': '凌扬运动=健身房；目标受众=门店本地周边潜在会员(C端)，非健身房老板/同行经营者',
        'audience_filter': '同行经营(B端)单独标注并排除默认视图；地名命中作为本地相关启发式信号；自家品牌单独标注',
        'evidence_note': '由用户本人登录的真实浏览器会话读取；未使用任何绕过风控/模拟登录手段',
    }
    payload = {'meta': meta, 'items': items}
    CANDIDATES.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    CANDIDATES_HTML.write_text(render_candidates(items, meta), encoding='utf-8')
    print(f"OK: {len(items)} 条候选 -> {CANDIDATES}")
    for it in items[:20]:
        print(f"  #{it['no']:>2} {it['title'][:52] or it['card_text'][:52]}  {it['url']}")
    return 0


def collect_keyword(keyword, wait):
    """导航 -> 页面内点击搜索按钮激活 SPA -> 取结构化读数。风控返回 (None, 原因)。"""
    url = ('https://www.douyin.com/jingxuan/search/'
           + quote(keyword, safe='') + '?type=general')
    pre = bridge_call('goto', url=url, wait=wait, timeout=wait // 1000 + 30)
    if is_blocked(pre):
        return None, f"BLOCKED(title={pre.get('title')})"
    bridge_call('eval', expr=ACTIVATE_JS, timeout=30)
    value = bridge_call('eval', expr=SCRAPE_JS)
    if is_blocked(value):
        return None, f"BLOCKED(captcha_iframes={value.get('captcha_iframes')})"
    return value, None


def cmd_multi(args):
    """多个关键词依次搜索，合并去重成一份候选清单（同城定向用）。"""
    global PLACE_TERMS, PLACE_NOTE
    if args.place:
        PLACE_TERMS = [w.strip() for w in re.split(r'[\s,，/、]+', args.place) if w.strip()]
    PLACE_NOTE = args.place_note or ''
    keywords = args.keywords or KEYWORDS_LOCAL
    # 先预检：已有可见验证框就别再连续导航，那只会加重风控。
    try:
        visible_captcha = bridge_call('eval', expr=CHECK_CAPTCHA_JS) or 0
    except Exception:
        visible_captcha = 0
    if visible_captcha:
        print(f"BLOCKED: 当前页面有 {visible_captcha} 个可见滑块验证框。")
        print("请在 Chrome 窗口里手动完成验证后重试；不绕过验证码、不模拟登录、不伪造结果。")
        return 2
    raw_items, per_kw, errors, urls = [], {}, [], []
    for kw in keywords:
        value, err = collect_keyword(kw, args.wait)
        if err:
            errors.append({'keyword': kw, 'error': err})
            print(f"WARN  {kw}: {err}")
            if 'captcha' in err.lower() or 'blocked' in err.lower():
                print("STOP: 触发平台验证，停止后续导航以免加重风控。已取得的读数照常出清单。")
                break
            continue
        n = 0
        for raw in value.get('items') or []:
            raw['keyword'] = kw
            raw_items.append(raw)
            n += 1
        per_kw[kw] = n
        urls.append(value.get('url'))
        print(f"  {kw}: {n} 条")
    if not raw_items:
        print("EMPTY: 所有关键词都未取得有效读数（风控或未渲染），不生成清单")
        return 3
    items = normalize({'items': raw_items})
    meta = {
        'keyword': ' / '.join(keywords),
        'place': args.place or '',
        'place_note': PLACE_NOTE,
        'place_source': '用户口述「厦门，翔安，浯家」；街道/户数/周边信息来自公开地图与翔安区政府公开资料，非用户逐项确认',
        'per_keyword_counts': per_kw,
        'errors': errors,
        'page_urls': urls,
        'captured_at': datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'),
        'source': 'douyin_web_search_via_logged_in_browser',
        'audience_definition': '凌扬运动=健身房；目标受众=门店本地周边潜在会员(C端)，非健身房老板/同行经营者',
        'audience_filter': '同行经营(B端)单独标注并排除默认视图；地名命中作为本地相关启发式信号',
        'evidence_note': '由用户本人登录的真实浏览器会话读取；未绕过风控、未模拟登录、未伪造数据；被风控拦截的词如实记入 errors',
    }
    BASE.mkdir(parents=True, exist_ok=True)
    CANDIDATES.write_text(json.dumps({'meta': meta, 'items': items}, ensure_ascii=False, indent=2), encoding='utf-8')
    CANDIDATES_HTML.write_text(render_candidates(items, meta), encoding='utf-8')
    local_n = sum(1 for i in items if i['audience'] == 'local')
    peer_n = sum(1 for i in items if i['audience'] == 'peer')
    print(f"OK: {len(items)} 条去重候选（local={local_n} / peer={peer_n} / 默认显示 {sum(1 for i in items if i['relevant'])}）-> {CANDIDATES}")
    for it in items[:25]:
        print(f"  #{it['no']:>2} [{it['audience']}] {it['title'][:52] or it['card_text'][:52]}")
    return 0


def cmd_save(args):
    if not CANDIDATES.exists():
        print("ERROR: 尚无候选清单，请先运行 collect")
        return 1
    data = json.loads(CANDIDATES.read_text(encoding='utf-8'))
    match = next((i for i in data['items'] if i['no'] == args.no), None)
    if not match:
        print(f"ERROR: 没有编号 {args.no} 的候选")
        return 1
    record = {
        'baseline_id': f"bl-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
        'selected_at': datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'),
        'selected_by': 'human',
        'purpose': args.purpose,
        'source': data['meta'],
        'video': match,
        'next_step': {
            'transcript': '人工听写该视频逐字稿 -> 存 data/baseline/human_transcript.txt -> 用 tests/quality_probe.py 对比 faster-whisper 输出算 WER/CER',
            'content': '拆解选题/钩子/脚本结构/镜头节奏，登记为创作模板',
            'both': '两条路径都走',
        }[args.purpose],
        'limitations': [
            '仅登记用户选定的公开视频 URL 与元数据，未下载视频、未取得平台授权',
            '人工基线文本尚未提供，WER/CER 客观读数当前不可得',
        ],
    }
    BASELINE_JSON.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"OK: 基线 #{args.no} 已登记 -> {BASELINE_JSON}")
    print(f"    {match['url']}")
    print(f"    用途: {args.purpose} | 下一步: {record['next_step']}")
    return 0


def cmd_guide(_args):
    status = {}
    try:
        status = json.loads((BR / 'status.json').read_text(encoding='utf-8'))
    except Exception:
        pass
    page = html.escape(status.get('url') or '(桥未运行)')
    ts = html.escape(status.get('ts') or '')
    BASE.mkdir(parents=True, exist_ok=True)
    GUIDE_HTML.write_text(f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>抖音基线选取 · 操作指引</title>
<style>:root{{color-scheme:light}}body{{margin:0;background:#f6f7f9;color:#1b1f24;font:15px/1.75 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
.wrap{{max-width:820px;margin:0 auto;padding:32px 24px 60px}}h1{{font-size:24px;margin:0 0 6px}}h2{{font-size:17px;margin:28px 0 10px}}
.card{{background:#fff;border:1px solid #e4e7ec;border-radius:12px;padding:18px 22px;margin:14px 0}}
.step{{display:flex;gap:12px;margin:10px 0}}.n{{flex:0 0 26px;height:26px;border-radius:7px;background:#1b1f24;color:#fff;display:grid;place-items:center;font-weight:700;font-size:13px}}
code{{background:#f1f3f6;padding:2px 6px;border-radius:5px;font-size:13px}}.warn{{background:#fff4e5;border-color:#f5d6a8;color:#7a4600}}
.small{{color:#667085;font-size:13px}}.kv{{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:#475467;word-break:break-all}}
</style></head><body><div class="wrap">
<h1>抖音基线选取 · 当前状态与操作指引</h1>
<div class="small">生成时间见文件；本页只陈述真实读数，不含推测结论。</div>

<div class="card warn"><b>方向已纠偏（2026-11-08）</b><br>
对标对象是<b>本地周边潜在会员</b>（会来办卡、上体验课的 C 端消费者），不是健身房老板／同行经营者。
旧关键词「健身房 获客」命中的 22 条里有 21 条是教老板做生意的 B 端内容，已作废。
候选清单默认排除「同行经营」类视频（仍可点「显示全部」查看，不删除）。
若窗口出现滑块验证，请在你自己的窗口里手动完成 —— 程序导航会触发风控，我不绕过、不模拟登录、不伪造结果。</div>

<div class="card"><h2 style="margin-top:0">需要你做的（在已打开的那个 Chrome 窗口里）</h2>
<div class="step"><div class="n">1</div><div>切到左上那个独立 Chrome 窗口（标题「抖音」／「验证码中间页」）。若被别的窗口挡住，从任务栏找 Chrome 图标。</div></div>
<div class="step"><div class="n">2</div><div>完成滑块／3D 验证；若弹出登录框，扫码或手机号登录。<b>请在你自己的窗口里操作</b>，我这边不接触账号密码。</div></div>
<div class="step"><div class="n">3</div><div>在抖音顶部搜索框<b>手动</b>输入 <code>翔安 健身房</code> 并回车（门店锚点：厦门 · 翔安 · 浯家，金海街道）。手动输入比程序导航更不容易触发风控。其他同城词：<code>厦门 翔安 健身房 探店</code>、<code>翔安 健身房 推荐</code>、<code>厦门 健身房 探店</code>。回车后停在结果页。</div></div>
<div class="step"><div class="n">4</div><div>回对话里说一声「好了」。我会立刻抓取结果、生成带封面和编号的候选清单给你挑。</div></div></div>

<div class="card"><h2 style="margin-top:0">我这边已就绪的链路</h2>
<div class="small">浏览器桥：<code>tools/douyin_bridge.mjs</code>（CDP 管道，不走被拦截的调试端口）<br>
抓取与渲染：<code>tools/baseline.py collect</code> → <code>data/baseline/candidates.html</code><br>
基线登记：<code>tools/baseline.py save &lt;编号&gt; --purpose transcript|content|both</code></div>
<p class="small" style="margin-bottom:0">Cookie 只留在独立 Chrome profile 内，不导出、不写进项目 JSON、不进代码。候选仅含公开视频的 URL／标题／封面／卡片文本。</p></div>

<div class="card"><h2 style="margin-top:0">桥的最近读数</h2>
<div class="kv">url: {page}<br>ts: {ts}</div></div>
</div></body></html>''', encoding='utf-8')
    print(f"OK: {GUIDE_HTML}")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('collect', help='抓取当前搜索结果页的候选视频')
    c.add_argument('--keyword', help='导航到该关键词搜索（自动构造抖音搜索页 URL）')
    c.add_argument('--goto', help='导航到指定完整 URL 后再抓取')
    c.add_argument('--place', help='门店锚点地名，空格/逗号分隔')
    c.add_argument('--place-note', dest='place_note', default='', help='锚点补充说明')
    c.add_argument('--wait', type=int, default=8000, help='导航后等待毫秒数')
    c.set_defaults(func=cmd_collect)
    m = sub.add_parser('multi', help='多个关键词依次搜索并合并去重成一份清单')
    m.add_argument('keywords', nargs='*', help='搜索词；留空则用内置 C 端词表')
    m.add_argument('--place', help='门店锚点地名，空格/逗号分隔，命中即视为本地相关')
    m.add_argument('--place-note', dest='place_note', default='', help='锚点补充说明，写入清单页')
    m.add_argument('--wait', type=int, default=3500, help='每次导航后等待毫秒数')
    m.set_defaults(func=cmd_multi)
    s = sub.add_parser('save', help='把选定编号登记为基线')
    s.add_argument('no', type=int)
    s.add_argument('--purpose', choices=['transcript', 'content', 'both'], default='both')
    s.set_defaults(func=cmd_save)
    g = sub.add_parser('guide', help='生成操作指引页')
    g.set_defaults(func=cmd_guide)
    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == '__main__':
    main()
