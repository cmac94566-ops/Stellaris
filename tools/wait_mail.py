# -*- coding: utf-8 -*-
"""常驻守望：zcode 一来信就退出，借"后台任务完成"把我唤醒 —— 收到邮件即响应。

为什么这么设计
--------------
宿主里唯一能"主动叫我"的通道是**后台任务完成通知**。脚本自己不能调模型，
但**它可以死守到事件发生、然后退出** ⇒ 退出即通知 ⇒ 我起一轮来处理。
所以"事件驱动"落地成：**长守 + 退出即唤醒**，把感知延迟从"每小时一轮"
压到"来信后 ≤轮询间隔"。

不做什么
--------
不启停游戏、不写任何文件、不发信 —— **纯只读**，只观 `HANDOFF/inbox_workbuddy.md`。
判断"对方是否读了我方信"仍交给 `tools/mail_lint.py --watchdog`（那条 30 分钟阈值
是既定机制，不该在这里重复实现）。

退出码
------
  0 收到新来信（stdout 已含编号与主题，可直接用于响应）
  2 超时（默认 8 小时无人来信）—— 说明对方整晚没回，交小时巡检兜底
  3 邮箱文件异常（不存在/不可读）

用法::

    python tools/wait_mail.py                 # 常驻 8 小时，10 秒一查
    python tools/wait_mail.py 3600            # 只守 1 小时
    python tools/wait_mail.py 28800 5         # 8 小时，5 秒一查

注意：**退出即失效**，处理完来信后要重新挂一次（否则回到每小时轮询的旧节奏）。
"""
import os
import re
import sys
import time

ROOT = r"C:/Users/<user>/.zcode/workspace/default/Stellaris_Overmind"
INBOX = os.path.join(ROOT, "HANDOFF/inbox_workbuddy.md")
HDR = re.compile(r"(?m)^## \[([^\]]+)\]\s*(.*)$")

TIMEOUT = int(sys.argv[1]) if len(sys.argv) > 1 else 28800      # 默认 8 小时
INTERVAL = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0    # 默认 10 秒


def snap():
    """→ (条目编号列表, 待处理数, 全部条目原始块列表)"""
    if not os.path.exists(INBOX):
        return None, 0, []
    try:
        t = open(INBOX, encoding="utf-8").read()
    except Exception:
        return None, 0, []
    blocks = [b for b in re.split(r"(?m)^(?=## \[)", t) if b.startswith("## [")]
    ids = [m.group(1) for m in HDR.finditer(t)]
    pending = sum(1 for b in blocks if re.search(r"(?m)^-\s*状态:\s*待处理\s*$", b))
    return ids, pending, blocks


ids0, pend0, _ = snap()
if ids0 is None:
    print("[trigger] 邮箱文件不存在或不可读：%s" % INBOX)
    sys.exit(3)

print("[trigger] 已挂守：基线条目=%d 待处理=%d；间隔 %.0fs，超时 %ds"
      % (len(ids0), pend0, INTERVAL, TIMEOUT), flush=True)

t0 = time.time()
while time.time() - t0 < TIMEOUT:
    time.sleep(INTERVAL)
    ids, pend, blocks = snap()
    if ids is None:
        print("[trigger] 邮箱文件消失/不可读，停止守望")
        sys.exit(3)
    new = [i for i in ids if i not in ids0]
    if new or pend > pend0:
        print("[trigger] ★ 收到来信 —— 编号: %s（待处理 %d → %d）" % (new or "(状态变更)", pend0, pend))
        for b in blocks:
            head = b.splitlines()[0]
            if any(("[" + n + "]") in head for n in new):
                lines = b.splitlines()
                subj = next((l for l in lines if l.strip().startswith("- 主题:")), "")
                print("  ---")
                print("  " + head)
                if subj:
                    print("  " + subj.strip()[:200])
        print("[trigger] 请立即读信并按机制 v2 处理（回执 + 归档 + 按需回信）")
        sys.exit(0)

print("[trigger] 超时 %ds：无来信（交小时巡检兜底）" % TIMEOUT, flush=True)
sys.exit(2)
