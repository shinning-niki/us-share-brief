#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股收盘简报 · 手机推送
支持多渠道，按环境变量自动选择（配置了哪个就走哪个，可多配）：
  FEISHU_WEBHOOK   飞书群自定义机器人 webhook
  BARK_URL         Bark（iOS）基础地址，如 https://api.day.app/你的Key
  SCT_KEY          Server酱 SendKey
  PUSHPLUS_TOKEN   PushPlus token
  TG_BOT_TOKEN + TG_CHAT_ID   Telegram
  PAGE_URL         简报页面地址（默认 GitHub Pages）

读 data/brief.json，非交易日直接跳过。
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIEF = os.path.join(ROOT, "data", "brief.json")

DEFAULT_PAGE = "https://shinning-niki.github.io/us-share-brief/"
UA = "Mozilla/5.0 (compatible; USShareBriefBot/1.0)"


def post_json(url, payload, timeout=15):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def get_url(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def title_of(b):
    return f"美股收盘简报 {b.get('trade_date', b.get('date', ''))}"


def build_text(b):
    """纯文本摘要：每条一行。"""
    return "\n".join(f"{it['no']}. {it['text']}" for it in (b.get("items") or []))


# --------------------------------------------------------------- 飞书
def send_feishu(url, b, page):
    items = b.get("items") or []
    md = "\n\n".join(f"**{it['no']}. {it['title']}**\n{it['text']}" for it in items)

    spx = ((b.get("indices") or {}).get("标普500") or {}).get("pct", 0)
    template = "red" if spx > 0 else ("green" if spx < 0 else "blue")

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title_of(b) + "(美东)"},
                "template": template,
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": md}},
                {"tag": "hr"},
                {"tag": "note", "elements": [
                    {"tag": "plain_text",
                     "content": f"生成于 {b.get('generated_at','')}(北京) · 数据仅供参考，不构成投资建议"}
                ]},
                {"tag": "action", "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "查看手机版 ↗"},
                    "url": page,
                    "type": "primary",
                }]},
            ],
        },
    }
    return post_json(url, payload)


# --------------------------------------------------------------- Bark
def send_bark(base, b, page):
    title = f"美股收盘 {b.get('trade_date', '')}"
    body = "；".join((it["text"] or "").rstrip("。") for it in (b.get("items") or [])[:3])
    if len(body) > 180:
        body = body[:180] + "…"
    u = (base.rstrip("/") + "/" + urllib.parse.quote(title) + "/"
         + urllib.parse.quote(body)
         + "?url=" + urllib.parse.quote(page, safe="")
         + "&group=" + urllib.parse.quote("美股简报")
         + "&isArchive=1")
    return get_url(u)


# --------------------------------------------------------------- Server酱
def send_sct(key, b, page):
    url = f"https://sctapi.ftqq.com/{key}.send"
    data = urllib.parse.urlencode({
        "title": title_of(b),
        "desp": build_text(b) + f"\n\n[查看手机版]({page})",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", errors="ignore")


# --------------------------------------------------------------- PushPlus
def send_pushplus(token, b, page):
    payload = {
        "token": token,
        "title": title_of(b),
        "content": build_text(b) + f"<br><br><a href='{page}'>查看手机版</a>",
        "template": "html",
    }
    return post_json("https://www.pushplus.plus/send", payload)


# --------------------------------------------------------------- Telegram
def send_telegram(token, chat_id, b, page):
    text = f"*{title_of(b)}*\n\n" + build_text(b) + f"\n\n[查看手机版]({page})"
    payload = {"chat_id": chat_id, "text": text,
               "parse_mode": "Markdown", "disable_web_page_preview": False}
    return post_json(f"https://api.telegram.org/bot{token}/sendMessage", payload)


def main():
    if not os.path.exists(BRIEF):
        print("[SKIP] 没有 brief.json")
        return 0
    with open(BRIEF, "r", encoding="utf-8") as f:
        b = json.load(f)

    if not b.get("is_trading_day"):
        print(f"[SKIP] 非交易日，不推送（{b.get('reason','')}）")
        return 0

    page = os.environ.get("PAGE_URL", DEFAULT_PAGE)
    sent, failed = [], []
    channels = []

    if os.environ.get("FEISHU_WEBHOOK"):
        channels.append(("飞书", lambda: send_feishu(os.environ["FEISHU_WEBHOOK"], b, page)))
    if os.environ.get("BARK_URL"):
        channels.append(("Bark", lambda: send_bark(os.environ["BARK_URL"], b, page)))
    if os.environ.get("SCT_KEY"):
        channels.append(("Server酱", lambda: send_sct(os.environ["SCT_KEY"], b, page)))
    if os.environ.get("PUSHPLUS_TOKEN"):
        channels.append(("PushPlus", lambda: send_pushplus(os.environ["PUSHPLUS_TOKEN"], b, page)))
    if os.environ.get("TG_BOT_TOKEN") and os.environ.get("TG_CHAT_ID"):
        channels.append(("Telegram",
                         lambda: send_telegram(os.environ["TG_BOT_TOKEN"],
                                               os.environ["TG_CHAT_ID"], b, page)))

    if not channels:
        # 以前这里 return 0，Secret 一旦丢失（或从没写进仓库），工作流会
        # 显示绿色成功但什么都没送出去。没送出去就是失败，必须报错。
        print("[ERR] 未配置任何推送渠道，简报没有送达任何地方。", file=sys.stderr)
        print("      请至少配置 FEISHU_WEBHOOK / BARK_URL / SCT_KEY / PUSHPLUS_TOKEN 之一。",
              file=sys.stderr)
        return 1

    for name, fn in channels:
        try:
            resp = fn()
            sent.append(name)
            print(f"[OK] {name} 推送成功 {str(resp)[:120]}")
        except Exception as e:
            failed.append(name)
            print(f"[ERR] {name} 推送失败: {e}", file=sys.stderr)
        time.sleep(0.5)

    print(f"[DONE] 成功 {len(sent)} / 失败 {len(failed)}")
    if failed and not sent:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
