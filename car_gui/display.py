#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开发板展示屏 —— **只读**大字客户端（方案《开发板展示屏方案》）。

v3（2026-10-29，真机照片第二轮反馈）
------------------------------------
v2 的教训写在最前面：**设计靠做减法，堆容器解决不了问题。**
v2 一屏里有 5 种"容器"互相抢戏 —— 左侧竖条、顶部四颗胶囊、满宽色块横幅、
进度条、整屏边框。每个都在强调自己，结果**第一眼落在色块上而不是内容上**。

v3 只回答两个问题：**它说了什么**、**它现在在干嘛**。其余全部退到背景。

    ① 顶部：一行 16 px 灰字   ● 车已连接 · 94% · 电脑在线
    ② 听到：标签 18 px + 正文 40 px 常规
    ③ 回应：标签 18 px + 正文 48 px 中黑   ← 视觉主角
    ④ 底部：一条 64 px 状态带（灰/绿/红）+ 右侧耗时

删掉的东西（都在 v2 里"加"出来的）：
    · 两条 4 px 彩色竖条      —— 只增加重量，不增加信息
    · 顶部 4 颗胶囊           —— 正常状态一行灰字就够，**只有异常才上色**
    · 满宽 104 px 色块横幅    —— 改成底部 64 px 状态带，不占满屏
    · 「响应 4.2s」独立胶囊   —— 并到状态带右侧，只留一个数
    · 「第 1/2 步」独立行     —— 并到状态带里
    · 大三角箭头              —— 删（"前进"两个字已经说清了）
    · 顶部 4 px 装饰色条      —— 删（纯装饰）
    · 竖条/紫色/蓝色装饰      —— 颜色只留语义：绿=运动、红=急停、黄=未连接/降级

三条硬约束（方案要求，改代码前先读）
------------------------------------
1. **绝不自己建 CarController**：串口独占，会和 `car_web.py` 抢。本文件
   **一行串口代码都没有**，只走 HTTP。
2. **请求必须带 `?role=display`**：否则大屏自己把 console_online 永远顶成 true。
3. **只用 `setPixelSize()`** + `QT_ENABLE_HIGHDPI_SCALING=0`，否则版式全崩。

用法
----
    bash run_display.sh                     # 全屏（板子本地终端 / VNC）
    bash run_display.sh --preview           # 假数据预览
    bash run_display.sh --screenshot a.png  # 离屏渲染（无需显示器）
    bash run_display.sh --theme dark        # 暗环境（分隔线/绿色单独调过）
    bash run_display.sh --scale 1.1         # 现场微调字号
    bash run_display.sh --screenshot a.png --preview-state estop   # 出四态图

退出码（`display_supervisor.sh` 靠它决定要不要重新拉起）
------------------------------------------------------
    0   用户按 Esc / F12 主动退出（＝想用桌面）→ 看护进程不再拉起
    3   没有图形环境（DISPLAY 空）→ 环境问题，不再空转
    1   找不到装了 PySide6 的 python3 → 环境问题，不再空转
    其它 崩溃 → 看护进程 2 秒后自己重启（方案 §7"崩了能自己回来"）

防息屏（2026-11-03 追加）
------------------------
板子上的省电/屏保守护进程不止一个（实测 LXQt 会同时拉起
`lxqt-powermanagement` + `xfce4-power-manager` + `xscreensaver`），
展示屏画面长期不动 → 几分钟后 X 黑屏；而电脑端发指令**不是 X 输入**，
叫不醒它。所以两道保险：

1. `setup_display_autostart.sh` 在 `~/.config/autostart/` 里把这些守护进程
   `Hidden=true` 关掉（根源）；
2. 本文件每 20 s 重申一次 `xset s off -dpms`；收到新指令/新事件时立刻
   `xset dpms force on`（兜底 + 秒醒）。
"""
from __future__ import print_function

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetrics,
                           QGuiApplication, QPainter, QPen)
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                               QProgressBar, QVBoxLayout, QWidget)

# ══════════════════════════════════════════════════════════════════════
#  X 防息屏小工具（测试里把它换掉，就能断言"到底发了什么 xset 参数"）
# ══════════════════════════════════════════════════════════════════════
def _xset(*args):
    """跑一条 `xset …`。没有 DISPLAY / 没装 xset 时静默失败（不影响展示）。"""
    if not os.environ.get("DISPLAY"):
        return False
    try:
        return subprocess.call(("xset",) + args, timeout=3,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL) == 0
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════
#  版式（600 px 基准，运行时按屏幕高等比缩放）
# ══════════════════════════════════════════════════════════════════════
BASE_H = 600.0
#: 顶部 44 · 听到 214 · 回应 **弹性**（吃满剩余）· 状态带 64 · 分隔线 2 = 600
H_TOP, H_HEARD, H_BAND = 44, 214, 64
H_REPLY_MIN = 236          # 回应区最小高度（实际会吃掉剩余空间）
#: 字号：16 顶部 / 18 标签 / 40 听到 / 48 回应 / 40 状态带 / 20 元信息
F_TOP, F_TAG, F_HEARD, F_REPLY, F_BAND, F_META = 16, 18, 40, 48, 40, 20

#: 每行字数上限的保守兜底（真实值按 QFontMetrics 实测宽度算）
CAP = {F_HEARD: (24, 2), F_REPLY: (20, 1)}

PAD_X = 30          # 左右留白
GAP_TAG = 6         # 标签与正文间距
BAR_MIN_W = 90      # 状态带里的迷你进度条宽度（基准 px）

# ── 配色：**只有语义色**，其余全是灰阶 ──────────────────────────────
LIGHT = {
    "bg": "#FFFFFF", "fg": "#20201E", "body": "#2C2C2A",
    "sub": "#8B8A84", "dim": "#A8A8A2",
    "line": "#E9E9E4",
    "warn": "#BA7517",                     # 未连接 / 降级
    "band_move": "#1D9E75",                # 运动正常（绿）
    "band_stop": "#6F6E67",                # 停止 / 待机（中性灰）
    "band_danger": "#E24B4A",              # 急停（红）
}
DARK = {
    "bg": "#14140F", "fg": "#F2F2EC", "body": "#E8E7E1",
    "sub": "#9C9B94", "dim": "#6E6D66",
    # ★ 深色主题单独调过：分隔线用半透明白，否则层次全塌
    "line": "rgba(255,255,255,0.12)",
    "warn": "#D9A24A",
    # ★ 深色下绿色降饱和（亮绿在深底上刺眼）
    "band_move": "#1E7A5C",
    "band_stop": "#3C3B35",
    "band_danger": "#C0453F",
}

#: 动作 → (中文, 状态带配色键)。**不再有箭头**
ACTION_STYLE = {
    "forward": ("前进", "band_move"),
    "backward": ("后退", "band_move"),
    "turn_left": ("左转", "band_move"),
    "turn_right": ("右转", "band_move"),
    "stop": ("停止", "band_stop"),
}

#: 中文字体优先级（板子 Noto Sans CJK SC；也列 Windows/macOS 便于本地预览）
CJK_FONTS = ("Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC",
             "WenQuanYi Zen Hei", "WenQuanYi Micro Hei", "Droid Sans Fallback",
             "Microsoft YaHei UI", "Microsoft YaHei", "SimHei",
             "PingFang SC", "Hiragino Sans GB")


def _batt_pct(v):
    """4S 电池电压 → 百分比（与网页控制台同一套曲线）。

    ⚠️ `status.voltage` 是**伏特**（如 15.9），别当百分比印 ——
    曾把满电 15.9 V 显示成"电量 15%"，看着像要没电。
    """
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    pts = [(16.2, 100), (14.8, 70), (13.5, 45), (12.0, 20), (10.5, 5)]
    if v >= pts[0][0]:
        return 100
    for a, b in zip(pts, pts[1:]):
        if b[0] <= v <= a[0]:
            r = (v - b[0]) / (a[0] - b[0])
            return int(round(b[1] + (a[1] - b[1]) * r))
    return 5


def _speed_label(mmps, cmd=None):
    """速度 → `中速 · 0.3 m/s`（**只在运动时显示**；停了就显示 —）。

    ★ 单位统一：屏幕别再出现"毫米每秒"和"米/秒"两套，统一 m/s。
    """
    try:
        v = float(mmps or 0)
    except (TypeError, ValueError):
        v = 0.0
    if v <= 0:
        return ""
    mps = v / 1000.0
    if cmd is None:
        word = "慢速" if mps < 0.35 else ("中速" if mps < 0.65 else "快速")
    else:
        try:
            c = int(cmd)
        except (TypeError, ValueError):
            c = 30
        word = "慢速" if c <= 27 else ("中速" if c <= 32 else "快速")
    return "%s · %.1f m/s" % (word, mps)


def _board_ip():
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return "板子IP:8090"


# ══════════════════════════════════════════════════════════════════════
#  数据获取
# ══════════════════════════════════════════════════════════════════════
class Fetcher(QThread):
    """每秒拉 `/api/status?role=display` 与 `/api/events?since=N`。

    · 连续失败 5 次才报"断连"（避免一次抖动就闪）；之后放慢到 2 s 重试
    · `seq` 回退（主控重启）→ 游标归零全量重拉
    · **首帧只取游标、不回放历史**：开机自启时不该把上一次演示的
      「前进一米 / 好的」又拿出来当成本次的话（2026-11-03 真机抓到）
    """

    tick = Signal(dict)

    def __init__(self, base, parent=None, replay=False):
        super(Fetcher, self).__init__(parent)
        self.base = base.rstrip("/")
        self._stop = False
        self.cursor = 0
        self.fails = 0
        self.replay = bool(replay)     # True = 连历史一起拉（调试用）
        self._primed = bool(replay)

    def stop(self):
        self._stop = True

    def _get(self, path, timeout):
        req = urllib.request.Request(self.base + path,
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def _prime(self):
        """把游标推到"当前"，这样第一帧不会有历史事件。"""
        try:
            ev = self._get("/api/events?since=999999999", 1.5)
            seq = ev.get("seq")
            if isinstance(seq, int):
                self.cursor = seq
            self._primed = True
            return True
        except Exception:
            return False           # 主控还没起来，下一轮再试

    def run(self):
        while not self._stop:
            t0 = time.time()
            pkt = {"ok": False, "ts": t0}
            if not self._primed:
                self._prime()
            try:
                pkt["status"] = self._get("/api/status?role=display", 1.5)
                try:
                    ev = self._get("/api/events?since=%d" % self.cursor, 1.5)
                except Exception:
                    ev = {}
                if ev:
                    seq = ev.get("seq")
                    if isinstance(seq, int) and seq < self.cursor:
                        self.cursor = 0
                        ev = self._get("/api/events?since=0", 1.5)
                    self.cursor = int(ev.get("since", ev.get("seq", self.cursor)) or 0)
                    pkt["events"] = ev.get("events") or []
                pkt["ok"] = True
                self.fails = 0
            except Exception as e:
                self.fails += 1
                pkt["error"] = "%s" % (e,)
                pkt["fails"] = self.fails
            if not self._stop:
                self.tick.emit(pkt)
            wait = 2.0 if self.fails >= 5 else 1.0
            time.sleep(max(0.05, wait - (time.time() - t0)))


# ══════════════════════════════════════════════════════════════════════
#  展示面板
# ══════════════════════════════════════════════════════════════════════
class Panel(QWidget):
    """整屏。四态：演示 / 待机 / 急停 / 断连。"""

    def __init__(self, screen_size=None, theme="light", preview=False, scale=None,
                 preview_state="plan"):
        super(Panel, self).__init__()
        sw, sh = (screen_size or (1024, 600))
        self.screen_size = (sw, sh)
        #: 缩放系数：以 600 px 高为基准（1024×600 → 1.0；1440p → 2.4）
        #: ⚠️ 屏幕尺寸靠实测：没接显示器时内核会报 1920×1080 这类默认值。
        auto_k = sh / BASE_H
        self.k = max(0.5, min(3.0, float(scale) if scale else auto_k))
        self.auto_k = auto_k
        self.C = DARK if theme == "dark" else LIGHT
        self.preview = preview
        self.preview_state = preview_state
        self.family, self.font_ok = self._pick_font()

        # ── 运行期状态 ──
        self.st = {}
        self.last_ok = 0.0
        self.fails = 0
        self.online_since = None
        self.turns = {}
        self.last_turn = None
        #: 用户按 Esc 主动退出 → 退出码 0（看护进程据此不再拉起）
        self._user_quit = False
        #: 防息屏：上次"点亮屏幕"的时间 / 上次的动作，用来决定要不要再 force on
        self._last_wake = 0.0
        self._last_action = None

        self.setStyleSheet("background:%s;" % self.C["bg"])
        self.setCursor(Qt.BlankCursor)
        self.setFocusPolicy(Qt.StrongFocus)   # 全屏时能收到键盘（Esc 退出）
        self._build()
        if preview:
            self._feed_preview()

    # ── 字体 ────────────────────────────────────────────────────
    def _pick_font(self):
        """挑一个真能渲染简体中文的字体（三级探测；Qt6 已无 writingSystems）。"""
        try:
            fams = list(QFontDatabase.families())
        except Exception:
            fams = []
        supports = []
        try:
            for f in fams:
                try:
                    if Qt.SimplifiedChinese in QFontDatabase.writingSystems(f):
                        supports.append(f)
                except Exception:
                    supports = []
                    break
        except Exception:
            supports = []
        if not supports:
            for pref in CJK_FONTS:
                try:
                    if QFontDatabase.hasFamily(pref):
                        supports.append(pref)
                except Exception:
                    if pref in fams:
                        supports.append(pref)
        for pref in CJK_FONTS:
            if pref in supports:
                return pref, True
        if supports:
            return supports[0], True
        base = QApplication.font().family() if QApplication.instance() else "Sans"
        return base, False

    def _font(self, px, weight=QFont.Normal):
        f = QFont(self.family)
        f.setPixelSize(max(8, int(round(px * self.k))))   # ★ 只用像素字号
        f.setWeight(weight)
        return f

    def _px(self, base):
        return int(round(base * self.k))

    # ── 小部件 ──────────────────────────────────────────────────
    def _label(self, px, color=None, weight=QFont.Normal, wrap=False, align=None):
        lb = QLabel("")
        lb.setFont(self._font(px, weight))
        lb.setWordWrap(wrap)
        lb.setAlignment(align or (Qt.AlignLeft | Qt.AlignVCenter))
        lb.setStyleSheet("color:%s; background:transparent;" % (color or self.C["body"]))
        return lb

    def _hline(self):
        f = QFrame()
        f.setFixedHeight(max(1, self._px(1)))
        f.setStyleSheet("background:%s;" % self.C["line"])
        return f

    # ── 建界面：只有 4 块 + 2 条线 ──────────────────────────────
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ① 顶部一行灰字（正常全灰，**异常才上色**）
        self.top_box = QWidget()
        self.top_box.setFixedHeight(self._px(H_TOP))
        tl = QHBoxLayout(self.top_box)
        tl.setContentsMargins(self._px(PAD_X), 0, self._px(PAD_X), 0)
        self.lb_top = QLabel("")
        self.lb_top.setFont(self._font(F_TOP))
        self.lb_top.setTextFormat(Qt.RichText)
        self.lb_top.setStyleSheet("color:%s; background:transparent;" % self.C["sub"])
        tl.addWidget(self.lb_top)
        root.addWidget(self.top_box)
        root.addWidget(self._hline())

        # ② 听到（40 px 常规，最多 2 行）
        self.heard_box = QWidget()
        self.heard_box.setFixedHeight(self._px(H_HEARD))
        hl = QVBoxLayout(self.heard_box)
        hl.setContentsMargins(self._px(PAD_X), self._px(10), self._px(PAD_X), 0)
        hl.setSpacing(self._px(GAP_TAG))
        self.lb_tag_heard = self._label(F_TAG, self.C["sub"])
        self.lb_heard = self._label(F_HEARD, self.C["body"], wrap=True)
        self.lb_heard.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        hl.addWidget(self.lb_tag_heard)
        hl.addWidget(self.lb_heard, 1)
        root.addWidget(self.heard_box)
        root.addWidget(self._hline())

        # ③ 回应（48 px 中黑，视觉主角；高度弹性吃满剩余）
        self.reply_box = QWidget()
        self.reply_box.setMinimumHeight(self._px(H_REPLY_MIN))
        rv = QVBoxLayout(self.reply_box)
        rv.setContentsMargins(self._px(PAD_X), self._px(10), self._px(PAD_X), 0)
        rv.setSpacing(self._px(GAP_TAG))
        self.lb_tag_reply = self._label(F_TAG, self.C["sub"])
        self.lb_reply = self._label(F_REPLY, self.C["fg"],
                                    weight=QFont.DemiBold, wrap=True)
        self.lb_reply.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        rv.addWidget(self.lb_tag_reply)
        rv.addWidget(self.lb_reply, 1)
        root.addWidget(self.reply_box, 1)

        # ④ 底部状态带 64 px（灰/绿/红底 + 白字；右侧耗时/步数）
        self.band = QFrame()
        self.band.setFixedHeight(self._px(H_BAND))
        self.band.setStyleSheet("background:%s;" % self.C["band_stop"])
        bl = QHBoxLayout(self.band)
        bl.setContentsMargins(self._px(PAD_X), 0, self._px(PAD_X), 0)
        bl.setSpacing(self._px(12))
        self.lb_band = QLabel("")
        self.lb_band.setFont(self._font(F_BAND, QFont.DemiBold))
        self.lb_band.setStyleSheet("color:#FFFFFF; background:transparent;")
        bl.addWidget(self.lb_band, 0)
        bl.addStretch(1)
        self.bar_plan = QProgressBar()
        self.bar_plan.setTextVisible(False)
        self.bar_plan.setFixedHeight(max(5, self._px(6)))
        self.bar_plan.setRange(0, 100)
        self.bar_plan.setValue(0)
        self.bar_plan.setFixedWidth(self._px(BAR_MIN_W))
        self.bar_plan.setStyleSheet(
            "QProgressBar{background:rgba(255,255,255,0.28);border:none;"
            "border-radius:%dpx;}"
            "QProgressBar::chunk{background:rgba(255,255,255,0.95);border-radius:%dpx;}"
            % (self._px(3), self._px(3)))
        self.lb_step = QLabel("")
        self.lb_step.setFont(self._font(F_META))
        self.lb_step.setStyleSheet("color:rgba(255,255,255,0.90); background:transparent;")
        self.lb_meta = QLabel("")
        self.lb_meta.setFont(self._font(F_META))
        self.lb_meta.setStyleSheet("color:rgba(255,255,255,0.90); background:transparent;")
        bl.addWidget(self.bar_plan, 0)
        bl.addWidget(self.lb_step, 0)
        bl.addSpacing(self._px(6))
        bl.addWidget(self.lb_meta, 0)
        self.bar_plan.hide()
        self.lb_step.hide()
        root.addWidget(self.band)

        self._set_top(None)
        self._set_band("等待指令", "band_stop")
        self._build_estop_overlay()

    def _build_estop_overlay(self):
        """急停整屏红边：用一个**置顶透明边框帧**画，而不是 paintEvent。

        🔴 为什么不能用 paintEvent（2026-10-29 实测踩到）：
           `setStyleSheet("background:#FFF")` 的样式会**级联到子控件**，
           子块把父窗口的整片区域重新刷成白底 → paintEvent 画的边框被盖掉，
           离屏渲染和真机上都会"有状态但看不到红边"。
           置顶的 QFrame 边框是最后一个子控件，永远画在最上层。
        """
        self._edge = QFrame(self)
        self._edge.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._edge.hide()

    def _sync_edge(self, locked):
        if not hasattr(self, "_edge"):
            return
        if not locked:
            self._edge.hide()
            return
        w = max(4, self._px(6))
        self._edge.setStyleSheet(
            "background:transparent; border:%dpx solid %s;"
            % (w, self.C["band_danger"]))
        self._edge.setGeometry(0, 0, self.width(), self.height())
        self._edge.show()
        self._edge.raise_()

    # ── 防息屏：板子上的省电守护进程会偷偷把 DPMS 打开 ─────────────
    def _keep_awake(self):
        """每 20 s 重申一次：点亮屏幕 + 空闲超时清零 + 关屏保 + 禁 DPMS。

        为什么不能只在 `run_display.sh` 里设一次：`xset -dpms` 是一次性设置，
        LXQt 会同时拉起 `lxqt-powermanagement` / `xfce4-power-manager` /
        `xscreensaver`，它们启动/改配置时会把 DPMS 再打开（2026-11-03 真机）。

        ⚠️ 顺序有讲究（实测）：`xset dpms force on` **会把 DPMS 重新打开**，
        所以必须"先唤醒 → 超时清零 → 最后禁 DPMS"，反过来写会留下
        `Standby/Suspend/Off = 600` 又变成 10 分钟黑屏。
        """
        _xset("dpms", "force", "on")               # 1) 万一已经黑了，先点亮
        _xset("dpms", "0", "0", "0")               # 2) 空闲超时清零（0 = 永不黑屏）
        _xset("s", "off", "-dpms", "s", "noblank")  # 3) 关屏保 + 最后禁掉 DPMS

    def _wake(self):
        """有新指令 / 新事件 → 立刻点亮屏幕（电脑端下命令不该看不见反应）。

        电脑端发指令走 HTTP，**不是 X 输入事件**，X 的空闲计时不会归零，
        屏保也就不会自己退 —— 所以这里主动 `xset dpms force on`。
        """
        now = time.time()
        if now - self._last_wake < 2.0:      # 一次指令会连着来好几个事件，别刷屏
            return
        self._last_wake = now
        self._keep_awake()                   # 点亮 + 顺带把空闲超时清零、禁 DPMS

    def resizeEvent(self, ev):
        super(Panel, self).resizeEvent(ev)
        if hasattr(self, "_edge") and self._edge.isVisible():
            self._edge.setGeometry(0, 0, self.width(), self.height())

    # ── 键盘：只认"退出看桌面"这一个动作（展示屏不接受任何操作）────
    def keyPressEvent(self, ev):
        """Esc / F12 = 退出展示屏（回到桌面），退出码 0。

        板子开机自动进全屏展示屏（方案 §十五），要看桌面就按 Esc；
        `display_supervisor.sh` 见到退出码 0 就不再拉起。
        """
        if ev.key() in (Qt.Key_Escape, Qt.Key_F12):
            self._user_quit = True
            app = QApplication.instance()
            if app:
                app.quit()
            return
        super(Panel, self).keyPressEvent(ev)

    # ── 文本截断：超长加省略号，绝不缩字号 ───────────────────────
    def _fit(self, text, px, max_lines):
        text = " ".join(str(text or "").split())
        if not text:
            return ""
        avail = self.width() - 2 * self._px(PAD_X)
        if avail <= 0:
            avail = self.screen_size[0] - 2 * self._px(PAD_X)
        fm = QFontMetrics(self._font(px))
        unit = max(1, fm.horizontalAdvance("测"))
        per = max(1, int(avail / unit))
        cap = min(per * max_lines, CAP.get(px, (per, max_lines))[0] * max_lines)
        if len(text) > cap:
            text = text[:max(1, cap - 1)] + "…"
        return text

    # ── 顶部一行灰字 ────────────────────────────────────────────
    def _set_top(self, st):
        """一行里只有**异常**才上色：未连接 / 电量低 / 电脑离线 / AI 不可用。"""
        warn = self.C["warn"]

        def sp(txt, bad=False):
            return ('<span style="color:%s">%s</span>' % (warn, txt)) if bad else txt

        if not st:
            self.lb_top.setText(sp("● 正在连接主控…", True).replace(
                "color:%s" % warn, "color:%s" % self.C["dim"]))
            return
        conn = bool(st.get("connected"))
        pct = _batt_pct(st.get("voltage"))
        online = bool(st.get("console_online"))
        ai_ok = st.get("llm_state") == "ready"

        parts = [
            sp("● 车已连接" if conn else "● 车未连接", not conn),
            sp(("%d%%" % pct) if pct is not None else "电量 —",
               pct is not None and pct <= 20),
            sp("电脑在线" if online else "电脑离线", not online),
        ]
        if not ai_ok:
            parts.append(sp("AI 不可用", True))
        self.lb_top.setText("   ·   ".join(parts))

    # ── 底部状态带 ──────────────────────────────────────────────
    def _set_band(self, label, ckey, meta="", step=None, total=None):
        """状态带。**只有多步计划时才显示进度条与 k/n**；其余一律隐藏。"""
        self.band.setStyleSheet("background:%s;" % self.C.get(ckey, self.C["band_stop"]))
        self.lb_band.setText(label)
        self.lb_meta.setText(meta)
        if total:
            k = int(step or 0)
            self.bar_plan.setValue(int(100.0 * k / max(1, int(total))))
            self.bar_plan.show()
            self.lb_step.setText("%d/%d" % (k, int(total)))
            self.lb_step.show()
        else:
            self.bar_plan.hide()
            self.lb_step.hide()

    # ── 四态判定 ────────────────────────────────────────────────
    def _demo(self):
        return bool(self.st.get("console_online"))

    def _standby(self):
        """离线 >60 s 才回待机（刷新页面/切网不闪）。"""
        if self.online_since is None:
            return True
        if self._demo():
            return False
        return (time.time() - self.online_since) > 60

    def paintEvent(self, ev):
        """只负责底图；急停红边交给置顶 `_edge` 帧（见 `_build_estop_overlay`）。"""
        super(Panel, self).paintEvent(ev)

    # ── 数据 ────────────────────────────────────────────────────
    def apply_packet(self, pkt):
        if pkt.get("ok"):
            self.last_ok = time.time()
            self.fails = 0
            self.st = pkt.get("status") or {}
            if self._demo() and self.online_since is None:
                self.online_since = time.time()
            # 动作/急停变了 → 可能就是电脑端刚下的指令（HTTP 不算 X 输入），
            # 主动点亮屏幕，别让人对着黑屏等反应
            act = (self.st.get("action"), bool(self.st.get("estop_locked")))
            if act != self._last_action:
                self._last_action = act
                self._wake()
            for e in (pkt.get("events") or []):
                self.apply_event(e)
        else:
            self.fails = int(pkt.get("fails") or self.fails + 1)
        self.refresh()

    def apply_event(self, e):
        t = e.get("type")
        turn = e.get("turn")
        if turn is None:
            return
        d = self.turns.setdefault(turn, {})
        if t == "heard":
            d["heard"] = e
            self.last_turn = turn
        elif t in ("reply", "exec", "reject", "step"):
            d[t] = e
        if len(self.turns) > 6:                 # 防内存增长
            for k in sorted(self.turns)[:-6]:
                self.turns.pop(k, None)
        self._wake()                            # 有新话 → 屏幕立刻亮（防息屏兜底）

    # ── 刷新 ────────────────────────────────────────────────────
    def refresh(self):
        st = self.st or {}
        locked = bool(st.get("estop_locked"))
        conn = bool(st.get("connected"))
        disconnected = self.fails >= 5 and (time.time() - self.last_ok) > 3.0
        standby = self._standby()

        self._set_top(st)

        d = self.turns.get(self.last_turn) or {}
        heard = (d.get("heard") or {}).get("text") or ""
        reply_ev = d.get("reply") or d.get("exec") or d.get("reject") or {}
        reply = (reply_ev.get("text") or reply_ev.get("label")
                 or reply_ev.get("reason") or "")

        # ② 听到
        self.lb_tag_heard.setText("听到")
        self.lb_heard.setText(self._fit(heard or "等待指令…", F_HEARD, 2))
        fade_heard = standby or disconnected or not heard or not conn
        self.lb_heard.setStyleSheet("color:%s; background:transparent;"
                                    % (self.C["dim"] if fade_heard else self.C["body"]))

        # ③ 回应（急停时也用**常规色**：红只给边框和状态带，避免三处红）
        self.lb_tag_reply.setText("回应")
        if locked:
            self.lb_reply.setText(self._fit("已急停，请在电脑端解除", F_REPLY, 1))
        else:
            self.lb_reply.setText(self._fit(reply or "—", F_REPLY, 1))
        self.lb_reply.setStyleSheet(
            "color:%s; background:transparent;"
            % (self.C["dim"] if (standby or disconnected or not conn) else self.C["fg"]))

        # ④ 状态带：label + 右侧 meta（耗时 / 速度 / 步数）
        secs = self._timing_secs(d)
        meta = ("%.1fs" % secs) if secs is not None else ""
        if locked:
            self._set_band("已锁定", "band_danger", meta)
        elif disconnected:
            self._set_band("与主控断开", "band_stop", "重连中")
        elif not conn:
            # 小车没连上时主控的 action 还停在默认值（"前进"）——不能当真，
            # 否则屏上是「车未连接 + 绿色 前进 0.3 m/s」自相矛盾（2026-11-03 真机）
            self._set_band("等待小车连接", "band_stop", _board_ip())
        elif standby:
            self._set_band("等待控制台连接", "band_stop", _board_ip())
        else:
            act = st.get("action") or "stop"
            label, ckey = ACTION_STYLE.get(act, ("待机", "band_stop"))
            moving = act in ("forward", "backward", "turn_left", "turn_right")
            plan = bool(st.get("plan_running") and st.get("plan_total"))
            if plan:
                # 多步：进度 + k/n 都在状态带里，右侧仍留耗时
                self._set_band(label, ckey, meta,
                               step=st.get("plan_step"), total=st.get("plan_total"))
            else:
                self._set_band(label, ckey, meta)
            # 停止时**不报速度**（"停止"和"1.2 m/s"会互相打架）
            if not moving and not plan:
                self.lb_meta.setText(meta)
            elif moving and not plan:
                sp = _speed_label(st.get("speed_mmps"), st.get("speed"))
                self.lb_meta.setText("  ·  ".join([x for x in (sp, meta) if x]))

        # ★ 必须主动重绘 + 同步红边（全屏窗口平时没有别的重绘来源）
        self.update()
        self._sync_edge(locked)

    def _timing_secs(self, d):
        """heard.ts → 第一条结果事件 ts（方案 §4.5 的"响应"指标）。"""
        h = (d.get("heard") or {}).get("ts")
        if not h:
            return None
        for key in ("reply", "exec", "reject", "step"):
            e = d.get(key)
            if e and e.get("ts"):
                dt = float(e["ts"]) - float(h)
                if 0 <= dt < 120:
                    return dt
        return None

    # ── 预览/截图用假数据 ────────────────────────────────────────
    def _feed_preview(self):
        """`preview_state`: plan / stop / estop / standby / disconnect。"""
        t = time.time()
        st = {
            "connected": True, "voltage": 15.9, "llm_state": "ready",
            "console_online": True, "action": "forward", "action_label": "前进",
            "speed": 30, "speed_mmps": 1158, "plan_running": True,
            "plan_step": 1, "plan_total": 2, "plan_label": "前进",
            "estop_locked": False,
        }
        heard, reply = "前进一米，然后左转三十度", "好的，先前进，再左转"
        mode = (self.preview_state or "plan").lower()
        if mode == "stop":
            st.update({"action": "stop", "plan_running": False,
                       "speed_mmps": 300, "speed": 25})
            heard, reply = "明天吃什么", "我只看得到眼前的路"
        elif mode == "estop":
            st.update({"action": "stop", "plan_running": False,
                       "estop_locked": True})
            heard, reply = "前进一米", "好的"
        elif mode == "standby":
            st.update({"action": "stop", "plan_running": False,
                       "console_online": False})
            self.online_since = t - 120
        elif mode == "disconnect":
            st.update({"action": "stop", "plan_running": False})
            self.fails = 6
            self.last_ok = t - 10
        self.st = st
        if self.online_since is None:
            self.online_since = t
        self.apply_event({"type": "heard", "turn": 1, "seq": 1, "ts": t - 2.4,
                          "text": heard, "src": "voice"})
        self.apply_event({"type": "reply", "turn": 1, "seq": 2, "ts": t,
                          "text": reply})
        self.refresh()


# ══════════════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════════════
def main(argv=None):
    ap = argparse.ArgumentParser(description="开发板展示屏（只读大字界面）")
    ap.add_argument("--base", default=os.environ.get("CAR_WEB_BASE",
                                                    "http://127.0.0.1:8090"))
    ap.add_argument("--windowed", action="store_true", help="窗口模式（调试版式）")
    ap.add_argument("--preview", action="store_true", help="假数据预览（不连主控）")
    ap.add_argument("--screenshot", default="", help="渲染 PNG 后退出（无需显示器）")
    ap.add_argument("--size", default="", help="截图尺寸，如 1024x600")
    ap.add_argument("--theme", default=os.environ.get("CAR_DISPLAY_THEME", "light"),
                    choices=["light", "dark"], help="浅色/暗色主题")
    ap.add_argument("--scale", default=None, type=float, help="强制缩放系数")
    ap.add_argument("--preview-state", default="plan",
                    choices=["plan", "stop", "estop", "standby", "disconnect"],
                    help="预览/截图时展示哪种状态")
    a = ap.parse_args(argv)

    shot = bool(a.screenshot)
    if shot and not os.environ.get("DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    app = QApplication(sys.argv[:1])
    app.setApplicationName("car-display")

    if shot:
        w, h = 1024, 600
        if a.size:
            try:
                w, h = [int(x) for x in a.size.lower().split("x")]
            except Exception:
                print("[warn] --size 格式不对，已按 1024x600 处理")
        size = (w, h)
    else:
        scr = QGuiApplication.primaryScreen()
        size = (scr.size().width(), scr.size().height()) if scr else (1024, 600)

    win = Panel(screen_size=size, theme=a.theme, preview=a.preview or shot,
                scale=a.scale, preview_state=a.preview_state)
    win.setWindowTitle("小车展示屏")
    print("屏幕 %dx%d → 缩放系数 %.2f（基准 1024x600）" % (size[0], size[1], win.k))

    fetcher = None
    if not (a.preview or shot):
        fetcher = Fetcher(a.base)
        fetcher.tick.connect(win.apply_packet)
        fetcher.start()
        timer = QTimer(win)
        timer.timeout.connect(win.refresh)
        timer.start(1000)
        # 防息屏：一上来先设一次，之后每 20 s 重申（省电守护进程会把它改回去）
        win._keep_awake()
        ka = QTimer(win)
        ka.timeout.connect(win._keep_awake)
        ka.start(20000)

    if not win.font_ok:
        print("[错误] 没找到中文字体（试过：%s）" % "、".join(CJK_FONTS))
        print("       请安装： sudo apt install -y fonts-noto-cjk")

    if shot:
        win.resize(size[0], size[1])
        win.show()

        def _grab():
            win.refresh()
            pix = win.grab()
            pix.save(a.screenshot)
            print("已保存截图：%s（%dx%d，字体=%s，缩放=%.2f）"
                  % (a.screenshot, pix.width(), pix.height(), win.family, win.k))
            app.quit()

        QTimer.singleShot(500, _grab)
        return app.exec()

    if a.windowed:
        win.resize(size[0], size[1])
        win.show()
    else:
        win.showFullScreen()
        win.activateWindow()
        win.raise_()
        win.setFocus()
        print("全屏展示中；按 Esc 退出回桌面（看护进程会当成「用户主动退出」，不再拉起）")
    rc = app.exec()
    if fetcher:
        fetcher.stop()
        fetcher.wait(1500)
    if win._user_quit:
        print("用户按 Esc 退出（退出码 0）")
        return 0
    return rc


if __name__ == "__main__":
    sys.exit(main())