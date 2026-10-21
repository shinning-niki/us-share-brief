#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""标记当日已完成：推送成功之后才调用。

把「北京日期 -> 美东交易日」写进 data/sent.json，供 check_done.py 去重。
写成独立脚本而不是 workflow 里的内联 python，是为了让「什么时候写」
这件事只有一个地方定义，避免以后改流程时漏掉。

必须在 notify.py 成功之后调用：如果推送失败就先写了标记，
后续冗余触发点会误以为已完成而跳过，等于彻底放弃当天补发。
"""

import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
SENT = os.path.join(DATA, "sent.json")
BRIEF = os.path.join(DATA, "brief.json")
TZ_CN = datetime.timezone(datetime.timedelta(hours=8))

today = datetime.datetime.now(TZ_CN).strftime("%Y-%m-%d")

trade_date = ""
try:
    with open(BRIEF, encoding="utf-8") as f:
        trade_date = (json.load(f) or {}).get("trade_date", "") or ""
except (ValueError, OSError) as exc:
    print(f"[WARN] 读取 brief.json 失败：{exc}", file=sys.stderr)

data = {}
if os.path.exists(SENT):
    try:
        with open(SENT, encoding="utf-8") as f:
            data = json.load(f) or {}
    except (ValueError, OSError):
        data = {}

data[today] = trade_date

# 只留最近 60 天，避免文件无限增长
for k in sorted(data.keys())[:-60]:
    data.pop(k, None)

os.makedirs(DATA, exist_ok=True)
with open(SENT, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print(f"[OK] 已标记 {today} 完成（对应美东交易日 {trade_date or '未知'}）")
