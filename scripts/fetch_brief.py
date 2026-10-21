#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股收盘简报 · 数据抓取
纯标准库实现（无第三方依赖）。

与 A 股版的差异（同为「收盘简报」，但市场结构不同）：
  A 股看「指数分化 + 量能 + 题材轮动」；美股看「指数分化 + 广度 + 权重股」。
  美股没有「涨跌家数」公开源，改用 RSP（标普等权）与 SPY（市值加权）的差值度量广度；
  美股没有实时 VIX 源，改用标普 500 日内振幅度量波动。

数据源分工：
  指数/板块/个股  腾讯 qt.gtimg.cn（单一请求批量取，稳定）
  板块强弱        11 个 GICS 行业 SPDR ETF（XL* 系列）
  权重股          MAG7
  宏观锚          SPY/RSP（广度）、TLT（长端美债）、UUP（美元）、GLD（黄金）
  收盘叙事        东财资讯搜索「美股收盘 / 美股复盘」（相当于 A 股的「收评」）
  消息要闻        东财资讯 + 新浪 7×24 快讯
  明日展望        东财研报库 qType=2 策略研报（筛海外/美股/全球）

输出：data/brief.json、data/history.json、data/summary.md
"""

import gzip
import html as html_mod
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ETree

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                                    # 极老版本兜底
    ET = timezone(timedelta(hours=-4))

CN_TZ = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

THROTTLE = 0.5
_last = [0.0]


def _fetch(url, referer=None, retries=3, delay=2, encoding="utf-8", timeout=20):
    last_err = None
    for i in range(retries):
        try:
            gap = time.time() - _last[0]
            if gap < THROTTLE:
                time.sleep(THROTTLE - gap)
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Referer": referer or "https://gu.qq.com/",
                "Accept": "*/*",
                "Connection": "close",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            _last[0] = time.time()
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                return raw.decode("gbk", errors="ignore")
        except Exception as e:
            last_err = e
            _last[0] = time.time()
            if i < retries - 1:
                time.sleep(delay * (i + 1))
    print(f"[WARN] 失败 {url[:70]} -> {last_err}", file=sys.stderr)
    return None


def get_json(url, referer=None, retries=3, delay=2, timeout=20):
    t = _fetch(url, referer=referer, retries=retries, delay=delay, timeout=timeout)
    if not t:
        return None
    try:
        return json.loads(t)
    except Exception:
        return None


def fnum(v, default=0.0):
    try:
        if v in ("-", "", None):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


# ============================================================ 标的清单
# 腾讯美股代码：指数 usDJI/usIXIC/usINX/usNDX；ETF 与个股直接用 ticker
US_INDEX = [
    ("usDJI", "道琼斯", None),
    ("usIXIC", "纳斯达克", None),
    ("usINX", "标普500", None),
    ("usIWM", "罗素2000", "IWM"),
    ("usSMH", "费城半导体", "SMH"),
]

SECTOR_ETF = [
    ("usXLK", "科技"),
    ("usXLC", "通信服务"),
    ("usXLY", "可选消费"),
    ("usXLP", "必需消费"),
    ("usXLF", "金融"),
    ("usXLV", "医疗保健"),
    ("usXLI", "工业"),
    ("usXLB", "材料"),
    ("usXLE", "能源"),
    ("usXLU", "公用事业"),
    ("usXLRE", "房地产"),
]

MAG7 = [
    ("usNVDA", "英伟达"),
    ("usAAPL", "苹果"),
    ("usMSFT", "微软"),
    ("usGOOGL", "谷歌"),
    ("usAMZN", "亚马逊"),
    ("usMETA", "Meta"),
    ("usTSLA", "特斯拉"),
]

MACRO = [
    ("usSPY", "标普500ETF"),
    ("usRSP", "标普等权"),
    ("usTLT", "20年美债"),
    ("usUUP", "美元"),
    ("usGLD", "黄金"),
]


def parse_tencent(text):
    out = {}
    for line in (text or "").split(";"):
        line = line.strip()
        if not line.startswith("v_") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        code = key[2:].strip()
        val = val.strip().strip('"')
        out[code] = val.split("~")
    return out


def pack_quote(f):
    """腾讯美股字段：3=最新/收盘 4=昨收 5=今开 30=时间 31=涨跌额 32=涨跌幅 33=高 34=低 37=成交额"""
    if not f or len(f) < 38:
        return None
    return {
        "close": fnum(f[3]),
        "prev": fnum(f[4]),
        "open": fnum(f[5]),
        "time": (f[30] or "").strip(),
        "chg": fnum(f[31]),
        "pct": fnum(f[32]),
        "high": fnum(f[33]),
        "low": fnum(f[34]),
        "volume": fnum(f[36]),
        "amount": fnum(f[37]),
    }


def fetch_all_quotes():
    """一次请求取全部标的，减少被限流的概率。"""
    codes = ([c for c, _, _ in US_INDEX] + [c for c, _ in SECTOR_ETF]
             + [c for c, _ in MAG7] + [c for c, _ in MACRO])
    txt = _fetch("https://qt.gtimg.cn/q=" + ",".join(codes), encoding="gbk")
    parsed = parse_tencent(txt)
    if not parsed:
        return {}, ""

    indices, sectors, mag7, macro = {}, [], [], {}
    for code, name, proxy in US_INDEX:
        q = pack_quote(parsed.get(code))
        if q and q["close"] > 0:
            q["name"] = name
            q["proxy"] = proxy or ""
            indices[name] = q
    for code, name in SECTOR_ETF:
        q = pack_quote(parsed.get(code))
        if q and q["close"] > 0:
            sectors.append({"name": name, "code": code[2:], "pct": q["pct"],
                            "close": q["close"], "chg": q["chg"]})
    for code, name in MAG7:
        q = pack_quote(parsed.get(code))
        if q and q["close"] > 0:
            mag7.append({"name": name, "code": code[2:], "pct": q["pct"],
                         "close": q["close"], "chg": q["chg"],
                         "amount": q["amount"]})
    for code, name in MACRO:
        q = pack_quote(parsed.get(code))
        if q and q["close"] > 0:
            macro[name] = {"name": name, "pct": q["pct"], "close": q["close"]}

    trade_time = ""
    for q in indices.values():
        if q["time"]:
            trade_time = max(trade_time, q["time"])
    return {"indices": indices, "sectors": sectors, "mag7": mag7,
            "macro": macro}, trade_time


# ============================================================ 交易日判定
class FatalFetchError(RuntimeError):
    """数据源异常导致无法生成简报——这不是休市，必须让工作流失败。

    以前这种情况会被判成「休市」，工作流随之跳过部署和推送、并以
    success 收尾：接口挂了，但你和 GitHub 都以为今天本来就该没简报。
    这是最坏的一种失败——静默且看起来一切正常。

    抛错之后：run 变红 → 触发 alert.py 告警 → 下一个冗余触发点自动重试。
    """


def expected_close_date(et_now):
    """当前应收盘的美东日期：16:00 前算上一日；周末回退到周五。"""
    d = et_now.date()
    if et_now.hour < 16:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


# 美股连续休市最多一天（独立日/圣诞/感恩节都是单日），所以「最近收盘日」
# 只可能等于「应收盘日的上一个工作日」。差得更多就不是休市了。
def prev_business_day(d):
    """上一个工作日（跳过周末）。用来精确区分休市与数据源滞后。"""
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def judge_trading_day(trade_time, et_now):
    """用「行情时间的美东日期」对齐「应收盘日」判断是否休市。

    美股假日无公开的稳定中文接口，用对齐法可自动覆盖：
    若行情日期正好停在应收盘日的前一个工作日，说明应收盘日当天休市。

    但「行情日期早于应收盘日」有两种完全不同的成因，必须分开处理：
      1. 应收盘日休市，最近收盘停在上一工作日 -> 正常，跳过推送
      2. 行情接口挂了，返回空值或几天前的陈数据 -> 异常，必须抛错告警

    区分依据不是「差几天」，而是「是否正好差一个工作日」：
    周一休市时应收盘日周一、最近收盘是上周五，差 3 个自然日但仍只是
    一个工作日；而接口若返回三天前的数据，那就会跨过不止一个工作日。
    用工作日数判断，比用自然日数阈值准确得多。
    """
    if not trade_time:
        raise FatalFetchError(
            "未取到行情时间：行情接口全部无响应，无法判断是休市还是接口故障。"
            "为避免把接口故障伪装成休市，本次按失败处理。")
    try:
        q_date = datetime.strptime(trade_time[:10], "%Y-%m-%d").date()
    except ValueError:
        raise FatalFetchError(f"行情时间格式异常（{trade_time}），数据源返回了无法解析的内容。")

    exp = expected_close_date(et_now)

    if q_date == exp:
        return True, q_date.isoformat(), ""
    if q_date > exp:
        # cron 延迟到美东 16:00 之后才跑，应收盘日已经滚到当天
        return True, q_date.isoformat(), ""

    prev_bd = prev_business_day(exp)
    if q_date == prev_bd:
        return False, q_date.isoformat(), f"美东 {exp} 休市（最近收盘为 {q_date}）"

    raise FatalFetchError(
        f"行情数据异常：应收盘日 {exp}（上一工作日 {prev_bd}），"
        f"但接口只返回 {q_date} 的数据。不是休市——休市时最近收盘应正好是 "
        f"{prev_bd}。按数据源故障处理。")


# ============================================================ 成交额历史
def load_history():
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_history(hist):
    os.makedirs(DATA_DIR, exist_ok=True)
    keys = sorted(hist.keys())[-30:]
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump({k: hist[k] for k in keys}, f, ensure_ascii=False, indent=2)


def resolve_prev_amount(today_str, hist):
    prev_dates = [d for d in sorted(hist.keys()) if d < today_str]
    if prev_dates:
        last = prev_dates[-1]
        v = fnum(hist.get(last, 0))
        if v > 0:
            print(f"[INFO] 上一交易日成交额取历史 {last}: {v/1e9:.1f} 亿美元")
            return v, last
    return 0.0, ""


# ============================================================ 资讯：外媒 RSS
# 国内财经媒体对美股的报道存在转述损耗与标题党，改为直接抓一手外媒。
# GitHub Actions 跑在美国机房，这些源天然可达；本机若走代理可能仅部分可用，
# 因此保留下面的国内源作兜底（外媒全挂时不至于开天窗）。
FOREIGN_FEEDS = [
    # (来源, 频道, RSS, 权重) —— 权重体现一手性：WSJ/美联储高于门户聚合
    ("WSJ", "市场", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml", 1.15),
    ("WSJ", "美国商业", "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml", 0.95),
    ("CNBC", "市场", "https://www.cnbc.com/id/20910258/device/rss/rss.html", 1.0),
    ("CNBC", "科技", "https://www.cnbc.com/id/19854910/device/rss/rss.html", 0.9),
    ("CNBC", "经济", "https://www.cnbc.com/id/10000664/device/rss/rss.html", 0.9),
    ("CNBC", "头条", "https://www.cnbc.com/id/100003114/device/rss/rss.html", 0.8),
    ("MarketWatch", "头条", "https://feeds.content.dowjones.io/public/rss/mw_topstories", 0.85),
    ("MarketWatch", "盘中", "https://feeds.content.dowjones.io/public/rss/mw_marketpulse", 0.85),
    ("美联储", "新闻稿", "https://www.federalreserve.gov/feeds/press_all.xml", 1.1),
]


def _fetch_bytes(url, timeout=15, retries=3):
    """RSS 多为 gzip 压缩，需要字节级抓取后手动解压。

    本机走代理时 SSL 偶发 UNEXPECTED_EOF，云端（美国机房）直连更稳，
    但仍保留重试，避免单个源失败拖垮整条链路。
    """
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/rss+xml,application/xml,text/xml,*/*",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "close",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except Exception as e:
            last = e
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
    raise last


def strip_html(s):
    s = html_mod.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def fetch_foreign_news(start_dt, end_dt):
    """抓外媒 RSS，统一成 {title, summary, url, dt, time, source, weight}。"""
    out, seen = [], set()
    start_cn = start_dt.astimezone(CN_TZ)
    end_cn = end_dt.astimezone(CN_TZ)
    for source, section, url, weight in FOREIGN_FEEDS:
        try:
            root = ETree.fromstring(_fetch_bytes(url))
        except Exception as e:
            print(f"[WARN] {source}/{section} 抓取失败 {type(e).__name__}", file=sys.stderr)
            continue
        n_ok = 0
        for it in root.findall(".//item"):
            title = strip_html(it.findtext("title", ""))
            desc = strip_html(it.findtext("description", ""))
            link = (it.findtext("link", "") or "").strip()
            pub = (it.findtext("pubDate", "") or "").strip()
            if not title or not link:
                continue
            try:
                dt = parsedate_to_datetime(pub)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            dt = dt.astimezone(CN_TZ)
            if not (start_cn <= dt <= end_cn):
                continue
            key = title[:50].lower()
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "title": title, "summary": desc, "url": link, "dt": dt,
                "time": dt.strftime("%H:%M"), "source": source,
                "section": section, "weight": weight, "kind": "foreign",
            })
            n_ok += 1
        print(f"[INFO] {source}/{section}: 窗口内 {n_ok} 条")
        time.sleep(0.3)
    out.sort(key=lambda x: x["dt"], reverse=True)
    print(f"[INFO] 外媒合计 {len(out)} 条")
    return out


# ------------------------------------------------------------ 翻译
_trans_cache = {}
_google_ok = [True]      # 熔断开关：Google 一旦不通就整轮不再重试，避免每条都卡 12 秒


def translate(text):
    """英译中。优先 Google 免费端点（Actions 在美国机房通常直连可用），
    失败降级 MyMemory，再失败保留英文——宁可给英文原文，也不编造中文。

    Google 在国内走代理会握手超时，因此带熔断：失败一次即禁用，后续直接走 MyMemory，
    否则每条翻译都等 12 秒，整个 job 会被拖到超时。
    """
    if not text:
        return text
    if not re.search(r"[A-Za-z]{4,}", text):
        return text                     # 纯数字/符号不必翻
    if text in _trans_cache:
        return _trans_cache[text]

    q = text[:480]

    if _google_ok[0]:
        try:
            u = ("https://translate.googleapis.com/translate_a/single"
                 "?client=gtx&sl=en&tl=zh-CN&dt=t&q=" + urllib.parse.quote(q))
            t = _fetch(u, referer="https://translate.google.com/", retries=1, timeout=8)
            if t:
                j = json.loads(t)
                zh = "".join(seg[0] for seg in j[0] if seg and seg[0])
                if zh.strip():
                    _trans_cache[text] = zh.strip()
                    return zh.strip()
            _google_ok[0] = False
        except Exception:
            _google_ok[0] = False
            print("[INFO] Google 翻译不可达，本轮改用 MyMemory", file=sys.stderr)

    # MyMemory 兜底
    try:
        u = ("https://api.mymemory.translated.net/get?langpair=en|zh-CN&q="
             + urllib.parse.quote(q))
        t = _fetch(u, retries=2, timeout=12)
        if t:
            j = json.loads(t)
            zh = ((j.get("responseData") or {}).get("translatedText") or "").strip()
            if zh and "MYMEMORY WARNING" not in zh.upper():
                _trans_cache[text] = zh
                return zh
    except Exception:
        pass

    _trans_cache[text] = text
    return text


# ============================================================ 资讯：东财搜索
NEWS_QUERIES = ["美股收盘", "美股复盘", "美股三大指数"]


def fetch_em_news(start_dt, end_dt=None, keywords=None):
    """东财资讯搜索，按相关性取稿后按发布时间过滤。

    注意：接口的 sort:"time" 参数不生效（返回不相关结果），只能用默认相关性排序，
    再在本地按时间筛，因此需要多关键词并行以扩大覆盖面。

    end_dt 必须封口：盘后十余小时才发布的稿件解释不了昨夜走势，
    不封口会让归因把「今天下午的新闻」错配给「昨晚的行情」。
    """
    out, seen = [], set()
    for kw in (keywords or NEWS_QUERIES):
        param = json.dumps({
            "uid": "", "keyword": kw, "type": ["cmsArticleWebOld"],
            "client": "web", "clientType": "web", "clientVersion": "curr",
            "param": {"cmsArticleWebOld": {
                "searchScope": "default", "sort": "default",
                "pageIndex": 1, "pageSize": 20, "preTag": "", "postTag": ""}},
        }, ensure_ascii=False)
        url = ("https://search-api-web.eastmoney.com/search/jsonp?cb=cb&param="
               + urllib.parse.quote(param))
        t = _fetch(url, referer="https://so.eastmoney.com/", retries=2, timeout=15)
        if not t:
            continue
        try:
            j = json.loads(t[t.index("(") + 1:t.rindex(")")])
        except Exception:
            continue
        rows = (j.get("result") or {}).get("cmsArticleWebOld") or []
        for a in rows:
            date = (a.get("date") or "").strip()
            title = re.sub(r"<[^>]+>", "", a.get("title") or "").strip()
            content = re.sub(r"<[^>]+>", "", a.get("content") or "").strip()
            url_a = a.get("url") or ""
            if not title or not date or not url_a:
                continue
            try:
                dt = datetime.strptime(date, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN_TZ)
            except ValueError:
                continue
            if dt < start_dt:
                continue
            if end_dt and dt > end_dt:
                continue
            key = title[:24]
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "time": date[11:16], "stamp": date, "dt": dt,
                "title": title, "body": content, "url": url_a,
                "media": a.get("mediaName") or "",
                "tags": [],
            })
        time.sleep(0.4)
    out.sort(key=lambda x: x["dt"], reverse=True)
    print(f"[INFO] 东财资讯 {len(out)} 条（{start_dt:%m-%d %H:%M} 之后）")
    return out


# ============================================================ 资讯：新浪 7×24
def crawl_sina_feed(start_dt, end_dt=None, max_pages=14):
    """回溯抓取新浪 7×24 全量快讯，直到早于 start_dt 为止。"""
    out, seen = [], set()
    start_cn = start_dt.astimezone(CN_TZ)
    end_cn = end_dt.astimezone(CN_TZ) if end_dt else None
    for page in range(1, max_pages + 1):
        url = ("https://zhibo.sina.com.cn/api/zhibo/feed"
               "?page=%d&page_size=100&zhibo_id=152&tag_id=0&dire=f&dpc=1" % page)
        d = get_json(url, referer="https://finance.sina.com.cn/7x24/", retries=2, delay=1)
        lst = (((d or {}).get("result") or {}).get("data") or {}).get("feed", {}).get("list") or []
        if not lst:
            break
        oldest = ""
        for it in lst:
            ct = it.get("create_time") or ""
            if not oldest or ct < oldest:
                oldest = ct
            try:
                dt = datetime.strptime(ct, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN_TZ)
            except ValueError:
                continue
            if dt < start_cn:
                continue
            if end_cn and dt > end_cn:
                continue
            txt = re.sub(r"<[^>]+>", "", it.get("rich_text") or it.get("text") or "").strip()
            txt = re.sub(r"\s+", " ", txt)
            if len(txt) < 10:
                continue
            key = txt[:40]
            if key in seen:
                continue
            seen.add(key)
            ext = it.get("ext") or ""
            docurl = ""
            m = re.search(r'"docurl":"(.*?)"', ext if isinstance(ext, str) else "")
            if m:
                docurl = m.group(1).replace("\\/", "/")
            out.append({
                "time": ct[11:16], "stamp": ct, "dt": dt, "text": txt,
                "url": docurl or "https://finance.sina.com.cn/7x24/",
                "tags": [t.get("name") for t in (it.get("tag") or [])],
            })
        if oldest and oldest < start_cn.strftime("%Y-%m-%d %H:%M:%S"):
            break
        time.sleep(0.4)
    print(f"[INFO] 新浪快讯 {len(out)} 条")
    return out


# ============================================================ 要闻打分
KEYWORDS = [
    ("美联储", 12), ("鲍威尔", 11), ("FOMC", 11), ("降息", 11), ("加息", 10),
    ("CPI", 10), ("PCE", 10), ("非农", 10), ("通胀", 9), ("点阵图", 9),
    ("国债", 8), ("收益率", 8), ("美债", 8), ("财政", 6), ("白宫", 7),
    ("特朗普", 8), ("关税", 9), ("出口管制", 8), ("制裁", 7), ("停摆", 8),
    ("财报", 9), ("业绩指引", 8), ("超预期", 6), ("电话会", 6),
    ("英伟达", 10), ("OpenAI", 8), ("台积电", 8), ("苹果", 8), ("特斯拉", 8),
    ("微软", 8), ("谷歌", 7), ("Meta", 7), ("亚马逊", 7), ("博通", 7),
    ("芯片", 8), ("半导体", 8), ("算力", 7), ("数据中心", 7), ("AI资本开支", 8),
    ("原油", 6), ("OPEC", 7), ("黄金", 6), ("美元", 6), ("日元", 5),
    ("纳斯达克", 6), ("标普", 6), ("道指", 5), ("纳指", 6), ("美股", 6),
    ("存储", 6), ("闪存", 6), ("HBM", 7), ("光模块", 6),
    ("并购", 6), ("回购", 5), ("IPO", 5), ("衰退", 8), ("软着陆", 7),
]
BLACKLIST = ["声明：本文", "免责声明", "广告", "点击查看", "直播中", "订阅",
             "股票交易异常波动", "无应披露未披露", "限售股", "减持计划",
             "揭秘涨停", "龙虎榜", "封单", "涨幅偏离值", "融资余额", "机构调研"]
# A 股专属语汇：东财搜索会混入 A 股稿件，命中且不含美股词则剔除
# 新浪快讯是全品类财经流，必须用「美国口径」收紧，否则「半导体三季报预增」这类
# A 股稿件会因为含通用行业词而误入美股简报。
# 注意不要把「美国」当强词——它会把「美国副总统万斯谈乌克兰」这类政治噪声全放进来。
US_MACRO_WORDS = ("美联储", "美股", "纳斯达克", "标普", "道指", "纳指", "美债",
                  "美元指数", "鲍威尔", "非农", "CPI", "PCE", "FOMC", "关税",
                  "白宫", "美国财长", "纽约联储", "美银", "高盛", "摩根士丹利")
US_STOCK_WORDS = ("英伟达", "苹果", "特斯拉", "微软", "谷歌", "Meta", "亚马逊",
                  "博通", "台积电", "OpenAI", "英特尔", "甲骨文", "奈飞", "礼来",
                  "美光", "闪迪", "SK海力士", "AMD", "高通", "波音")
# 政治口水与日常新闻，对定价没有直接信息量
NOISE_WORDS = ("万斯", "普京", "泽连斯基", "北约", "竞选", "披萨", "飓风",
               "天气", "客流量", "发文感谢", "私生活")
A_SHARE_ONLY = ["A股", "沪指", "深成指", "创业板", "科创板", "北交所", "上交所",
                "深交所", "证监会", "北向", "两市成交", "涨停", "跌停", "公募",
                "可转债", "股权转让", "业绩预告", "券商", "上市公司"]
# 开盘/盘前类快讯只描述开盘一瞬，信息密度低于收盘总结
OPEN_SUMMARY = re.compile(r"开盘丨|美股开盘|三大指数开盘|盘前丨|美股盘前")
MOVE_RE = re.compile(r"涨超|跌超|拉升|跳水|异动|封板|盘前涨|盘后涨|盘中涨|盘中跌|"
                     r"股价[^，。]{0,6}[涨跌]|收涨|收跌|高开|低开")
# 事件型表述：用来判断一条消息是否可能构成催化，纯价格描述不算
EVENT_RE = re.compile(r"发布|宣布|签署|获批|批准|启动|上调|下调|追加|投资|扩产|"
                      r"收购|并购|合作|推出|计划|目标|指引|订单|中标|裁员|停产|"
                      r"数据|通胀|利率|政策|法案|关税|制裁|首选|评级|目标价|升至|"
                      r"削峰|布局|回归|增加|减少|警告|预计")
PLUMBING = ["清算", "招标", "中标", "任职资格", "公司章程", "募集说明书",
            "公示期", "名单公示", "配额", "拟分配"]


def split_title(text):
    m = re.match(r"^【([^】]{6,90})】\s*(.*)$", text)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return text[:60].strip(), text


def clean_body(title, body):
    body = re.sub(r"^【[^】]*】", "", body or "").strip()
    body = re.sub(r"\s+", " ", body)
    n = 0
    for a, b in zip(title or "", body or ""):
        if a != b:
            break
        n += 1
    if n >= 12:
        idx = max(body[:n + 1].rfind(ch) for ch in "，,。；;：:、）) ")
        if idx >= 8:
            body = body[idx + 1:].lstrip("，,、：: ")
    body = re.sub(r"[，,、；;：:]+$", "", body).strip()
    body = re.sub(r"[。．\.]{2,}$", "。", body)          # 源文常自带重复句号
    if len(body) > 150:
        body = body[:150].rstrip("，,、；;。") + "…"
    if body and not body.endswith(("。", "…", "！", "？")):
        body += "。"
    return body


def pick_news(em_news, sina_feed, limit=3, focus_words=None):
    """政策权重 + 与当日主线相关性打分，同主题只留一条。"""
    focus_words = [w for w in (focus_words or []) if w]
    wmap = dict(KEYWORDS)
    cands = []

    for it in em_news:
        title = it["title"]
        if len(title) < 8 or any(b in title for b in BLACKLIST):
            continue
        if MOVE_RE.search(title):
            continue
        hits = [kw for kw, _ in KEYWORDS if kw in title]
        has_us = any(k in title or k in (it.get("body") or "")[:120]
                     for k in ("美股", "美国", "美联储", "纳斯达克", "标普", "道指", "美元", "美债"))
        if any(a in title for a in A_SHARE_ONLY) and not has_us:
            continue
        score = sum(wmap[k] for k in hits) + 4          # 东财稿件正文完整，基础加权
        if OPEN_SUMMARY.search(title):
            score -= 12
        if any(w in title and len(w) >= 3 for w in focus_words):
            score += 8
        score -= 6 * sum(1 for p in PLUMBING if p in title)
        if score < 10:
            continue
        topic = max(hits, key=lambda k: wmap[k]) if hits else "其他"
        cands.append({"score": score, "time": it["time"], "title": title,
                      "body": it.get("body") or "", "url": it["url"],
                      "media": it.get("media") or "", "topic": topic, "kind": "em"})

    for it in sina_feed:
        title, body = split_title(it["text"])
        if len(title) < 8 or any(b in title for b in BLACKLIST):
            continue
        if MOVE_RE.search(title):
            continue
        if not (any(k in title for k in US_MACRO_WORDS)
                or any(k in title for k in US_STOCK_WORDS)):
            continue
        if any(a in title for a in A_SHARE_ONLY):
            continue
        if any(w in title for w in NOISE_WORDS):
            continue
        hits = [kw for kw, _ in KEYWORDS if kw in title]
        score = sum(wmap[k] for k in hits) - 4          # 快讯无正文、信息密度低，降权
        if OPEN_SUMMARY.search(title):
            score -= 12                                 # 开盘一瞬的快讯，只描述开盘几分钟，信息量低于收盘总结
        if any(w in title and len(w) >= 3 for w in focus_words):
            score += 8
        score -= 6 * sum(1 for p in PLUMBING if p in title)
        if score < 10:
            continue
        topic = max(hits, key=lambda k: wmap[k]) if hits else "其他"
        cands.append({"score": score, "time": it["time"], "title": title,
                      "body": body, "url": it["url"], "media": "新浪财经",
                      "topic": topic, "kind": "sina"})

    cands.sort(key=lambda x: (-x["score"], x["time"]))
    out, seen, used = [], set(), set()
    MIN_KEEP = 10                      # 门槛：宁可只出 2 条，也不用低价值消息凑满 3 条
                                       # 东财稿有 +4 正文加权，快讯 -4，两条线在此基本对齐
    for c in cands:
        k = c["title"][:12]
        if k in seen or c["topic"] in used or c["score"] < MIN_KEEP:
            continue
        seen.add(k)
        used.add(c["topic"])
        out.append(c)
        if len(out) >= limit:
            break
    if len(out) < limit:
        for c in cands:
            if c in out or c["title"][:12] in seen or c["score"] < MIN_KEEP:
                continue
            seen.add(c["title"][:12])
            out.append(c)
            if len(out) >= limit:
                break

    for i, c in enumerate(out):
        body = clean_body(c["title"], c["body"])
        if not body or len(body) < 20:
            body = c["title"] + "。"
        c["summary"] = body
        c["no"] = i + 1
    return out


# ============================================================ 外媒要闻打分
# 英文标题相关度用英文词表，与盘面挂钩时通过 THEME_LEX_EN 做中英映射
EN_KEYWORDS = [
    ("Federal Reserve", 12), ("FOMC", 11), ("Powell", 11), ("Fed ", 10),
    ("inflation", 9), ("CPI", 10), ("jobs report", 10), ("payrolls", 10),
    ("rate cut", 11), ("rate hike", 10), ("Treasury", 8), ("yield", 7),
    ("earnings", 9), ("guidance", 8), ("quarterly", 6),
    ("Nvidia", 10), ("Apple", 8), ("Tesla", 8), ("Microsoft", 8),
    ("Amazon", 8), ("Meta", 7), ("Google", 7), ("Alphabet", 7),
    ("Broadcom", 8), ("TSMC", 8), ("OpenAI", 8), ("Oracle", 7),
    ("chip", 8), ("semiconductor", 8), ("data center", 7), ("AI ", 5),
    ("tariff", 9), ("Trump", 8), ("White House", 7), ("shutdown", 8),
    ("oil", 6), ("OPEC", 7), ("gold", 6), ("dollar", 6), ("bond", 6),
    ("S&P", 6), ("Nasdaq", 6), ("Dow", 5), ("stocks", 5),
    ("selloff", 7), ("rout", 7), ("recession", 8), ("layoff", 6),
]
# 个人理财/通讯稿类栏目，与盘面无关
EN_NOISE = ["newsletter", "i'm ", "my husband", "my wife", "should i",
            "here's how much", "i have $", "retirement savings", "529 plan",
            "investing club", "homestretch", "what to watch", "morning squawk",
            "cramer", "musk says he", "spacex share", "— plus", " - plus"]
# CNBC 部分条目的 description 是栏目固定文案，不是新闻正文
BAD_SUMMARY = ["每个工作日", "投资俱乐部", "订阅", "注册", "点击这里"]
BAD_SUMMARY_EN = ["every weekday", "investing club", "homestretch", "sign up",
                  "subscribe", "click here"]
# 市场相关性闸门：不满足就直接剔除。
# 只用关键词打分会把「OpenAI 涉嫌偷拍被拒保释」这类社会新闻选进来（它含 OpenAI 但无关市场），
# 所以先要求标题落在市场/产业语汇里，再谈权重。
# 注意 ai 必须用词边界：否则 OpenAI 里的 "AI" 会把社会新闻全放进来。
MARKET_GATE = re.compile(
    r"stock|share|market|investor|earning|trad|rally|selloff|rout|bounce|"
    r"yield|bond|\bfed\b|fomc|inflation|tariff|econom|gdp|profit|revenue|"
    r"valuation|\bipo\b|acquisition|merger|oil|gold|dollar|"
    r"tech|chip|semiconductor|data center|cloud|software|"
    r"nasdaq|s&p|dow|(?<![a-z])ai(?![a-z])", re.I)
# 公司名单独出现不代表是市场新闻（「OpenAI 窃听案嫌疑人」含 OpenAI 但与社会新闻无异），
# 必须叠加商业动作词才算。
COMPANY_RE = re.compile(
    r"nvidia|apple|tesla|microsoft|amazon\b|meta\b|google|alphabet|broadcom|"
    r"tsmc|openai|oracle|jpmorgan|goldman|berkshire|netflix|lilly|intel\b|"
    r"amd\b|boeing|disney|palantir|qualcomm", re.I)
BIZ_ACTION = re.compile(
    r"announc|launch|earning|beat|miss|guidance|acqui|merg|partner|invest|"
    r"profit|revenue|sales|forecast|raise|layoff|expand|\bbuy\b|\bdeal\b|"
    r"\bipo\b|contract|\border\b|sue|lawsuit|recall|approv", re.I)


def market_related(title):
    if MARKET_GATE.search(title):
        return True
    return bool(COMPANY_RE.search(title) and BIZ_ACTION.search(title))
THEME_LEX_EN = {
    "科技": ["tech", "AI", "software", "cloud", "data center", "apple", "microsoft"],
    "半导体": ["chip", "semiconductor", "nvidia", "tsmc", "broadcom", "memory"],
    "通信服务": ["google", "alphabet", "meta", "advertis", "streaming", "netflix"],
    "可选消费": ["tesla", "amazon", "retail", "consumer", "auto"],
    "必需消费": ["food", "beverage", "consumer staples", "procter", "coca"],
    "金融": ["bank", "goldman", "jpmorgan", "morgan stanley", "insurance", "credit"],
    "医疗保健": ["pharma", "drug", "lilly", "fda", "biotech", "health"],
    "工业": ["industrial", "boeing", "defense", "airline", "manufactur"],
    "材料": ["copper", "chemical", "steel", "material"],
    "能源": ["oil", "crude", "opec", "energy", "natural gas"],
    "公用事业": ["utility", "power grid", "nuclear", "electricity"],
    "房地产": ["real estate", "reit", "housing", "mortgage"],
}


def pick_foreign_news(items, limit=3, focus_sectors=None):
    """外媒要闻：英文词表打分 + 来源权重 + 与当日领涨方向的英文映射。"""
    focus_words = []
    for sec in (focus_sectors or []):
        for key, ws in THEME_LEX_EN.items():
            if key == sec or key in sec or sec in key:
                focus_words.extend(ws)
                break

    cands = []
    for it in items:
        title = it["title"]
        low = title.lower()
        if any(n in low for n in EN_NOISE):
            continue
        if not market_related(title):
            continue                          # 不落在市场/产业语汇里，直接跳过
        hits = [(kw, w) for kw, w in EN_KEYWORDS if kw.lower() in low]
        base = sum(w for _, w in hits)
        if base <= 0:
            continue
        score = base * it.get("weight", 1.0)
        if any(w in low for w in focus_words):
            score += 8                       # 与当日领涨方向相关
        if it["source"] == "美联储":
            score += 4                       # 一手政策源，优先级高
        topic = (max(hits, key=lambda x: x[1])[0] if hits else "其他")
        cands.append({"score": score, "item": it, "topic": topic})

    cands.sort(key=lambda x: (-x["score"], x["item"]["time"]))
    out, used = [], set()
    MIN_KEEP = 7           # 外媒条目平均分偏低（标题短、关键词命中少），门槛比国内源低
    for c in cands:
        if c["score"] < MIN_KEEP:
            continue
        k = c["topic"]
        if k in used:
            continue
        used.add(k)
        out.append(c["item"])
        if len(out) >= limit:
            break
    if len(out) < limit:
        for c in cands:
            if c["item"] in out or c["score"] < MIN_KEEP:
                continue
            out.append(c["item"])
            if len(out) >= limit:
                break
    return out


# ============================================================ 明日事件（硬数据）
# 财报日历只给 ticker 和 EPS 数字，对非交易员等于天书。
# 这里补上「中文名 + 做什么」，并在渲染层补「盘前/盘后意味着什么」。
COMPANY_INFO = {
    "AAPL": ("苹果", "消费电子"), "MSFT": ("微软", "软件/云"),
    "GOOGL": ("谷歌", "搜索/广告"), "AMZN": ("亚马逊", "电商/云"),
    "NVDA": ("英伟达", "AI 芯片"), "META": ("脸书母公司", "社交广告"),
    "TSLA": ("特斯拉", "电动车"), "AVGO": ("博通", "芯片"),
    "NFLX": ("奈飞", "流媒体"), "DIS": ("迪士尼", "影视/乐园"),
    "GE": ("通用电气", "航空发动机"), "RTX": ("雷神技术", "军工"),
    "ISRG": ("直觉外科", "手术机器人"), "LMT": ("洛克希德马丁", "军工"),
    "BA": ("波音", "飞机制造"), "CAT": ("卡特彼勒", "工程机械"),
    "LRCX": ("泛林集团", "半导体设备"), "AMAT": ("应用材料", "半导体设备"),
    "ASML": ("阿斯麦", "光刻机"), "TSM": ("台积电", "晶圆代工"),
    "MU": ("美光", "存储芯片"), "AMD": ("AMD", "芯片"),
    "INTC": ("英特尔", "芯片"), "QCOM": ("高通", "手机芯片"),
    "JPM": ("摩根大通", "银行"), "GS": ("高盛", "投行"),
    "MS": ("摩根士丹利", "投行"), "BAC": ("美国银行", "银行"),
    "WFC": ("富国银行", "银行"), "C": ("花旗", "银行"),
    "V": ("Visa", "支付"), "MA": ("万事达", "支付"),
    "PYPL": ("PayPal", "支付"), "AXP": ("美国运通", "支付"),
    "JNJ": ("强生", "医药"), "PFE": ("辉瑞", "制药"),
    "MRK": ("默沙东", "制药"), "LLY": ("礼来", "制药/减肥药"),
    "ABBV": ("艾伯维", "制药"), "UNH": ("联合健康", "医疗保险"),
    "TMO": ("赛默飞", "实验室设备"), "ABT": ("雅培", "医疗器械"),
    "XOM": ("埃克森美孚", "石油"), "CVX": ("雪佛龙", "石油"),
    "COP": ("康菲石油", "石油"), "SLB": ("斯伦贝谢", "油服"),
    "WMT": ("沃尔玛", "零售"), "COST": ("好市多", "会员零售"),
    "HD": ("家得宝", "家居建材"), "TGT": ("塔吉特", "零售"),
    "NKE": ("耐克", "运动服饰"), "SBUX": ("星巴克", "餐饮"),
    "MCD": ("麦当劳", "餐饮"), "KO": ("可口可乐", "饮料"),
    "PEP": ("百事", "饮料/零食"), "PG": ("宝洁", "日用品"),
    "PM": ("菲利普莫里斯", "烟草"), "MDLZ": ("亿滋", "零食"),
    "GIS": ("通用磨坊", "食品"), "K": ("家乐氏", "食品"),
    "CHWY": ("Chewy", "宠物电商"), "FRPT": ("Freshpet", "宠物鲜粮"),
    "IBM": ("IBM", "IT 服务"), "ORCL": ("甲骨文", "数据库/云"),
    "CRM": ("Salesforce", "企业软件"), "ADBE": ("Adobe", "创意软件"),
    "NOW": ("ServiceNow", "企业软件"), "CSCO": ("思科", "网络设备"),
    "UBER": ("优步", "出行"), "ABNB": ("爱彼迎", "民宿"),
    "BKNG": ("Booking", "在线旅游"), "T": ("美国电话电报", "电信"),
    "VZ": ("威瑞森", "电信"), "TMUS": ("T-Mobile", "电信"),
    "BRK.B": ("伯克希尔", "控股"), "SAP": ("SAP", "企业软件"),
}

# 经济数据：「中文名 + 怎么看」。最后一项是"这条为什么值得看"。
ECON_INFO = [
    (re.compile(r"NAHB", re.I), ("全美住宅建筑商信心指数", "50 是荣枯线，低于 50 说明房市收缩")),
    (re.compile(r"\bCPI\b", re.I), ("消费者物价指数", "通胀最直接的读数，直接决定降息预期")),
    (re.compile(r"\bPPI\b", re.I), ("生产者物价指数", "上游成本，领先 CPI 一个环节")),
    (re.compile(r"\bPCE\b", re.I), ("核心 PCE 物价", "美联储最看重的通胀指标")),
    (re.compile(r"Nonfarm|Payroll", re.I), ("非农就业人数", "就业强弱的最直接读数，影响降息节奏")),
    (re.compile(r"Unemployment", re.I), ("失业率", "就业市场的另一面")),
    (re.compile(r"Jobless", re.I), ("初请失业金人数", "每周高频，裁员是否恶化的先行指标")),
    (re.compile(r"\bGDP\b", re.I), ("GDP 增速", "经济总量的季度读数")),
    (re.compile(r"Retail Sales", re.I), ("零售销售", "消费是否还有韧性")),
    (re.compile(r"Consumer Confidence", re.I), ("消费者信心指数", "消费意愿的先行指标")),
    (re.compile(r"Consumer Sentiment", re.I), ("密歇根消费者情绪", "含通胀预期分项")),
    (re.compile(r"\bISM\b", re.I), ("ISM 制造业指数", "50 是荣枯线")),
    (re.compile(r"\bPMI\b", re.I), ("PMI 采购经理指数", "50 是荣枯线")),
    (re.compile(r"Durable", re.I), ("耐用品订单", "企业资本开支意愿的先行读数")),
    (re.compile(r"Housing|Home Sales|Construction", re.I),
     ("房地产数据", "利率敏感型需求的温度计，看房贷利率如何传导到成交")),
    (re.compile(r"FOMC|Fed Chair|Interest Rate", re.I), ("美联储利率决议/讲话", "直接影响全市场定价锚")),
]

# 同一个指标常常同时给出「总量」和「环比」两个值（Housing Starts 1.300M / 1.9%），
# 只按指标名匹配会让两条显示成同一个名字，看起来像重复条目。
# 这里用值里带的单位再区分一次：M/K/B 是总量，% 是环比。
ECON_INFO_DETAIL = [
    (re.compile(r"Housing Starts", re.I), "M",
     ("新屋开工（年化户数）", "每月新开工的住宅数量，是房市的先行读数")),
    (re.compile(r"Housing Starts", re.I), "%",
     ("新屋开工（月率环比）", "同一指标较上个月的变化幅度")),
    (re.compile(r"Building Permits", re.I), "M",
     ("营建许可（年化户数）", "拿地到开工之间的前置审批，比开工更早一步")),
    (re.compile(r"Building Permits", re.I), "%",
     ("营建许可（月率环比）", "同一指标较上个月的变化幅度")),
    (re.compile(r"GDPNow", re.I), None,
     ("亚特兰大联储 GDPNow 模型",
      "用已公布数据实时推算的季度 GDP 增速，不是官方终值，会随新数据反复修正")),
    (re.compile(r"Existing Home Sales", re.I), None,
     ("成屋销售", "二手房成交，占美国房屋交易的大头")),
    (re.compile(r"New Home Sales", re.I), None,
     ("新屋销售", "新房成交，比开工更贴近终端需求")),
]


def econ_explain(name, value=""):
    """返回 (中文名, 一句人话解读)。先按「指标名 + 值形态」精确匹配，再退回按名匹配。"""
    v = str(value or "")
    for rx, unit, info in ECON_INFO_DETAIL:
        if rx.search(name or "") and (unit is None or unit in v):
            return info
    for rx, info in ECON_INFO:
        if rx.search(name or ""):
            return info
    return ("", "")


def trend_word(consensus, previous):
    """预期 vs 前值的方向。数字比大小即可，不做任何预测。"""
    def num(s):
        t = (str(s or "").replace("%", "").replace(",", "")
             .replace("$", "").replace("(", "-").replace(")", "").strip())
        mul = 1.0
        if t and t[-1].upper() in ("M", "K", "B"):
            mul = {"K": 1e3, "M": 1e6, "B": 1e9}[t[-1].upper()]
            t = t[:-1]
        try:
            return float(t) * mul
        except ValueError:
            return None
    a, b = num(consensus), num(previous)
    if a is None or b is None:
        return ""
    if abs(a - b) < 1e-9:
        return "预期与上期持平"
    return "预期回升" if a > b else "预期回落"
def _nasdaq_json(url, timeout=15):
    try:
        raw = _fetch_bytes(url, timeout=timeout)
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        print(f"[WARN] Nasdaq 接口失败 {type(e).__name__}", file=sys.stderr)
        return None


def fetch_earnings(date_str, limit=4):
    """明日财报：按市值筛大公司，保留盘前/盘后时段标记。

    这比「机构观点」更硬——是确定要发生的事，不是谁的观点。
    """
    d = _nasdaq_json(f"https://api.nasdaq.com/api/calendar/earnings?date={date_str}")
    rows = ((d or {}).get("data") or {}).get("rows") or []

    def mcap(r):
        v = str(r.get("marketCap") or "").replace(",", "").strip()
        try:
            return float(v)
        except ValueError:
            return 0.0

    rows = [r for r in rows if r.get("symbol")]
    rows.sort(key=lambda r: -mcap(r))
    out = []
    for r in rows[:limit]:
        sym = r["symbol"].upper()
        t = (r.get("time") or "").strip()
        when = {"time-pre-market": "盘前", "time-after-hours": "盘后"}.get(t, "")
        cn, sector = COMPANY_INFO.get(sym, ("", ""))
        out.append({
            "symbol": sym,
            "name": (r.get("name") or "").strip(),
            "name_cn": cn,
            "sector": sector,
            "when": when,
            "eps": (r.get("epsForecast") or "").strip(),
        })
    return out


IMPORTANT_ECON = re.compile(
    r"CPI|PPI|PCE|GDP|Nonfarm|Payroll|Unemployment|Jobless|Retail Sales|"
    r"Consumer Confidence|Consumer Sentiment|ISM|PMI|Housing|Durable|"
    r"FOMC|Fed Chair|Interest Rate|Trade Balance|Inflation|Average Earnings",
    re.I)


def fetch_econ_events(date_str, limit=4):
    """明日美国重要经济数据：事件名 + 预期值 + 前值。"""
    d = _nasdaq_json(f"https://api.nasdaq.com/api/calendar/economicevents?date={date_str}")
    rows = ((d or {}).get("data") or {}).get("rows") or []
    out = []
    for r in rows:
        if (r.get("country") or "") != "United States":
            continue
        name = (r.get("eventName") or "").strip()
        if not name or not IMPORTANT_ECON.search(name):
            continue
        cons = (r.get("consensus") or "").strip()
        prev = (r.get("previous") or "").strip()
        cn, note = econ_explain(name, cons)
        out.append({
            "time": (r.get("gmt") or "").strip(),
            "name": name,
            "name_cn": cn,
            "note": note,
            "consensus": cons,
            "previous": prev,
            "trend": trend_word(cons, prev),
        })
        if len(out) >= limit:
            break
    return out


# ============================================================ 明日展望
STRATEGY_HINT = re.compile(r"海外|美股|美国|全球|美联储|大类资产|中选|美元|港股|外盘")


def fetch_strategy_reports(days=3, limit=2):
    end = datetime.now(CN_TZ)
    begin = end - timedelta(days=days)
    url = ("https://reportapi.eastmoney.com/report/list"
           "?industryCode=*&pageSize=60&industry=*&rating=&ratingChange="
           "&beginTime=%s&endTime=%s&pageNo=1&fields=&qType=2&orgCode=&code=*"
           "&rcode=&p=1&pageNum=1"
           % (begin.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")))
    d = get_json(url, referer="https://data.eastmoney.com/report/strategyreport.jshtml",
                 retries=2, timeout=15)
    rows = (d or {}).get("data") or []
    pool = []
    for r in rows:
        title = (r.get("title") or "").strip()
        org = (r.get("orgSName") or r.get("orgName") or "").strip()
        code = r.get("infoCode") or ""
        if not title or not org or not code:
            continue
        if not STRATEGY_HINT.search(title):
            continue
        # 标题里带 A 股/国内指数的研报，落点是国内配置，不是美股展望
        if re.search(r"A股|沪指|深成指|创业板|科创板|北交所|大盘|两市", title):
            continue
        pool.append({"org": org, "title": title,
                     "date": (r.get("publishDate") or "")[:10],
                     "url": f"https://data.eastmoney.com/report/info/{code}.html"})

    # 优先直接谈海外/美股的研报，其次按日期新到旧
    pool.sort(key=lambda p: (0 if re.search(r"美股|美国|海外|全球|美联储", p["title"]) else 1,
                             [-ord(c) for c in p["date"]]))
    picked = pool[:limit]
    for p in picked:
        p["digest"] = fetch_report_digest(p["url"])
        time.sleep(0.4)
    return picked


def fetch_report_digest(url):
    h = _fetch(url, referer="https://data.eastmoney.com/report/strategyreport.jshtml",
               retries=1, timeout=15)
    if not h:
        return ""
    i = h.find("zw-content")
    if i < 0:
        return ""
    gt = h.find(">", i)
    seg = h[(gt + 1 if gt > 0 else i):i + 30000]
    end = seg.find('class="right"')
    if 0 < end < 25000:
        seg = seg[:end]
    seg = re.sub(r"<script[\s\S]*?</script>", " ", seg)
    plain = re.sub(r"<[^>]+>", " ", seg)
    plain = re.sub(r"&nbsp;|&#\d+;", " ", plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    for anchor in ("查看PDF原文", "查看PDF", "www.eastmoney.com"):
        j = plain.find(anchor)
        if 0 <= j < 120:
            plain = plain[j + len(anchor):].strip()
    for label in ("摘要（核心观点）：", "摘要(核心观点)：", "核心观点：", "投资要点：", "摘要："):
        j = plain.find(label)
        if 0 <= j < 200:
            plain = plain[j + len(label):].strip()
            break
    plain = re.split(r"(风险提示|风险因素|免责声明|相关研究|查看PDF)", plain)[0].strip()
    # 标签在页面上常写成「核心观点 美股企稳需要…」（无冒号），会残留在句首
    plain = re.sub(r"^(核心观点|投资要点|摘要|投资建议|主要观点)\s*[:：]?\s*", "", plain)
    sentences = [s.strip() for s in re.split(r"[。！？]", plain) if len(s.strip()) >= 12]
    if not sentences:
        return ""
    # 剔除导语型套话：它们描述「本文写了什么」，不构成判断
    meta = re.compile(r"^(本文|本报告|本篇|报告|我们主要|文章)|分为|篇幅|另行撰文|"
                      r"敬请关注|系列报告|详见")
    usable = [s for s in sentences if not meta.search(s)]
    if not usable:
        return ""
    fwd = re.compile(r"配置|建议关注|展望|后续|判断|有望|维持|预计|海外|美股|美元|降息")
    pick = next((s for s in usable if fwd.search(s)), usable[0])
    if len(pick) > 110:
        pick = pick[:110].rstrip("，,、；;") + "…"
    return pick + "。"


# ============================================================ 归因
# 美股题材词表：用于在「领涨板块/权重股」与「当日要闻」之间找交集
THEME_LEX = {
    "科技": ["科技", "AI", "算力", "云", "软件", "数据中心", "资本开支"],
    "半导体": ["半导体", "芯片", "英伟达", "存储", "台积电", "光刻", "博通", "代工"],
    "通信服务": ["谷歌", "Meta", "广告", "流媒体", "电信", "搜索"],
    "可选消费": ["特斯拉", "亚马逊", "零售", "消费", "电商", "汽车", "耐克"],
    "必需消费": ["食品", "饮料", "日用品", "宝洁", "可口可乐"],
    "金融": ["银行", "投行", "高盛", "摩根", "保险", "信贷", "收益率曲线"],
    "医疗保健": ["医药", "制药", "减肥药", "礼来", "FDA", "临床", "生物科技"],
    "工业": ["工业", "制造", "航空", "国防", "资本品", "运输"],
    "材料": ["铜", "化工", "钢铁", "原材料", "锂"],
    "能源": ["原油", "油价", "OPEC", "页岩", "天然气", "炼化"],
    "公用事业": ["电力", "电网", "核电", "公用", "用电"],
    "房地产": ["房地产", "REITs", "房贷", "商业地产"],
}

ATTRIBUTION_NOTE = "以下由盘面数据与当日报道交叉推导，属相关性对齐，不能直接认定为涨跌原因。"


def build_attribution(d, em_pool=None):
    """把盘面数据翻译成「为什么涨/跌」的判断。

    原则（沿用 A 股版）：数字只在页面顶部出现一次，这里只给判断、不复述数字；
    因果关系一律标注为相关性对齐。

    em_pool 是完整的东财资讯池（有正文、可判断事件型），不是最终 3 条要闻——
    用最终要闻做催化匹配会大面积漏配。
    """
    idx = d.get("indices") or {}
    macro = d.get("macro") or {}
    mag7 = d.get("mag7") or []
    sec = d.get("sectors") or []
    amt = d.get("amount") or {}
    news = d.get("news") or []

    def p(name):
        q = idx.get(name)
        return q["pct"] if q else None

    dj, ixic, spx = p("道琼斯"), p("纳斯达克"), p("标普500")
    rut, sox = p("罗素2000"), p("费城半导体")
    blocks = []

    # 1) 结构：这是哪种行情
    if ixic is not None and dj is not None and spx is not None:
        if ixic - dj > 0.8:
            v = ("纳指显著强于道指，成长股承担了当日主要涨幅，资金愿意为远期现金流付溢价——"
                 "这是风险偏好抬升的进攻型结构")
        elif dj - ixic > 0.8:
            v = ("道指强于纳指，资金从长久期成长股撤向现金流确定的权重与防御板块，"
                 "属于典型的风险规避式轮动")
        elif ixic > 0 and dj > 0:
            v = "三大指数同向收红，普涨格局，赚钱效应有外溢"
        elif ixic < 0 and dj < 0:
            v = "三大指数同步走弱，系统性回调，个股很难走出独立行情"
        else:
            v = "指数涨跌互现，结构性行情，选方向比调仓位更重要"

        tail = ""
        if rut is not None and spx is not None:
            if rut - spx > 0.5:
                tail += "；小盘罗素2000 跑赢标普，说明行情不只在权重股里，风险偏好向低流动性资产扩散"
            elif spx - rut > 0.8:
                tail += "；小盘罗素2000 明显落后，资金只敢待在流动性最好的大票里"
        if sox is not None:
            if sox > 1.5:
                tail += "；费城半导体大涨，是当日风险偏好的最强读数"
            elif sox < -1.5:
                tail += "；费城半导体重挫，科技主线本身在失血"
        blocks.append({"k": "结构", "t": v + tail + "。"})

    # 2) 广度与量能：指数涨幅是不是「少数股票抬出来的」
    spy = (macro.get("标普500ETF") or {}).get("pct")
    rsp = (macro.get("标普等权") or {}).get("pct")
    if spy is not None and rsp is not None:
        gap = spy - rsp
        avg7 = (sum(x["pct"] for x in mag7) / len(mag7)) if mag7 else None
        if gap > 0.4:
            v = ("标普市值加权明显强于等权指数——指数是被少数超大权重股抬上去的，"
                 "多数成分股并没有跟上，这种上涨的内部结构偏脆弱")
        elif gap < -0.4:
            v = ("等权指数强于市值加权，说明上涨由多数个股共同推动，广度健康，"
                 "不是靠几只巨头撑门面")
        else:
            v = "等权与市值加权基本同步，上涨的广度与集中度都没有走极端"

        if avg7 is not None and spx is not None:
            if avg7 - spx > 1.0:
                v += f"；MAG7 平均涨幅高出标普 {avg7 - spx:.1f} 个百分点，七巨头是当日指数的直接发动机"
            elif spx - avg7 > 1.0:
                v += "；MAG7 反而跑输标普，指数上涨靠的是权重股以外的力量，这是少见的扩散信号"
        blocks.append({"k": "广度", "t": v + "。"})

    if amt.get("delta") is not None and amt.get("total"):
        if amt["delta"] > 0 and spx is not None and spx > 0:
            v = "成交额较上一交易日放大且指数收涨，量价配合是健康的，增量资金在进场"
        elif amt["delta"] > 0 and spx is not None and spx < 0:
            v = "放量下跌，抛压是有真实成交支撑的，不是无量阴跌，短期需要时间消化"
        elif amt["delta"] < 0 and spx is not None and spx > 0:
            v = "缩量上涨，参与度不足，涨幅的持续性需要后续放量确认"
        elif amt["delta"] < 0:
            v = "缩量整理，多空都在观望，缺乏方向性资金"
        else:
            v = "成交活跃度与上一交易日基本持平"
        blocks.append({"k": "量能", "t": v + "（成交额以 SPY+QQQ 为活跃度代理）。"})

    # 3) 主线：钱往哪走
    if sec:
        ranked = sorted(sec, key=lambda x: -x["pct"])
        top, bottom = ranked[:3], ranked[-3:]
        seg = "买盘集中在 " + "、".join(f"{x['name']}" for x in top if x["pct"] > 0)
        if all(x["pct"] <= 0 for x in top):
            seg = "全部板块收跌，最强的 " + top[0]["name"] + " 也没能守住红盘"
        down_names = [x["name"] for x in bottom if x["pct"] < 0]
        if down_names:
            seg += "；卖盘主要来自 " + "、".join(down_names)
        if mag7:
            best = max(mag7, key=lambda x: x["pct"])
            worst = min(mag7, key=lambda x: x["pct"])
            seg += f"。权重股里 {best['name']} 最强、{worst['name']} 最弱"
        blocks.append({"k": "主线", "t": seg + "。"})

    # 4) 催化：在有正文的资讯稿与事件型快讯里，找与领涨方向相关性最强的消息
    #    打分制而非「先命中先取」——后者会被无关的弱相关消息抢占（例如「汽车」命中可选消费，
    #    但真正驱动当日的是「大摩把亚马逊列为首选股」）。
    up_sectors = [x["name"] for x in sorted(sec, key=lambda y: -y["pct"])[:4] if x["pct"] > 0]
    leaders = [x["name"] for x in sorted(mag7, key=lambda y: -y["pct"])[:3] if x["pct"] > 0]
    pool = em_pool or []
    cand_pool = [n for n in pool
                 if EVENT_RE.search(n["title"]) and not MOVE_RE.search(n["title"])]
    strong = re.compile(r"大摩|摩根|高盛|花旗|瑞银|美银|上调|下调|目标价|首选|财报|"
                        r"订单|获批|批准|超预期|不及预期|指引|宣布|达成")
    hit, best_score = None, 0
    for name in up_sectors + leaders:
        words = []
        for key, ws in THEME_LEX.items():
            if key == name or key in name or name in key:
                words = ws
                break
        if not words:
            continue
        for n in cand_pool:
            title = n["title"]
            s = sum(3 for w in words if w in title)
            s += sum(6 for w in leaders if w in title)      # 命中领涨个股是最强信号
            if strong.search(title):
                s += 4
            if s > best_score:
                best_score, hit = s, (name, n)
    if hit and best_score >= 9:
        blocks.append({"k": "催化", "t":
                       f"{hit[0]}方向的走强与当日消息面同向——{hit[1]['time']}「{hit[1]['title']}」。"
                       f"这只是时间上的对齐，不能据此认定因果。"})
    elif news:
        blocks.append({"k": "催化", "t":
                       f"领涨方向没有找到直接对应的事件驱动，更像资金自发的仓位再平衡；"
                       f"当日信息量最大的是「{news[0]['title'][:40]}」，但它解释不了盘面的主要涨幅。"})

    # 5) 宏观锚：只提示同向/背离，不给预测
    tlt = (macro.get("20年美债") or {}).get("pct")
    uup = (macro.get("美元") or {}).get("pct")
    gld = (macro.get("黄金") or {}).get("pct")
    parts = []
    if tlt is not None:
        if tlt < -0.3 and spx is not None and spx > 0.3:
            parts.append("长端美债走弱（利率上行）却没能压住股指，说明当日定价矛盾不在利率端，"
                         "这种背离本身值得警惕")
        elif tlt < -0.3:
            parts.append("长端美债走弱，利率上行在压制高估值资产")
        elif tlt > 0.3:
            parts.append("长端美债走强、利率回落，对估值是顺风")
        else:
            parts.append("长端美债基本走平，利率不是当日的主要矛盾")
    if uup is not None and abs(uup) >= 0.3:
        parts.append("美元" + ("走强，对新兴市场与大宗构成压力" if uup > 0 else "走弱，risk-on 的资金环境"))
    if gld is not None and abs(gld) >= 0.8:
        parts.append("黄金" + ("大涨，避险或降息预期在升温" if gld > 0 else "回落，避险需求降温"))
    if parts:
        blocks.append({"k": "宏观", "t": "；".join(parts) + "。"})

    return blocks


# ============================================================ 推送条目
def build_items(d):
    items = []
    idx = d.get("indices") or {}
    order = ["道琼斯", "纳斯达克", "标普500", "罗素2000", "费城半导体"]
    parts = [f"{k} {idx[k]['pct']:+.2f}%" for k in order if k in idx]
    if parts:
        items.append({"tag": "指数", "title": "收盘", "text": "、".join(parts) + "。"})
    for b in (d.get("attribution") or [])[:2]:
        items.append({"tag": b["k"], "title": b["k"], "text": b["t"]})
    for n in (d.get("news") or [])[:2]:
        items.append({"tag": "要闻", "title": n["title"][:24], "text": n["summary"]})
    ot = d.get("outlook") or []
    if ot:
        items.append({"tag": "展望", "title": ot[0]["org"],
                      "text": f"{ot[0]['org']}：{ot[0]['title']}"})
    for i, it in enumerate(items, 1):
        it["no"] = i
    return items[:6]


# ============================================================ 主流程
def main():
    now = datetime.now(CN_TZ)
    et_now = datetime.now(ET)
    today = now.strftime("%Y-%m-%d")
    print(f"[INFO] 运行时间 {now:%Y-%m-%d %H:%M} 北京 / {et_now:%Y-%m-%d %H:%M} 美东")

    hist = load_history()
    brief = {
        "date": today,
        "weekday": WEEKDAY_CN[now.weekday()],
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "is_trading_day": False,
        "items": [],
    }

    quotes, trade_time = fetch_all_quotes()
    # 先打日志再判定：judge_trading_day 抛错时，这几行是排查的第一现场
    print(f"[INFO] 行情时间 {trade_time or '无'} / 取到 指数{len(quotes.get('indices') or {})} "
          f"板块{len(quotes.get('sectors') or {})} 权重股{len(quotes.get('mag7') or {})}")
    ok, trade_date, reason = judge_trading_day(trade_time, et_now)
    print(f"[INFO] 判定 {'交易日' if ok else '休市'} {reason}")

    if not ok:
        # 只有「真休市」才会走到这里：judge_trading_day 已经把接口故障抛成
        # FatalFetchError，不会再伪装成休市混进来
        brief["reason"] = reason
    else:
        # 判定为交易日，但数据残缺到没法成稿时，宁可失败告警也不要推一份
        # 只有两三个板块的半成品
        n_idx = len(quotes.get("indices") or {})
        n_sec = len(quotes.get("sectors") or {})
        if n_idx < 4:
            raise FatalFetchError(
                f"判定为交易日，但只取到 {n_idx} 个指数行情（至少需要 4 个）。")
        if n_sec < 6:
            raise FatalFetchError(
                f"判定为交易日，但只取到 {n_sec} 个板块行情（至少需要 6 个）。")

        brief["is_trading_day"] = True
        brief["trade_date"] = trade_date
        brief["indices"] = quotes["indices"]
        brief["sectors"] = quotes["sectors"]
        brief["mag7"] = quotes["mag7"]
        brief["macro"] = quotes["macro"]

        # 成交额：SPY + QQQ 作为活跃度代理
        spy_q = quotes["macro"].get("标普500ETF")
        total = 0.0
        try:
            txt = _fetch("https://qt.gtimg.cn/q=usSPY,usQQQ", encoding="gbk")
            parsed = parse_tencent(txt)
            for c in ("usSPY", "usQQQ"):
                q = pack_quote(parsed.get(c))
                if q:
                    total += q["amount"]
        except Exception:
            pass
        prev, prev_date = resolve_prev_amount(trade_date, hist)
        brief["amount"] = {
            "total": round(total / 1e8, 0),
            "prev": round(prev / 1e8, 0) if prev else 0,
            "delta": round((total - prev) / 1e8, 0) if prev else None,
            "unit": "亿美元",
            "proxy": "SPY+QQQ",
        }

        # 资讯窗口：美股开盘前 1 小时 → 收盘后 4 小时（美东 20:00），两端都严格封口
        # 用 zoneinfo 换算，夏令时/冬令时自动适配
        try:
            from datetime import time as dtime
            trade_day = datetime.strptime(trade_date, "%Y-%m-%d").date()
            open_et = datetime.combine(trade_day, dtime(9, 30), tzinfo=ET)
            start_cn = (open_et - timedelta(hours=1)).astimezone(CN_TZ)
            close_et = datetime.combine(trade_day, dtime(16, 0), tzinfo=ET)
            end_cn = min((close_et + timedelta(hours=4)).astimezone(CN_TZ), now)
        except Exception:
            start_cn = now - timedelta(hours=12)
            end_cn = now
        print(f"[INFO] 资讯窗口 {start_cn:%m-%d %H:%M} → {end_cn:%m-%d %H:%M}（北京时间）")

        focus_sectors = [x["name"] for x in sorted(
            quotes["sectors"], key=lambda y: -y["pct"])[:4] if x["pct"] > 0]

        # ---------- 外媒要闻（主源） ----------
        # 国内媒体对美股的转述有损耗和标题党，改为直接读一手外媒再译。
        foreign = []
        try:
            foreign = fetch_foreign_news(start_cn, end_cn)
        except Exception as e:
            print(f"[WARN] 外媒抓取失败 {e}", file=sys.stderr)

        news = []
        used_urls = set()          # 要闻已占用的链接，展望不能再重复
        if foreign:
            picked = pick_foreign_news(foreign, limit=3, focus_sectors=focus_sectors)
            print(f"[INFO] 外媒要闻选中 {len(picked)} 条，开始翻译")
            for i, it in enumerate(picked, 1):
                zh_title = translate(it["title"])
                time.sleep(0.6)
                body_en = it["summary"] or ""
                zh_body = translate(body_en) if body_en else ""
                if zh_body:
                    time.sleep(0.6)
                # 部分 CNBC 条目的 description 是栏目固定文案（“每个工作日发布 Homestretch”），
                # 不是新闻内容，检测到就丢弃、回退到标题，避免摘要跑题。
                if any(b in (zh_body or "") for b in BAD_SUMMARY) or \
                   any(b.lower() in body_en.lower() for b in BAD_SUMMARY_EN):
                    zh_body = ""
                news.append({
                    "no": i,
                    "time": it["time"],
                    "title": zh_title,
                    "title_en": it["title"],
                    "summary": (zh_body or zh_title)[:160],
                    "url": it["url"],
                    "media": f'{it["source"]} · {it["section"]}',
                    "kind": "foreign",
                })
                used_urls.add(it["url"])
        # ---------- 国内兜底（外媒全挂时不开天窗） ----------
        em_news, feed = [], []
        try:
            em_news = fetch_em_news(start_cn, end_cn)
        except Exception as e:
            print(f"[WARN] 东财资讯失败 {e}", file=sys.stderr)
        try:
            feed = crawl_sina_feed(start_cn, end_cn)
        except Exception as e:
            print(f"[WARN] 新浪快讯失败 {e}", file=sys.stderr)

        if not news:
            print("[WARN] 外媒无可用量，降级国内资讯源")
            try:
                focus = focus_sectors + [x["name"] for x in sorted(
                    quotes["mag7"], key=lambda y: -y["pct"])[:2] if x["pct"] > 0]
                news = pick_news(em_news, feed, limit=3, focus_words=focus)
            except Exception as e:
                print(f"[WARN] 国内要闻也失败 {e}", file=sys.stderr)
        brief["news"] = news

        # ---------- 明日事件（硬数据，比观点可靠） ----------
        try:
            nxt = trade_day + timedelta(days=1)
            while nxt.weekday() >= 5:
                nxt += timedelta(days=1)
            nd = nxt.strftime("%Y-%m-%d")
            brief["calendar"] = {
                "date": nd,
                "earnings": fetch_earnings(nd),
                "econ": fetch_econ_events(nd),
            }
            print(f"[INFO] 明日({nd}) 财报 {len(brief['calendar']['earnings'])} 家 / "
                  f"经济数据 {len(brief['calendar']['econ'])} 项")
        except Exception as e:
            print(f"[WARN] 日历失败 {e}", file=sys.stderr)

        # ---------- 明日展望：优先外媒机构观点，其次国内研报 ----------
        try:
            # 个人财经节目（Cramer / Mad Money 类）是评论秀，不是机构研究观点，
            # 放在「展望」里会污染信息质量，先剔除；机构研究/分析师表述优先。
            personal = re.compile(r"cramer|mad money|suze orman|dave ramsey", re.I)
            inst = re.compile(r"goldman|jpmorgan|morgan stanley|bank of america|"
                              r"citi|ubs|strategist|analyst|economist|research note|"
                              r"survey|fed official|treasury secretary", re.I)
            view_re = re.compile(r"expect|forecast|outlook|predict|says|warns|sees|"
                                 r"strategist|analyst|estimate", re.I)
            pool_v = [n for n in foreign
                      if view_re.search(n["title"])
                      and not personal.search(n["title"])
                      and not any(x in n["title"].lower() for x in EN_NOISE)
                      and n["url"] not in used_urls]      # 不能把要闻原样搬来当展望
            pool_v.sort(key=lambda x: (0 if inst.search(x["title"]) else 1, -x.get("weight", 1)))
            views, used_src = [], set()
            for v in pool_v:                      # 第一轮：同源只取一条，保证视角分散
                if v["source"] in used_src:
                    continue
                used_src.add(v["source"])
                views.append(v)
                if len(views) >= 2:
                    break
            if len(views) < 2:                    # 不足再放宽同源
                for v in pool_v:
                    if v in views:
                        continue
                    views.append(v)
                    if len(views) >= 2:
                        break
            outlook = []
            for v in views:
                zh_t = translate(v["title"])
                time.sleep(0.6)
                zh_d = translate(v["summary"]) if v["summary"] else ""
                time.sleep(0.6)
                outlook.append({
                    "org": f'{v["source"]} · {v["section"]}',
                    "title": zh_t,
                    "title_en": v["title"],
                    "digest": (zh_d or "")[:140],
                    "date": v["dt"].strftime("%m-%d %H:%M"),
                    "url": v["url"],
                    "kind": "foreign",
                })
            if not outlook:
                print("[INFO] 外媒无机构观点，降级东财研报")
                outlook = fetch_strategy_reports()
            brief["outlook"] = outlook
        except Exception as e:
            print(f"[WARN] 展望失败 {e}", file=sys.stderr)

        # 归因用的催化候选池：东财正文稿 + 新浪事件型快讯
        # （归因文案是中文，用中文语料匹配更稳；外媒要闻不参与因果匹配）
        attr_pool = [{"time": n["time"], "title": n["title"], "url": n["url"]} for n in em_news]
        for f in feed:
            t, _ = split_title(f["text"])
            if 12 <= len(t) <= 90:
                attr_pool.append({"time": f["time"], "title": t, "url": f["url"]})
        brief["attribution"] = build_attribution(brief, attr_pool)
        brief["items"] = build_items(brief)

        if total > 0:
            hist[trade_date] = total
            save_history(hist)

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "brief.json"), "w", encoding="utf-8") as f:
        json.dump(brief, f, ensure_ascii=False, indent=2)

    md = [f"### 美股收盘简报 {brief.get('trade_date', brief['date'])}", ""]
    if brief["is_trading_day"]:
        for b in brief.get("attribution") or []:
            md.append(f"- **{b['k']}** — {b['t']}")
        for n in brief.get("news") or []:
            md.append(f"- **要闻** — {n['title']}（{n['time']}）")
        for o in brief.get("outlook") or []:
            md.append(f"- **展望** — {o['org']}：{o['title']}")
    else:
        md.append(f"- 今日无美股交易：{brief.get('reason', '')}")
    with open(os.path.join(DATA_DIR, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")

    print(f"[OK] 交易日={brief['is_trading_day']} 板块={len(brief.get('sectors') or [])} "
          f"归因={len(brief.get('attribution') or [])}段 要闻={len(brief.get('news') or [])}条 "
          f"展望={len(brief.get('outlook') or [])}条")


if __name__ == "__main__":
    main()
