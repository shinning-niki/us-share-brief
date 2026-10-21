#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""当日完成闸门：判断今天的简报是否已经推送成功。

背景
----
GitHub Actions 原生 cron 是 best-effort：高峰期会延迟几十分钟，甚至静默丢弃
不跑。单点 cron 一旦被丢，当天就是整天空窗，且没有任何提示。
所以改成多个冗余触发点（见 daily-brief.yml），靠这个闸门去重：
第一个跑成功的写下标记，后续触发点看到标记就退出。

标记文件 data/sent.json
----------------------
    {"2026-10-21": "2026-10-20"}     北京日期 -> 对应的美东交易日

为什么用「北京日期」而不是「美东交易日」做 key
----------------------------------------------
整个推送窗口（北京 07:40 → 10:20）都落在同一个北京日期内，不会跨日。
若用美东日期，cron 延迟跨过美东午夜时会算出次日，导致同一天重复推送。
北京日期天然是一个不会漂移的窗口。

注意：这个文件只在「推送成功之后」才写入。抓取阶段写的 history.json
不能拿来当闸门用——否则推送失败但 history 已提交时，后续触发点会误判
为已完成，同样造成静默漏发。

用法
----
    python scripts/check_done.py            # 正常判断
    python scripts/check_done.py --force    # 忽略标记，强制重跑

输出（供 workflow 解析）
----------------------
    today=YYYY-MM-DD
    done=true|false
    skip=true|false
"""

import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SENT = os.path.join(ROOT, "data", "sent.json")
TZ_CN = datetime.timezone(datetime.timedelta(hours=8))

today = datetime.datetime.now(TZ_CN).strftime("%Y-%m-%d")
force = "--force" in sys.argv

done = False
trade_date = ""
if os.path.exists(SENT):
    try:
        with open(SENT, encoding="utf-8") as f:
            data = json.load(f) or {}
        done = today in data
        trade_date = data.get(today, "") or ""
    except (ValueError, OSError) as exc:
        # 标记文件损坏不应该阻塞发送——宁可重复推一次，不可漏发
        print(f"[WARN] 读取 sent.json 失败：{exc}", file=sys.stderr)
        done = False

skip = done and not force

print(f"today={today}")
print(f"trade_date={trade_date}")
print(f"done={'true' if done else 'false'}")
print(f"force={'true' if force else 'false'}")
print(f"skip={'true' if skip else 'false'}")
