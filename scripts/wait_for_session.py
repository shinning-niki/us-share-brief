#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""睡到盘前 / 盘中简报的目标时刻，用等待吸收 GitHub 的队列延迟。

为什么需要这个
--------------
和 wait_for_close.py 是同一套方法论：GitHub Actions 原生 cron 是 best-effort，
实测延迟 0.5 ~ 5.5 小时，且没有哪个 UTC 时段可靠。

但盘中简报比收盘简报更怕延迟——收盘简报晚两小时只是"晚一点知道"，盘中简报
晚两小时就变成"收盘复盘"，语义完全错位。所以不能把 cron 设在目标时刻附近，
必须提前排队，再把队列延迟用睡眠吃掉：

    pre  目标 美东 09:00（开盘前 30 分钟）
    mid  目标 美东 10:30（开盘后 60 分钟）

窗口保护
--------
睡醒之后如果已经错过语义窗口，必须放弃而不是硬发——把盘中数据当盘前发出去，
比不发更糟。所以每个阶段都有 hard_deadline：

    pre  美东 09:26 之后不再发（已开盘，盘前数据失效）
    mid  美东 12:00 之后不再发（已是午盘，不再是"开盘一小时"）

放弃后返回 reason=too-late，工作流直接跳过，等下一个触发点。
"""

import argparse
import os
import sys
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# 阶段配置：目标时刻 + 语义窗口上限
STAGES = {
    "pre": {
        "target": dtime(9, 0),      # 美东 09:00，开盘前 30 分钟
        "deadline": dtime(9, 26),   # 之后盘前已结束，不再发
        "label": "盘前",
        # cron 最早从 UTC 11:00 起跑（冬令时 ET 06:00），睡 3 小时即到目标。
        # 留 4 小时上限，超出说明 cron 设错，交给后面的触发点。
        "max_wait": 4 * 3600,
    },
    "mid": {
        "target": dtime(10, 30),    # 美东 10:30，开盘后 60 分钟
        "deadline": dtime(12, 0),   # 之后是午盘，不再是「开盘一小时」
        "label": "盘中",
        "max_wait": 4 * 3600,
    },
}

HEARTBEAT_SECONDS = 15 * 60


def gh_output(**kv):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        for k, v in kv.items():
            print(f"[OUTPUT] {k}={v}")
        return
    with open(path, "a", encoding="utf-8") as f:
        for k, v in kv.items():
            f.write(f"{k}={v}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=list(STAGES), required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印决策不真的睡（本地验证分支用）")
    args = ap.parse_args()
    cfg = STAGES[args.stage]

    now = datetime.now(ET)
    print(f"[INFO] 阶段={args.stage} 美东当前 {now.strftime('%Y-%m-%d %H:%M:%S %Z')}"
          f"（UTC{now.utcoffset().total_seconds() / 3600:+.0f}）")

    if now.weekday() >= 5:
        print("[INFO] 美东周末，不发盘中类简报。")
        gh_output(waited="0", reason="weekend")
        return 0

    target = now.replace(hour=cfg["target"].hour, minute=cfg["target"].minute,
                         second=0, microsecond=0)
    deadline = now.replace(hour=cfg["deadline"].hour, minute=cfg["deadline"].minute,
                           second=0, microsecond=0)
    delta = (target - now).total_seconds()

    # 已经过了语义窗口：宁可放弃，也不把盘中数据当盘前发
    if now > deadline:
        print(f"[WARN] 已过{cfg['label']}窗口上限（{cfg['deadline'].strftime('%H:%M')} ET），"
              f"现在 {now.strftime('%H:%M')} ET。本次跳过——晚到的盘前简报没有意义。")
        gh_output(waited="0", reason="too-late")
        return 0

    if delta <= 0:
        print(f"[INFO] 已到{cfg['label']}目标时刻（{cfg['target'].strftime('%H:%M')} ET），"
              f"直接执行。")
        gh_output(waited="0", reason="already-in-window")
        return 0

    if delta > cfg["max_wait"]:
        print(f"[WARN] 距目标还有 {delta / 3600:.2f} 小时，超过可承受的 "
              f"{cfg['max_wait'] / 3600:.1f} 小时。本次跳过，等后续触发点。")
        gh_output(waited="0", reason="too-early")
        return 0

    print(f"[INFO] 距{cfg['label']}目标（{cfg['target'].strftime('%H:%M')} ET）还有 "
          f"{delta / 60:.0f} 分钟，开始等待。把 cron 的队列延迟用睡眠吸收掉。")

    if args.dry_run:
        print(f"[DRY-RUN] 本应睡 {delta / 60:.0f} 分钟后抓数。")
        gh_output(waited=f"{delta:.0f}", reason="dry-run")
        return 0

    slept = 0
    next_beat = HEARTBEAT_SECONDS
    while slept < delta:
        chunk = min(30, delta - slept)
        time.sleep(chunk)
        slept += chunk
        if slept >= next_beat:
            print(f"[INFO] 已等待 {slept / 60:.0f} 分钟，"
                  f"距目标还有 {(delta - slept) / 60:.0f} 分钟")
            next_beat += HEARTBEAT_SECONDS

    after = datetime.now(ET)
    print(f"[INFO] 等待结束，美东 {after.strftime('%H:%M:%S')}，开始抓{cfg['label']}数据。")
    gh_output(waited=f"{slept:.0f}", reason="waited")
    return 0


if __name__ == "__main__":
    sys.exit(main())
