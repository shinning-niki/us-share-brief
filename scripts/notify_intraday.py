#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""美股盘前 / 盘中简报 · 手机推送

与 notify.py 的差异
-------------------
收盘简报的正文由 build_brief.py 组装成一段段「维度」，推送层负责拼接。
盘前/盘中简报只是一份实时快照，正文在 fetch_intraday.py 里就已经排好版，
推送层直接原样下发，不再二次加工——加工环节越少，越不会把数字读错。

推送方式沿用收盘简报的结论：不带 url 参数，正文随消息下发。
Bark 消息一旦带 url，iOS 点通知是直接跳浏览器，而 GitHub Pages 在国内
时通时不通，等于把「看内容」绑在一条不可靠的路上。
"""

import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTRA = os.path.join(ROOT, "data", "intraday.json")
UA = "Mozilla/5.0 (compatible; USShareBriefBot/1.0)"

STAGE_TITLE = {"pre": "美股盘前", "mid": "美股盘中"}


def post_json(url, payload, timeout=20):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def send_bark(base, title, body):
    base = base.rstrip("/")
    origin, _, key = base.rpartition("/")
    if not origin.startswith("http"):
        origin, key = base, ""
    payload = {
        "title": title,
        "body": body,
        "group": "美股简报",      # 和收盘简报同组，一屏能看全当天三段
        "isArchive": 1,
    }
    if key:
        payload["device_key"] = key
    return post_json(origin + "/push", payload)


def send_feishu(url, title, body):
    # 正文里有换行，lark_md 会原样保留；标题按涨跌染色
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title},
                       "template": "blue"},
            "elements": [{"tag": "div",
                          "text": {"tag": "lark_md", "content": body}}],
        },
    }
    return post_json(url, payload)


def main():
    if not os.path.exists(INTRA):
        print("[ERR] 没有 intraday.json，抓取环节可能没跑。", file=sys.stderr)
        return 1
    with open(INTRA, encoding="utf-8") as f:
        d = json.load(f)

    if d.get("skipped"):
        print(f"[SKIP] 本次不推送：{d.get('reason', '')}")
        return 0

    stage = d.get("stage") or "pre"
    title = f"{STAGE_TITLE.get(stage, '美股')} {d.get('date', '')}"
    body = (d.get("text") or "").strip()
    if not body:
        print("[ERR] 正文为空，不推送空消息。", file=sys.stderr)
        return 1

    sent, failed, channels = [], [], []

    if os.environ.get("BARK_URL"):
        channels.append(("Bark", lambda: send_bark(os.environ["BARK_URL"], title, body)))
    if os.environ.get("FEISHU_WEBHOOK"):
        channels.append(("飞书", lambda: send_feishu(os.environ["FEISHU_WEBHOOK"], title, body)))

    if not channels:
        print("[ERR] 未配置任何推送渠道，简报没有送达。", file=sys.stderr)
        return 1

    for name, fn in channels:
        try:
            resp = fn()
            sent.append(name)
            print(f"[OK] {name} 推送成功 {str(resp)[:120]}")
        except Exception as e:                          # noqa: BLE001
            failed.append(name)
            print(f"[ERR] {name} 推送失败：{e}", file=sys.stderr)
        time.sleep(0.5)

    print(f"[DONE] {stage} 成功 {len(sent)} / 失败 {len(failed)}")
    if failed and not sent:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
