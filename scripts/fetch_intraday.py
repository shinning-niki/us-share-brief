#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""美股盘前 / 盘中简报 · 抓取与渲染

与收盘简报的分工
----------------
  收盘简报（daily-brief.yml）  美东 17:30 之后，一份完整复盘 + 要闻 + 展望
  本文件  pre  美东 09:00     开盘前 30 分钟，盘前交易的资金流向
  本文件  mid  美东 10:30     开盘后 60 分钟，开盘一小时的真实强弱

为什么不用收盘简报的腾讯接口
----------------------------
腾讯 qt.gtimg.cn 在盘前时段不更新：字段 30 的行情时间戳仍停在上一交易日
16:00，f[3] 是昨日收盘价而不是盘前价。拿它做盘前简报会把昨天的收盘当成
今天的盘前，这是最坏的一种错误——看起来有数据，实际全是过期的。

改用新浪美股 hq.sinajs.cn/list=gb_<ticker>，它有独立的盘前盘后字段。
字段布局（实测 2026-11-06，四项交叉验算通过）：

    f[1]   昨收（未开盘时）/ 最新价（盘中）
    f[2]   涨跌幅 %
    f[4-6] 今开 / 最高 / 最低
    f[11]  成交量
    f[21]  盘前（或盘后）成交价
    f[22]  盘前涨跌幅 %
    f[23]  盘前涨跌额
    f[24]  盘前行情时间，形如 "Nov 06 09:15AM EST"
    f[26]  基准昨收
    f[27]  盘前成交量

验算：NVDA 昨收 181.60 + 盘前额 7.71 = 189.31 = f[21]；7.71 / 181.60 = 4.25% = f[22]。
SMH、IWM、AAPL 三只同样吻合。

不直接信任 f[2] / f[22] 的百分比，而是用「成交价 / f[26] 基准」自己算一遍：
字段在不同标的上偶有错位（f[21] 与 f[22] 的语义在个别代码上会串），
但价格和基准是硬数据，自算的涨跌幅永远对得上。

指数本身没有盘前交易
--------------------
gb_$dji / gb_$ixic / gb_$inx 在盘前时段 f[21] 恒为 0，f[24] 是空的。
所以盘前/盘中的「指数」一律用跟踪它的 ETF 代理，并在正文里写明，
不能让读者以为是指数本身的盘前报价。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from fetch_brief import (  # noqa: E402
    fetch_econ_events, fetch_earnings, fetch_foreign_news, fnum,
    pick_foreign_news, translate)

ET = ZoneInfo("America/New_York")
CN = timezone(timedelta(hours=8))
DATA_DIR = os.path.join(ROOT, "data")
OUT_PATH = os.path.join(DATA_DIR, "intraday.json")
SNAP_DIR = os.path.join(DATA_DIR, "sessions")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# --------------------------------------------------------------- 标的池
# 指数用 ETF 代理：盘前时段指数本身没有报价，ETF 才有连续交易
INDEX_PROXY = [
    ("dia", "道琼斯", "DIA"),
    ("qqq", "纳斯达克", "QQQ"),
    ("spy", "标普500", "SPY"),
    ("iwm", "罗素2000", "IWM"),
    ("smh", "费城半导体", "SMH"),
]

SECTOR = [
    ("xlk", "科技"), ("xlc", "通信服务"), ("xly", "可选消费"),
    ("xlp", "必需消费"), ("xlf", "金融"), ("xlv", "医疗保健"),
    ("xli", "工业"), ("xlb", "材料"), ("xle", "能源"),
    ("xlu", "公用事业"), ("xlre", "房地产"),
]

MAG7 = [
    ("nvda", "英伟达"), ("aapl", "苹果"), ("msft", "微软"), ("googl", "谷歌"),
    ("amzn", "亚马逊"), ("meta", "Meta"), ("tsla", "特斯拉"),
]

MACRO = [
    ("tlt", "20年美债"), ("uup", "美元"), ("gld", "黄金"), ("uso", "原油"),
]

# 异动观察池：高关注度、盘前流动性好的名字。
# 不进「七巨头」，只在涨跌幅超过阈值时出现在「异动」一行。
WATCH = [
    ("amd", "超威半导体"), ("avgo", "博通"), ("tsm", "台积电"),
    ("mu", "美光"), ("qcom", "高通"), ("intc", "英特尔"), ("arm", "ARM"),
    ("orcl", "甲骨文"), ("pltr", "Palantir"), ("nflx", "奈飞"),
    ("jpm", "摩根大通"), ("gs", "高盛"), ("xom", "埃克森美孚"),
    ("lly", "礼来"), ("unh", "联合健康"), ("wmt", "沃尔玛"),
    ("cost", "好市多"), ("ba", "波音"), ("cat", "卡特彼勒"),
    ("coin", "Coinbase"), ("mstr", "MicroStrategy"), ("hood", "Robinhood"),
    ("sofi", "SoFi"), ("rivn", "Rivian"), ("uber", "优步"), ("shop", "Shopify"),
]

MOVE_THRESHOLD = 3.0   # 异动阈值（%）
MOVE_LIMIT = 5         # 最多列几个


# --------------------------------------------------------------- 抓取
def _get(codes, retries=3):
    """新浪批量行情。一次 30 个代码稳定，超出分批。"""
    out = {}
    for i in range(0, len(codes), 30):
        batch = codes[i:i + 30]
        url = "https://hq.sinajs.cn/list=" + ",".join("gb_" + c for c in batch)
        last = None
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": UA, "Referer": "https://finance.sina.com.cn/"})
                raw = urllib.request.urlopen(req, timeout=20).read()
                out.update(_parse(raw.decode("gbk", errors="ignore")))
                last = None
                break
            except Exception as e:                     # noqa: BLE001
                last = e
                time.sleep(1.5 * (attempt + 1))
        if last:
            print(f"[WARN] 批次 {i // 30} 抓取失败：{last}", file=sys.stderr)
        time.sleep(0.3)
    return out


def _parse(text):
    out = {}
    for line in (text or "").split(";"):
        line = line.strip()
        if '="' not in line or not line.startswith("var hq_str_"):
            continue
        key = line.split("=", 1)[0].replace("var hq_str_", "").strip()
        body = line.split('="', 1)[1].rstrip('"')
        if not body:
            continue
        f = body.split(",")
        if len(f) < 27:
            continue
        out[key.replace("gb_", "")] = f
    return out


def quote(f, stage):
    """从原始字段解出一个标的的当前状态。价格与基准都是硬数据，涨跌幅自算。"""
    if not f or len(f) < 27:
        return None
    base = fnum(f[26]) or fnum(f[1])
    if base <= 0:
        return None

    if stage == "pre":
        price = fnum(f[21]) or fnum(f[1])
        ts = f[24].strip() if len(f) > 24 else ""
        vol = fnum(f[27]) if len(f) > 27 else 0.0
        # 盘前没有任何成交时 f[21] 为 0，回退到昨收，涨跌幅自然是 0
        has_pre = fnum(f[21]) > 0
    else:
        price = fnum(f[1])
        ts = f[3].strip() if len(f) > 3 else ""
        vol = fnum(f[11])
        has_pre = True

    if price <= 0:
        return None
    return {
        "name": f[0].strip(),
        "price": round(price, 2),
        "base": round(base, 2),
        "pct": round((price - base) / base * 100, 2),
        "ts": ts,
        "vol": vol,
        "has_pre": has_pre,
    }


def et_date_of(ts, stage):
    """把行情时间戳统一换算成「美东日期」。

    两个阶段的原始格式完全不同，不统一就会误判休市：
      pre  f[24] = "Nov 06 09:15AM EST"        美东时间，无年份
      mid  f[3]  = "2026-11-06 22:16:21"       北京时间

    必须都归到美东日期再比，不能直接比字符串——北京 11-07 05:00 对应
    美东 11-06 16:00，日期差一天。
    """
    ts = (ts or "").strip()
    now_et = datetime.now(ET)
    if stage == "pre":
        m = re.match(r"([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{1,2}):(\d{2})(AM|PM)", ts)
        if not m:
            return ""
        mon, day, hh, mm, ap = m.groups()
        hour = int(hh) % 12 + (12 if ap == "PM" else 0)
        try:
            return datetime.strptime(f"{now_et.year} {mon} {day} {hour:02d}:{mm}",
                                     "%Y %b %d %H:%M").strftime("%Y-%m-%d")
        except ValueError:
            return ""
    try:
        cn = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN)
    except ValueError:
        return ""
    return cn.astimezone(ET).strftime("%Y-%m-%d")


def session_date_hits(quotes, stage):
    """统计有多少标的的行情时间落在「美东今天」，用于识破休市日。

    节假日没有交易，行情时间会全部停在上一交易日。此时若照发，正文
    会把昨天的价格当成今天的。所以先看命中率，全不命中就判休市跳过。
    """
    today = datetime.now(ET).strftime("%Y-%m-%d")
    hit = 0
    for q in quotes:
        if et_date_of(q.get("ts") or "", stage) == today:
            hit += 1
    return hit, len(quotes)


# --------------------------------------------------------------- 判断
def describe(idx, sectors, mag7, macro):
    """把一堆数字压成两句人话。

    只给判断，不复述数字——数字在上面那行已经出现过一次，
    正文再念一遍就变成了机器朗读，用户看不动。
    """
    allpct = ([v["pct"] for v in idx.values()]
              + [s["pct"] for s in sectors]
              + [m["pct"] for m in mag7])
    n = len(allpct)
    up = sum(1 for p in allpct if p > 0)
    avg = sum(allpct) / n if n else 0.0
    spx = idx.get("标普500", {}).get("pct", 0.0)

    # 指数方向与广度背离时，比单纯数涨跌家数更有信息量——这是「指数涨、
    # 多数股票跌」的窄幅行情，是风险偏好最脆弱的一种形态。
    max_abs = max((abs(p) for p in allpct), default=0.0)
    if max_abs < 0.2:
        # 最大波动都不到 0.2%，说明盘前几乎没有成交，不是「普涨」而是没方向
        breadth = "几乎全线走平，盘前还没有形成方向"
    elif spx > 0.2 and up < n * 0.45:
        breadth = "指数在涨但多数标的在跌，是少数板块撑起来的"
    elif spx < -0.2 and up > n * 0.55:
        breadth = "指数在跌但多数标的在涨，杀的是权重股"
    elif up >= n * 0.7:
        breadth = "普涨"
    elif up >= n * 0.55:
        breadth = "涨多跌少"
    elif up >= n * 0.45:
        breadth = "多空混杂"
    elif up >= n * 0.3:
        breadth = "跌多涨少"
    else:
        breadth = "普跌"

    semi = idx.get("费城半导体", {}).get("pct", 0.0)
    gap = semi - spx
    big = sum(m["pct"] for m in mag7) / len(mag7) if mag7 else 0.0
    small = idx.get("罗素2000", {}).get("pct", 0.0)

    if gap >= 2.0:
        struct = "涨幅高度集中在芯片链"
    elif gap <= -2.0:
        struct = "半导体明显拖后腿，风险偏好正在收缩"
    elif big - spx >= 1.0:
        struct = "权重股领跑，资金回到大市值"
    elif big - spx <= -1.0:
        # 只说「资金扩散」是不成立的：得先看中小盘有没有真的接住。
        # 权重跌、中小盘也跌，那是全面撤退，不是轮动。
        struct = ("权重股跑输，资金向中小盘扩散" if small - spx > 0.3
                  else "权重股领跌，中小盘也没接住，是全面减仓")
    elif abs(spx) < 0.3:
        struct = "指数几乎没动，强弱全在结构里"
    else:
        struct = "整体同向，没有结构背离"

    if max_abs < 0.2:
        struct = ""          # 走平时不再补一句「指数没动」，那是同一句话说两遍

    return {
        "breadth": breadth,
        "up": up,
        "total": n,
        "avg": round(avg, 2),
        "struct": struct,
    }


def format_pct(v):
    # -0.04 会显示成 "-0.0%"，看着像笔误。四舍五入到 0 的改用「持平」，
    # 中文里「持平」比「0.0%」更像话，尤其宏观那行经常出现。
    if abs(v) < 0.05:
        return "持平"
    return f"{v:+.1f}%"


def _line(items, fmt="{name} {pct}", sep=" ｜ "):
    return sep.join(fmt.format(name=i["name"], pct=format_pct(i["pct"])) for i in items)


# 异动归因用：这些名字都属于芯片/半导体链
SEMI_POOL = {"超威半导体", "博通", "台积电", "美光", "高通", "英特尔",
             "ARM", "英伟达", "费城半导体"}


def to_cn_clock(ts, stage):
    """把行情时间统一成北京时间。

    盘前字段 f[24] 是美东时间（"Nov 06 09:17AM EST"），盘中字段 f[3]
    已经是中国时间。两种格式在正文里混着出现，读者会以为数据在不同
    时区之间跳。统一换算成北京，并让延迟一眼可见（数据时间 vs 当前）。
    """
    ts = (ts or "").strip()
    if stage != "pre":
        return ts[11:16] if len(ts) >= 16 else ts
    m = re.match(r"([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{1,2}):(\d{2})(AM|PM)", ts)
    if not m:
        return ""
    mon, day, hh, mm, ap = m.groups()
    hour = int(hh) % 12 + (12 if ap == "PM" else 0)
    try:
        et = datetime.strptime(f"{datetime.now(ET).year} {mon} {day} {hour:02d}:{mm}",
                               "%Y %b %d %H:%M").replace(tzinfo=ET)
    except ValueError:
        return ""
    return et.astimezone(CN).strftime("%H:%M")


def mover_note(movers):
    """5 个异动全是芯片股时，正文必须点出来。

    逐个列名字读者要自己归纳，等于把最值钱的那句判断留给用户去做。
    """
    if len(movers) < 3:
        return ""
    semi = sum(1 for m in movers if m["name"] in SEMI_POOL)
    if semi == len(movers):
        return "，全部集中在芯片链"
    if semi >= len(movers) * 0.6:
        return "，以芯片链为主"
    return ""


# --------------------------------------------------------------- 渲染
def pick_earnings(rows, limit=3):
    """挑真正值得在盘前/盘中提一句的财报。

    Nasdaq 的日历按市值排序，头几个常是伯克希尔这种「市值极大但几乎不
    影响当日交易」的公司，还会把 BRK.A 和 BRK.B 当成两家公司各列一次。
    而且没有 EPS 预期的行（覆盖不足）对本简报毫无信息量。

    所以：丢掉没有预期值的 -> 按公司名去重 -> 认识的（有中文名）优先。
    """
    out, seen = [], set()
    for e in sorted(rows, key=lambda x: 0 if x.get("name_cn") else 1):
        if not (e.get("eps") or "").strip():
            continue
        key = (e.get("name") or e.get("symbol") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(e)
        if len(out) >= limit:
            break
    return out


# 盘前 09:00 看财报，盘前档的其实已经公布完了，结果已经反映在盘前价里；
# 真正「今晚才知道」的是盘后档。两者的时间含义必须分开写。
EARN_WHEN = {"盘后": "今晚盘后", "盘前": "盘前已公布"}


def pick_econ(rows, et_now, limit=3):
    """只留「还没发生」的经济数据。

    盘前 09:00 推送时，美东 08:30 发布的初请失业金已经出来了，它的影响
    已经写在盘前报价里。把一条已经发生、却只给「预期」不给实际值的数据
    列进「今日关注」，是在制造噪音。
    """
    out, seen = [], set()
    for e in rows:
        t = (e.get("time") or "").strip()
        if not t:
            continue
        try:
            h, m = (int(x) for x in t.split(":")[:2])
        except ValueError:
            continue
        when = et_now.replace(hour=h, minute=m, second=0, microsecond=0)
        if when <= et_now:          # 已经公布，不再列为「今日关注」
            continue
        name = e.get("name_cn") or e.get("name") or ""
        if not name or name in seen:
            continue
        seen.add(name)
        e = dict(e)
        e["time_cn"] = when.astimezone(CN).strftime("%H:%M")
        out.append(e)
        if len(out) >= limit:
            break
    return out


NEWS_LIMIT = 3         # 最多几条
NEWS_MAXLEN = 46       # 单条标题汉字上限，超了截断加省略号

# 二次过滤：pick_foreign_news 只保证「落在市场语汇里」，挡不住两类东西——
# 党派口水（「民主党应该如何……」）和评论专栏（opinion / commentary）。
# 这两类标题里有 tariff、Fed 这类词，能骗过关键词打分，但对判断当天行情没用。
NEWS_NOISE = re.compile(
    r"\b(opinion|commentary|column|editorial|podcast|newsletter|"
    r"crossword|quiz|horoscope|"
    r"democrats?|republicans?|senate|congress|election|midterms?|"
    r"campaign|impeach|inaugurat|gop)\b", re.I)


def fetch_news(stage, focus_sectors, now_cn):
    """重点海外新闻：盘前看隔夜，盘中看开盘后新出的。

    三个硬约束：
    1. 宁缺毋滥——没有合格条目这一栏整个不出现，不用「暂无要闻」占位。
    2. 只保留落在市场/产业语汇里的（pick_foreign_news 已经过 market_related 过滤），
       政治口水、明星八卦、纯观点专栏一律不进。
    3. 只给「发生了什么」，不写「所以涨跌」——标题本身是事实，因果是读者的判断，
       简报不替读者下这个结论。
    """
    if stage == "pre":
        windows = [12]          # 盘前：过去 12 小时，覆盖隔夜到盘前
    else:
        windows = [4, 12]       # 盘中：先看开盘至今，不够 2 条再放宽到 12 小时

    for hours in windows:
        start = now_cn - timedelta(hours=hours)
        try:
            items = fetch_foreign_news(start, now_cn)
        except Exception as e:                          # noqa: BLE001
            print(f"[WARN] 海外新闻抓取失败：{e}", file=sys.stderr)
            return []
        picked = [p for p in pick_foreign_news(items, limit=NEWS_LIMIT + 3,
                                               focus_sectors=focus_sectors)
                  if p.get("title") and not NEWS_NOISE.search(p["title"])]
        # 盘中窗口太窄常常筛不出东西，放宽一次再决定要不要显示
        if len(picked) < 2 and hours != windows[-1]:
            continue

        out = []
        for p in picked[:NEWS_LIMIT]:
            zh = (translate(p["title"]) or "").strip()
            if not zh:
                continue
            zh = zh.rstrip(".。 ")
            if len(zh) > NEWS_MAXLEN:
                zh = zh[:NEWS_MAXLEN].rstrip() + "…"
            out.append({"cn": zh, "source": p.get("source", ""),
                        "time": p.get("time", ""), "url": p.get("url", "")})
        if out:
            print(f"[INFO] 海外要闻 {len(out)} 条（窗口 {hours} 小时）")
        return out
    return []


def render(stage, d):
    """生成推送正文。纯文本，手机上一屏可读。"""
    idx, sectors, mag7, macro = d["indices"], d["sectors"], d["mag7"], d["macro"]
    judge = d["judge"]
    lines = []

    # 第一行：时间与场景。读者要先知道「这是几点、什么阶段的数据」
    if stage == "pre":
        lines.append(f"距开盘 {d['minutes_to_open']} 分钟 · 美东 {d['et_clock']} · 北京 {d['cn_clock']}")
    elif d["minutes_open"] <= 0:
        lines.append(f"刚开盘 · 美东 {d['et_clock']} · 北京 {d['cn_clock']}")
    else:
        lines.append(f"开盘 {d['minutes_open']} 分钟 · 美东 {d['et_clock']} · 北京 {d['cn_clock']}")

    # 指数：必须先声明是 ETF 代理，否则读者会把 ETF 涨幅当成指数涨幅
    lines.append("")
    lines.append("【指数 · ETF 代理】")
    lines.append(_line(list(idx.values())))

    # 定调：两句判断，不复述数字
    lines.append("")
    tone = judge["breadth"] + ("，" + judge["struct"] if judge.get("struct") else "")
    lines.append(f"【定调】{tone}。")

    # 盘前→盘中的变化，只有盘中简报才有
    if stage == "mid" and d.get("diff"):
        lines.append("")
        lines.append(f"【开盘后变化】{d['diff']}")

    # 板块两极：第二档幅度太小就不列，避免把「工业 +0.1%」说成强势
    if sectors:
        ranked = sorted(sectors, key=lambda x: -x["pct"])
        up = [i for i in ranked[:2] if i["pct"] > 0.15]
        dn = [i for i in sorted(sectors, key=lambda x: x["pct"])[:2] if i["pct"] < -0.15]
        up_s = "、".join("{} {}".format(i["name"], format_pct(i["pct"])) for i in up) or "无明显强势"
        dn_s = "、".join("{} {}".format(i["name"], format_pct(i["pct"])) for i in dn) or "无明显弱势"
        lines.append("")
        lines.append("【板块】强 " + up_s + " ｜ 弱 " + dn_s)

    if mag7:
        lines.append("")
        lines.append("【七巨头】" + _line(mag7, "{name} {pct}", sep="、"))

    if d.get("movers"):
        lines.append("")
        lines.append("【异动】" + _line(d["movers"], "{name} {pct}", sep="、")
                     + mover_note(d["movers"]))

    if macro:
        lines.append("")
        lines.append(f"【宏观】{_line(macro, '{name} {pct}')}")

    # 海外要闻：只陈述发生了什么，不给「所以涨/跌」的因果。
    # 标题里已经带了时间与来源，读者自己能判断这条信息的新鲜度和分量。
    if d.get("news"):
        lines.append("")
        lines.append("【海外要闻 · 北京时间】")
        for n in d["news"]:
            tail = "，".join(x for x in (n.get("source"), n.get("time")) if x)
            lines.append(f"· {n['cn']}" + (f"（{tail}）" if tail else ""))

    # 今日关注：只留「还没发生」的事。
    # 盘前档财报在 09:00 前已陆续公布，结果已经体现在上面那行盘前价里，
    # 再列出来是噪音；真正「今晚才知道」的是盘后档。
    watch = []
    for e in (d.get("earnings") or [])[:4]:
        when = (e.get("when") or "").strip()
        if when == "盘前":
            continue
        label = EARN_WHEN.get(when, "今日")
        seg = f"{label} {e.get('name_cn') or e.get('symbol')}"
        if e.get("eps"):
            seg += f" EPS 预期 {e['eps']}"
        watch.append(seg)
        if len(watch) >= 3:
            break
    for ev in (d.get("econ") or [])[:2]:
        seg = f"{ev.get('time_cn', '')} {ev.get('name_cn') or ev.get('name')}".strip()
        if ev.get("consensus"):
            seg += f" 预期 {ev['consensus']}"
        elif ev.get("note"):
            seg += f"（{ev['note']}）"
        watch.append(seg)
    if watch:
        lines.append("")
        lines.append("【今日关注】" + "；".join(watch))

    lines.append("")
    lines.append(f"指数为跟踪 ETF 报价 · 数据时间 北京 {d['data_cn']} · 仅供参考，不构成投资建议")
    return "\n".join(lines)


def build_diff(snapshot, idx, minutes_open=60):
    """盘中简报的增量信息：盘前的方向，开盘一小时后还在不在。

    这是盘中简报唯一无法被收盘简报替代的价值——判断「盘前情绪是被
    证实还是被打脸」。只看标普代表的大盘方向，不逐个复述绝对值。
    """
    if not snapshot:
        return ""
    prev = snapshot.get("indices") or {}
    spx_pre = (prev.get("标普500") or {}).get("pct")
    if spx_pre is None:
        return ""
    spx_now = idx.get("标普500", {}).get("pct", 0.0)
    delta = spx_now - spx_pre

    if spx_pre * spx_now < 0 and abs(delta) >= 0.5:
        return ("标普由涨转跌，盘前情绪被打反。" if spx_pre > 0
                else "标普由跌转涨，盘前悲观被打反。")
    if abs(spx_now) > abs(spx_pre) + 0.5:
        return "标普方向未变但幅度扩大，盘前情绪被开盘放大。"
    if abs(spx_now) < abs(spx_pre) - 0.5:
        return "标普方向未变但幅度收窄，盘前情绪正在被消化。"
    if minutes_open >= 5:
        return f"盘前方向基本延续，开盘 {minutes_open} 分钟仍未反转。"
    return "盘前方向基本延续，开盘至今没有反转。"


# --------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["pre", "mid"], required=True)
    ap.add_argument("--no-calendar", action="store_true",
                    help="跳过财报/经济数据抓取（本地调试用，省时间）")
    ap.add_argument("--no-news", action="store_true",
                    help="跳过海外新闻抓取（本地调试用，省翻译耗时）")
    args = ap.parse_args()
    stage = args.stage

    now_et = datetime.now(ET)
    now_cn = datetime.now(CN)
    print(f"[INFO] 阶段={stage} 美东 {now_et:%Y-%m-%d %H:%M:%S} 北京 {now_cn:%H:%M}")

    codes = ([c for c, _, _ in INDEX_PROXY] + [c for c, _ in SECTOR]
             + [c for c, _ in MAG7] + [c for c, _ in MACRO]
             + [c for c, _ in WATCH])
    raw = _get(codes)
    if not raw:
        print("[ERR] 行情接口全部无响应，按失败处理（不把故障伪装成休市）。",
              file=sys.stderr)
        return 1

    def q(code):
        return quote(raw.get(code), stage)

    indices = {}
    for code, name, etf in INDEX_PROXY:
        v = q(code)
        if v:
            v["name"] = name
            v["etf"] = etf
            indices[name] = v
    sectors = []
    for code, name in SECTOR:
        v = q(code)
        if v:
            v["name"] = name
            sectors.append(v)
    mag7 = []
    for code, name in MAG7:
        v = q(code)
        if v:
            v["name"] = name
            mag7.append(v)
    macro = []
    for code, name in MACRO:
        v = q(code)
        if v:
            v["name"] = name
            macro.append(v)

    if len(indices) < 3:
        print(f"[ERR] 指数代理只取到 {len(indices)} 个，数据源异常。", file=sys.stderr)
        return 1

    quotes = list(indices.values()) + sectors + mag7 + macro
    hit, total = session_date_hits(quotes, stage)
    if hit == 0:
        print(f"[SKIP] {total} 个标的的行情时间都不是美东今天（{now_et:%b %d}），"
              f"判定为休市或数据源滞后，本次不发。")
        _write({"stage": stage, "skipped": True,
                "reason": "行情时间不匹配美东今日，疑似休市"})
        return 0

    # 数据有效性闸门：没有成交的时段（开盘前、数据源未刷新）会返回
    # 全 0 的涨跌幅。这时候如果照发，正文就是一堆 0.0% 加一句「普跌」——
    # 比不发糟得多，因为读的人会以为今天真的全线横盘。
    nonzero = sum(1 for q in quotes if abs(q["pct"]) >= 0.01)
    if nonzero < len(quotes) * 0.3:
        print(f"[ERR] 只有 {nonzero}/{len(quotes)} 个标的有涨跌幅，行情尚未刷新。"
              f"判定为数据未就绪，本次按失败处理（不发半成品）。", file=sys.stderr)
        _write({"stage": stage, "skipped": True,
                "reason": f"行情未刷新（{nonzero}/{len(quotes)} 有报价）"})
        return 1

    # 异动：观察池里涨跌幅超阈值的，按绝对值排序取前几个
    movers = []
    for code, name in WATCH:
        v = q(code)
        if v and abs(v["pct"]) >= MOVE_THRESHOLD:
            v["name"] = name
            movers.append(v)
    movers.sort(key=lambda x: -abs(x["pct"]))
    movers = movers[:MOVE_LIMIT]

    judge = describe(indices, sectors, mag7, macro)

    # 行情时间取最新一个，让读者知道数据新鲜度
    ts_list = [v["ts"] for v in quotes if v.get("ts")]
    quote_ts = max(ts_list) if ts_list else ""

    # 日历：盘前看当日，盘中看当日（当日财报多在盘后公布）
    earnings, econ = [], []
    if not args.no_calendar:
        ds = now_et.strftime("%Y-%m-%d")
        try:
            earnings = pick_earnings(fetch_earnings(ds, limit=12), limit=3)
        except Exception as e:                          # noqa: BLE001
            print(f"[WARN] 财报日历失败：{e}", file=sys.stderr)
        try:
            econ = pick_econ(fetch_econ_events(ds, limit=8), now_et, limit=2)
        except Exception as e:                          # noqa: BLE001
            print(f"[WARN] 经济数据失败：{e}", file=sys.stderr)

    # 海外要闻：把当日领涨方向传进去，让与主线相关的新闻优先排前面
    news = []
    if not args.no_news:
        focus = [s["name"] for s in sorted(sectors, key=lambda x: -x["pct"])[:2]
                 if s["pct"] > 0.15]
        try:
            news = fetch_news(stage, focus, now_cn)
        except Exception as e:                          # noqa: BLE001
            print(f"[WARN] 海外新闻失败：{e}", file=sys.stderr)

    # 盘前 → 盘中的对比
    diff = ""
    snap_path = os.path.join(SNAP_DIR, f"{now_et:%Y-%m-%d}.json")
    if stage == "mid":
        try:
            with open(snap_path, encoding="utf-8") as f:
                diff = build_diff(json.load(f), indices,
                                  max(0, now_et.hour * 60 + now_et.minute - 570))
        except Exception:                               # noqa: BLE001
            diff = ""
    else:
        # 盘前跑完把快照落盘，供两个小时后对比
        os.makedirs(SNAP_DIR, exist_ok=True)
        with open(snap_path, "w", encoding="utf-8") as f:
            json.dump({"stage": "pre", "ts": now_et.isoformat(),
                       "indices": {k: {"pct": v["pct"], "price": v["price"]}
                                   for k, v in indices.items()},
                       "sectors": {s["name"]: s["pct"] for s in sectors}},
                      f, ensure_ascii=False, indent=2)

    open_min = now_et.hour * 60 + now_et.minute - (9 * 60 + 30)
    d = {
        "stage": stage,
        "date": now_et.strftime("%Y-%m-%d"),
        "weekday": WEEKDAY_CN[now_et.weekday()],
        "generated_at": now_cn.strftime("%Y-%m-%d %H:%M"),
        "et_clock": now_et.strftime("%H:%M"),
        "cn_clock": now_cn.strftime("%H:%M"),
        "minutes_to_open": max(0, -open_min) if open_min < 0 else 0,
        "minutes_open": max(0, open_min),
        "quote_ts": quote_ts,
        "data_cn": to_cn_clock(quote_ts, stage),
        "indices": {k: {"name": k, "pct": v["pct"], "price": v["price"]}
                    for k, v in indices.items()},
        "sectors": sectors,
        "mag7": mag7,
        "macro": macro,
        "movers": movers,
        "judge": judge,
        "earnings": earnings,
        "econ": econ,
        "news": news,
        "diff": diff,
        "date_hits": f"{hit}/{total}",
    }
    d["text"] = render(stage, d)
    _write(d)

    print("\n" + "=" * 60)
    print(d["text"])
    print("=" * 60)
    return 0


def _write(d):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    print(f"[OK] 已写入 {OUT_PATH}")


if __name__ == "__main__":
    sys.exit(main())
