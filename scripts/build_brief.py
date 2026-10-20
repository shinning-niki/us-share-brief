#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股收盘简报 · 手机端页面生成
读 data/brief.json -> dist/index.html（单文件、零外部依赖、移动优先）

信息架构（与 A 股简报同构，但按美股的市场结构替换变量）：
  1. 指数条（五大指数 + 振幅/广度/美元黄金美债，合并为一条信息带）
  2. 板块强弱（11 个 GICS 行业 ETF 涨跌条形可视化）
  3. 权重股（MAG7）—— 美股指数方向的直接决定者
  4. 盘面归因（结构 / 广度 / 量能 / 主线 / 催化 / 宏观）
  5. 今日要闻（最多 3 条）
  6. 明日展望（机构策略研报观点）

配色沿用「红涨绿跌」，与 A 股简报保持一致，避免两套简报间切换的认知成本。
"""

import html
import json
import os
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "brief.json")
DIST = os.path.join(ROOT, "dist")

INDEX_ORDER = ["道琼斯", "纳斯达克", "标普500", "罗素2000", "费城半导体"]
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def esc(s):
    return html.escape(str(s if s is not None else ""))


def cls(v):
    return "up" if v > 0 else ("down" if v < 0 else "flat")


def pct_text(v, sign=True):
    return (f"{v:+.2f}%" if sign else f"{v:.2f}%")


# ------------------------------------------------------------ 1. 指数条
def hero(b):
    idx = b.get("indices") or {}
    amt = b.get("amount") or {}
    macro = b.get("macro") or {}

    chips = []
    for k in INDEX_ORDER:
        q = idx.get(k)
        if not q:
            continue
        proxy = q.get("proxy") or ""
        # 用 ETF 代理的标的（罗素2000=IWM、费半=SMH）点位不是指数点位，只给涨跌幅
        if proxy:
            val = f'<span class="chip-tag">{esc(proxy)}</span>'
            sub = pct_text(q["pct"])
        else:
            val = f"{q['close']:,.2f}"
            sub = pct_text(q["pct"])
        chips.append(f"""
      <div class="chip">
        <div class="chip-name">{esc(k)}</div>
        <div class="chip-val {cls(q['pct'])}">{val}</div>
        <div class="chip-pct {cls(q['pct'])}">{sub}</div>
      </div>""")

    facts = []
    spx = idx.get("标普500") or {}
    if spx.get("prev"):
        amp = (spx["high"] - spx["low"]) / spx["prev"] * 100
        facts.append(f'<span class="fact">标普日内振幅 <b>{amp:.2f}%</b></span>')

    spy = (macro.get("标普500ETF") or {}).get("pct")
    rsp = (macro.get("标普等权") or {}).get("pct")
    if spy is not None and rsp is not None:
        gap = spy - rsp
        word = "市值加权占优" if gap > 0.15 else ("等权占优" if gap < -0.15 else "两者接近")
        facts.append(f'<span class="fact">广度 <b class="{cls(gap)}">{gap:+.2f}</b> pct · {word}</span>')

    if amt.get("total"):
        d = amt.get("delta")
        seg = f'<b>{amt["total"]:,.0f}</b> 亿美元'
        if d is not None:
            word = "放量" if d > 0 else ("缩量" if d < 0 else "持平")
            seg += f'<span class="sep">·</span>较上日{word} <b class="{cls(d)}">{abs(d):,.0f}</b> 亿'
        facts.append(f'<span class="fact">SPY+QQQ 成交 {seg}</span>')

    anchors = []
    for k in ("20年美债", "美元", "黄金"):
        q = macro.get(k)
        if q:
            anchors.append(f'<span class="anchor">{esc(k)} '
                           f'<b class="{cls(q["pct"])}">{pct_text(q["pct"])}</b></span>')
    anchor_html = (f'<div class="anchors">{"".join(anchors)}</div>' if anchors else "")
    fact_html = f'<div class="facts">{"".join(facts)}</div>' if facts else ""

    return f"""
  <section class="hero">
    <div class="chips">{"".join(chips)}
    </div>
    {fact_html}
    {anchor_html}
  </section>"""


# ------------------------------------------------------------ 2. 板块
def sector_rows(rows, max_abs, kind):
    out = []
    for r in rows:
        pct = r.get("pct", 0)
        w = max(min(abs(pct) / (max_abs or 1) * 100, 100), 3)
        out.append(f"""
      <div class="srow">
        <div class="sname">{esc(r.get("name", ""))}</div>
        <div class="sbar {kind}"><i class="{kind}" style="width:{w:.1f}%"></i></div>
        <div class="spct {kind}">{pct_text(pct)}</div>
      </div>""")
    return "".join(out)


def sectors_block(b):
    sec = b.get("sectors") or []
    if not sec:
        return ""
    ranked = sorted(sec, key=lambda x: -x["pct"])
    up = [r for r in ranked if r["pct"] > 0]
    down = [r for r in ranked if r["pct"] < 0]
    flat = [r for r in ranked if r["pct"] == 0]
    max_abs = max([abs(r["pct"]) for r in sec] or [1]) or 1
    total = len(sec)
    up_ratio = len(up) / total * 100 if total else 50

    breadth = f"""
    <div class="breadth">
      <div class="b-up" style="width:{up_ratio:.1f}%"></div>
      <div class="b-down" style="width:{100 - up_ratio:.1f}%"></div>
    </div>
    <div class="breadth-legend">
      <span class="up">上涨 {len(up)}</span>
      <span class="muted">共 {total} 个 GICS 行业</span>
      <span class="down">下跌 {len(down)}</span>
    </div>"""

    groups = []
    if up:
        groups.append(f"""
    <div class="grp">
      <div class="grp-title up">上涨行业</div>
      {sector_rows(up, max_abs, 'up')}
    </div>""")
    if down:
        groups.append(f"""
    <div class="grp">
      <div class="grp-title down">下跌行业</div>
      {sector_rows(sorted(down, key=lambda x: x["pct"]), max_abs, 'down')}
    </div>""")
    if flat:
        groups.append(f"""
    <div class="grp">
      <div class="grp-title flat">持平</div>
      {sector_rows(flat, max_abs, 'flat')}
    </div>""")

    return f"""
  <section class="block">
    <h2>板块强弱<span class="h2-sub">11 个 GICS 行业 SPDR ETF</span></h2>
    {breadth}
    {"".join(groups)}
  </section>"""


# ------------------------------------------------------------ 3. 权重股
def mag7_block(b):
    rows = b.get("mag7") or []
    if not rows:
        return ""
    ranked = sorted(rows, key=lambda x: -x["pct"])
    chips = []
    for r in ranked:
        chips.append(f"""
      <div class="mchip">
        <div class="mname">{esc(r["name"])}</div>
        <div class="mpct {cls(r['pct'])}">{pct_text(r['pct'])}</div>
      </div>""")
    avg = sum(r["pct"] for r in rows) / len(rows)
    spx = ((b.get("indices") or {}).get("标普500") or {}).get("pct")
    gap = ""
    if spx is not None:
        d = avg - spx
        gap = (f'<span class="h2-sub">均值 {avg:+.2f}% · '
               f'较标普 {"高" if d >= 0 else "低"} {abs(d):.2f} pct</span>')
    return f"""
  <section class="block">
    <h2>权重股 MAG7{gap}</h2>
    <div class="mgrid">{"".join(chips)}
    </div>
  </section>"""


# ------------------------------------------------------------ 4. 归因
def attribution_block(b):
    blocks = b.get("attribution") or []
    if not blocks:
        return ""
    rows = []
    for x in blocks:
        rows.append(f"""
      <div class="arow">
        <div class="akey">{esc(x.get('k', ''))}</div>
        <div class="atext">{esc(x.get('t', ''))}</div>
      </div>""")
    return f"""
  <section class="block">
    <h2>盘面归因<span class="h2-sub">由盘面数据与当日报道交叉推导</span></h2>
    {"".join(rows)}
    <p class="disclaimer">归因为相关性对齐，不能直接认定为涨跌原因；不构成投资建议。</p>
  </section>"""


# ------------------------------------------------------------ 5. 要闻
def news_block(b):
    news = b.get("news") or []
    if not news:
        return """
  <section class="block">
    <h2>今日要闻</h2>
    <p class="empty-note">未检索到满足条件的当日重大消息。</p>
  </section>"""
    cards = []
    for n in news:
        media = n.get("media") or ""
        m_html = f'<span class="ntag">{esc(media)}</span>' if media else ""
        # 外媒条目附英文原题：中文是机器翻译，原文可供核对，避免译错被当成事实
        en = n.get("title_en") or ""
        en_html = f'<div class="ntitle-en">{esc(en)}</div>' if en else ""
        cards.append(f"""
      <article class="ncard">
        <div class="nhead"><span class="ntime">{esc(n.get('time', ''))}</span>{m_html}</div>
        <div class="ntitle">{esc(n.get('title', ''))}</div>
        {en_html}
        <p class="nbody">{esc(n.get('summary', ''))}</p>
        <a class="src" href="{esc(n.get('url', '#'))}" target="_blank" rel="noopener">查看原文 ↗</a>
      </article>""")
    src = "外媒一手信源 · 中文为机器翻译" if any(
        n.get("kind") == "foreign" for n in news) else ""
    sub = f'<span class="h2-sub">{esc(src)}</span>' if src else f'<span class="h2-sub">Top {len(news)}</span>'
    return f"""
  <section class="block">
    <h2>今日要闻{sub}</h2>
    {"".join(cards)}
  </section>"""


# ------------------------------------------------------------ 6. 明日事件
def calendar_block(b):
    cal = b.get("calendar") or {}
    earn = cal.get("earnings") or []
    econ = cal.get("econ") or []
    if not earn and not econ:
        return ""

    rows = []
    for e in earn:
        when = f'<span class="cal-when">{esc(e["when"])}</span>' if e.get("when") else ""
        eps = f'<span class="cal-x">EPS 预期 {esc(e["eps"])}</span>' if e.get("eps") else ""
        rows.append(f"""
      <div class="cal-row">
        <div class="cal-k">财报</div>
        <div class="cal-main"><b>{esc(e.get('symbol', ''))}</b>
          <span class="cal-sub">{esc((e.get('name') or '')[:26])}</span></div>
        <div class="cal-v">{when}{eps}</div>
      </div>""")
    for e in econ:
        cons = f'预期 <b>{esc(e["consensus"])}</b>' if e.get("consensus") else ""
        prev = f' 前值 {esc(e["previous"])}' if e.get("previous") else ""
        t = f'<span class="cal-when">{esc(e["time"])}</span>' if e.get("time") else ""
        rows.append(f"""
      <div class="cal-row">
        <div class="cal-k">数据</div>
        <div class="cal-main"><b>{esc(e.get('name', ''))}</b></div>
        <div class="cal-v">{t}{cons}{prev}</div>
      </div>""")

    return f"""
  <section class="block">
    <h2>明日事件<span class="h2-sub">{esc(cal.get('date', ''))} · 美东时间 · 确定性事件而非观点</span></h2>
    {"".join(rows)}
  </section>"""


# ------------------------------------------------------------ 7. 明日展望
def outlook_block(b):
    ot = b.get("outlook") or []
    if not ot:
        return """
  <section class="block">
    <h2>明日展望</h2>
    <p class="empty-note">未获取到可用的机构观点，不做方向性判断。</p>
  </section>"""
    cards = []
    foreign = any(o.get("kind") == "foreign" for o in ot)
    for o in ot:
        digest = o.get("digest") or ""
        d_html = f'<p class="nbody">{esc(digest)}</p>' if digest else ""
        en = o.get("title_en") or ""
        en_html = f'<div class="ntitle-en">{esc(en)}</div>' if en else ""
        cards.append(f"""
      <article class="ncard ocard">
        <div class="nhead"><span class="org">{esc(o.get('org', ''))}</span>
          <span class="ntime">{esc(o.get('date', ''))}</span></div>
        <div class="ntitle">{esc(o.get('title', ''))}</div>
        {en_html}
        {d_html}
        <a class="src" href="{esc(o.get('url', '#'))}" target="_blank" rel="noopener">原文 ↗</a>
      </article>""")
    sub = "外媒观点 · 非投资建议" if foreign else "中文研报 · 外媒观点缺失时降级 · 非投资建议"
    return f"""
  <section class="block">
    <h2>明日展望<span class="h2-sub">{sub}</span></h2>
    {"".join(cards)}
  </section>"""


def build():
    with open(DATA, "r", encoding="utf-8") as f:
        b = json.load(f)

    trade_date = b.get("trade_date") or b.get("date", "")
    weekday = ""
    try:
        weekday = WEEKDAY_CN[datetime.strptime(trade_date, "%Y-%m-%d").weekday()]
    except Exception:
        weekday = b.get("weekday", "")

    if not b.get("is_trading_day"):
        body = f"""
  <section class="empty">
    <div class="empty-icon">休</div>
    <h2>今日无美股交易</h2>
    <p>{esc(b.get('reason', '非交易日，无简报'))}</p>
  </section>"""
    else:
        body = (hero(b) + sectors_block(b) + mag7_block(b)
                + attribution_block(b) + news_block(b) + calendar_block(b)
                + outlook_block(b))

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0d1117">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>美股收盘简报 {esc(trade_date)}</title>
<style>
  *,*::before,*::after{{box-sizing:border-box;-webkit-tap-highlight-color:transparent}}
  :root{{
    --bg:#0d1117; --card:#161b22; --card2:#1c232c; --line:#222b36;
    --txt:#e6edf3; --muted:#8b949e; --dim:#6e7681;
    --up:#ff4d4f; --up-soft:rgba(255,77,79,.16);
    --down:#2ea043; --down-soft:rgba(46,160,67,.16);
    --flat:#8b949e;
  }}
  html,body{{margin:0;padding:0;background:var(--bg);color:var(--txt)}}
  body{{
    font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
    font-size:15px;line-height:1.68;
    padding:calc(env(safe-area-inset-top) + 18px) 15px calc(env(safe-area-inset-bottom) + 30px);
    max-width:540px;margin:0 auto;-webkit-font-smoothing:antialiased;
  }}
  .up{{color:var(--up)}} .down{{color:var(--down)}} .flat{{color:var(--flat)}}
  .muted{{color:var(--muted)}}

  header{{margin-bottom:16px}}
  .eyebrow{{font-size:11px;color:var(--dim);letter-spacing:.16em;text-transform:uppercase}}
  h1{{font-size:24px;line-height:1.25;margin:5px 0 4px;letter-spacing:-.01em;font-weight:700}}
  .meta{{font-size:12.5px;color:var(--muted)}}
  .meta b{{color:#c9d1d9;font-weight:600}}

  /* ---- 指数 ---- */
  .hero{{margin-bottom:20px}}
  .chips{{display:grid;grid-template-columns:repeat(3,1fr);gap:7px}}
  .chip{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:9px 10px 8px;min-width:0}}
  .chip-name{{font-size:11px;color:var(--muted);margin-bottom:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
  .chip-val{{font-size:15.5px;font-weight:680;letter-spacing:-.02em;font-variant-numeric:tabular-nums;line-height:1.3}}
  .chip-tag{{font-size:11px;font-weight:600;color:#8b949e;border:1px solid #2a343f;border-radius:5px;padding:1px 5px}}
  .chip-pct{{font-size:11.5px;font-weight:600;font-variant-numeric:tabular-nums}}
  .facts{{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:10px;font-size:12.5px;color:var(--muted)}}
  .fact b{{color:#e6edf3;font-weight:650;font-variant-numeric:tabular-nums}}
  .fact .sep{{margin:0 5px;color:#3a434e}}
  .anchors{{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}}
  .anchor{{font-size:11.5px;color:var(--dim);background:var(--card2);border:1px solid var(--line);
          border-radius:20px;padding:2px 9px}}
  .anchor b{{font-variant-numeric:tabular-nums;font-weight:650}}

  /* ---- 区块 ---- */
  .block{{margin:0 0 22px}}
  h2{{font-size:14px;font-weight:650;margin:0 0 10px;letter-spacing:.01em;display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}}
  .h2-sub{{font-size:11px;color:var(--dim);font-weight:400;letter-spacing:0}}

  /* ---- 板块 ---- */
  .breadth{{display:flex;height:6px;border-radius:4px;overflow:hidden;background:#232b35;margin-bottom:6px}}
  .b-up{{background:var(--up)}}
  .b-down{{background:var(--down)}}
  .breadth-legend{{display:flex;justify-content:space-between;font-size:11.5px;margin-bottom:14px}}
  .grp{{margin-bottom:14px}}
  .grp:last-child{{margin-bottom:0}}
  .grp-title{{font-size:11.5px;font-weight:600;letter-spacing:.06em;margin-bottom:7px;display:flex;align-items:center;gap:6px}}
  .grp-title::after{{content:"";flex:1;height:1px;background:var(--line)}}
  .grp-title.up::after{{background:linear-gradient(90deg,rgba(255,77,79,.32),transparent)}}
  .grp-title.down::after{{background:linear-gradient(90deg,rgba(46,160,67,.32),transparent)}}
  .srow{{display:grid;grid-template-columns:82px 1fr 56px;align-items:center;gap:9px;padding:4px 0}}
  .sname{{font-size:12.5px;color:#c9d1d9;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;line-height:1.3}}
  .sbar{{height:9px;background:#1c232c;border-radius:5px;overflow:hidden;display:flex}}
  .sbar.down{{justify-content:flex-end}}
  .sbar i{{display:block;height:100%;border-radius:5px}}
  .sbar i.up{{background:linear-gradient(90deg,rgba(255,77,79,.55),var(--up))}}
  .sbar i.down{{background:linear-gradient(90deg,rgba(46,160,67,.55),var(--down))}}
  .sbar i.flat{{background:#4a5560}}
  .spct{{font-size:12.5px;font-weight:650;text-align:right;font-variant-numeric:tabular-nums}}

  /* ---- MAG7 ---- */
  .mgrid{{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}}
  .mchip{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:7px 8px;min-width:0}}
  .mname{{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
  .mpct{{font-size:13.5px;font-weight:680;font-variant-numeric:tabular-nums;letter-spacing:-.01em}}

  /* ---- 归因 ---- */
  .arow{{display:grid;grid-template-columns:44px 1fr;gap:10px;padding:9px 0;border-bottom:1px solid var(--line)}}
  .arow:last-of-type{{border-bottom:0}}
  .akey{{font-size:11.5px;font-weight:600;color:#58a6ff;letter-spacing:.05em;padding-top:2px}}
  .atext{{font-size:13.5px;line-height:1.68;color:#d5dde6}}
  .disclaimer{{margin:10px 0 0;font-size:11px;color:#4a5560;line-height:1.7}}

  /* ---- 卡片 ---- */
  .ncard{{background:var(--card);border:1px solid var(--line);border-radius:13px;padding:12px 13px 10px;margin-bottom:9px}}
  .ncard:last-child{{margin-bottom:0}}
  .nhead{{display:flex;align-items:center;gap:8px;margin-bottom:5px}}
  .ntime{{font-size:11px;color:var(--dim);font-variant-numeric:tabular-nums}}
  .ntag{{font-size:10px;color:#8b949e;border:1px solid #2a343f;border-radius:20px;padding:0 6px;line-height:1.7;
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:170px}}
  .org{{font-size:11.5px;font-weight:650;color:#58a6ff}}
  .ntitle{{font-size:14px;line-height:1.5;font-weight:600;color:#e6edf3;margin-bottom:4px;
          display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}}
  .nbody{{margin:0 0 7px;font-size:13px;line-height:1.68;color:#aeb8c2}}
  .src{{font-size:11.5px;color:#58a6ff;text-decoration:none;opacity:.9}}
  .src:active{{opacity:.5}}
  .ocard .ntitle{{font-size:13.5px}}
  .ntitle-en{{font-size:11px;line-height:1.5;color:#5c6773;margin:-2px 0 6px;
             display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
  .empty-note{{font-size:12.5px;color:var(--dim);margin:0}}

  /* ---- 明日事件 ---- */
  .cal-row{{display:grid;grid-template-columns:38px 1fr auto;gap:9px;align-items:baseline;
           padding:7px 0;border-bottom:1px solid var(--line);font-size:12.5px}}
  .cal-row:last-child{{border-bottom:0;padding-bottom:0}}
  .cal-k{{font-size:10.5px;color:#58a6ff;letter-spacing:.04em}}
  .cal-main{{color:#d5dde6;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  .cal-main b{{font-weight:650;color:#e6edf3}}
  .cal-sub{{color:var(--dim);font-size:11px;margin-left:5px}}
  .cal-v{{font-size:11.5px;color:var(--muted);text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
  .cal-v b{{color:#e6edf3;font-weight:650}}
  .cal-when{{display:inline-block;font-size:10px;color:#8b949e;border:1px solid #2a343f;
            border-radius:4px;padding:0 4px;margin-right:5px}}
  .cal-x{{color:var(--dim);font-size:11px}}

  .empty{{text-align:center;padding:56px 20px;color:var(--muted)}}
  .empty-icon{{width:52px;height:52px;border-radius:16px;background:var(--card);border:1px solid var(--line);
       display:flex;align-items:center;justify-content:center;margin:0 auto 14px;font-size:20px;color:#484f58}}
  .empty h2{{font-size:17px;color:var(--txt);margin:0 0 6px;justify-content:center}}
  .empty p{{font-size:13px;margin:0}}

  footer{{margin-top:26px;padding-top:13px;border-top:1px solid var(--line);
          font-size:11px;color:#4a5560;line-height:1.85}}
</style>
</head>
<body>
  <header>
    <div class="eyebrow">US Stock Close Brief</div>
    <h1>美股收盘简报</h1>
    <div class="meta"><b>{esc(trade_date)}</b> {esc(weekday)}(美东) · 生成于 {esc(b.get('generated_at',''))}(北京)</div>
  </header>
{body}
  <footer>
    行情数据：腾讯行情（指数/ETF/个股）。要闻与观点：WSJ · CNBC · MarketWatch · 美联储官网（英文原文机器翻译，附原文链接可核对）。
    明日事件：Nasdaq 财报与经济数据日历。国内资讯仅在全部外媒不可用时降级兜底。<br>
    罗素2000 以 IWM、费城半导体以 SMH 作为代理标的，点位非指数原值，仅取涨跌幅。<br>
    归因为相关性判断，不构成因果结论；不构成投资建议。红涨绿跌。
  </footer>
</body>
</html>
"""
    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, "index.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"[OK] {out} ({len(doc)} bytes)")


if __name__ == "__main__":
    build()
