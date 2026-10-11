# -*- coding: utf-8 -*-
"""
car_gui_tk.py —— 智能小车手动控制台（Tkinter 备用版，零额外依赖）

什么时候用这个
--------------
PySide6 在开发板上装不上、或 Qt 报 `could not load the Qt platform plugin "xcb"`
之类的错时，用这个版本顶上。功能与 PySide6 版基本一致（少了样式美化）。

前提：板子上装了 python3-tk
    sudo apt install -y python3-tk

运行：
    python3 car_gui_tk.py
    python3 car_gui_tk.py --port /dev/ttyCAR

快捷键：W/A/S/D 或方向键移动，Q/E 平移，空格急停，[ ] 调速。
"""

import argparse
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from car_gui_core import (
    APP_NAME, APP_VERSION, DEFAULT_BAUD, DEFAULT_SPEED,
    SPEED_MIN, SPEED_MAX, SPEED_STEP, COMMON_COMMANDS,
    CarController, guess_default_port, list_serial_ports,
)

# 语音识别（可选）
HAS_ASR_MODULE = False
ASR_IMPORT_ERROR = ""
try:
    from asr_engine import (
        AsrEngine, dependency_status, guess_input_device, guess_model_dir,
        list_input_devices,
    )
    HAS_ASR_MODULE = True
except Exception as _e:                       # pragma: no cover
    ASR_IMPORT_ERROR = str(_e)

SPEED_PRESETS = (100, 200, 300, 400, 500)

C_BG = "#f5f6f8"
C_PANEL = "#ffffff"
C_MUTED = "#5f5e5a"
C_BLUE = "#185fa5"
C_GREEN = "#3b6d11"
C_AMBER = "#854f0b"
C_RED = "#a32d2d"
C_DANGER = "#e24b4a"

KEY_MAP = {
    "w": "forward", "up": "forward",
    "s": "backward", "down": "backward",
    "a": "turn_left", "left": "turn_left",
    "d": "turn_right", "right": "turn_right",
    "q": "strafe_left",
    "e": "strafe_right",
}


class App(object):

    def __init__(self, root, port=None, baud=DEFAULT_BAUD, model_dir=None,
                 auto_tts=True, demo=False, auto_llm=True, llm_mode="auto"):
        self.root = root
        self.ctrl = CarController()
        self.baud = baud
        self.hold = set()
        self._kws_warned = False        # 麦克风断链只提示一次，别刷屏
        self._title0 = ""               # 原始窗口标题（急停锁定时会加前缀）
        self._chat_only = False         # 「纯聊天」模式：只回话、不执行指令
        self._chat_shown = -1           # 对话记录已渲染条数

        # 语音识别状态
        self.asr = None
        self._results = queue.Queue()
        self._auto_listen = False
        self._ptt_ok = False
        self._mic_yielded = False      # 录音前 KWS 是否让位（录完要还回去）
        #: 自动聆听：主线程正在执行指令/播报（聆听循环要等它，见 `_auto_loop`）
        self._auto_exec = threading.Event()
        #: 自动聆听失败退避（秒）—— 绝不紧密重试，否则 PortAudio 会断言 abort
        self._auto_backoff = 0.0
        self._auto_last_err = None
        if HAS_ASR_MODULE:
            self.asr = AsrEngine(log=self.ctrl.log)

        root.title("%s  v%s（Tkinter 版）" % (APP_NAME, APP_VERSION))
        root.configure(bg=C_BG)
        root.geometry("1060x820")
        root.minsize(940, 700)

        self._build(port)
        self._bind_keys()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._title0 = self.root.title()

        if model_dir:
            self.ent_model.delete(0, "end")
            self.ent_model.insert(0, model_dir)
        self.refresh_voice_devices()

        # 离线演示模式（--demo）：没接小车也能语音交互
        if demo:
            self.var_demo.set(True)
            self.on_toggle_demo()

        # LLM（指令纠错 + 对话）：云端只查配置、本地不加载权重，同步做即可。
        # mode="auto"：配了云端 key 且网络通 → 云端（2.7~4.5s）；否则回退本地（16~35s）。
        if auto_llm:
            ok, msg = self.ctrl.init_llm(mode=llm_mode)
            if not ok:
                self.ctrl.log("LLM 未启用：%s（规则解析与播报不受影响）" % msg)

        # 语音播报（TTS）：**后台**预加载（要 5~7 秒，同步会冻住界面）
        if auto_tts:
            ok, msg = self.ctrl.init_tts(background=True)
            self.ctrl.log("TTS：%s" % msg)
        else:
            self.ctrl.set_speak_enabled(False)
            self.ctrl.log("TTS：已按 --no-tts 参数关闭播报")

        self.ctrl.log("就绪。选择串口后点击「连接」。")
        if not HAS_ASR_MODULE:
            self.ctrl.log("语音模块导入失败：%s" % ASR_IMPORT_ERROR)
        self.root.after(100, self.tick)

    # ══════════════════════════════════════════════════════
    def _build(self, port):
        pad = {"padx": 10, "pady": 8}

        # ── 顶部状态条 ─────────────────────────────────────
        top = tk.Frame(self.root, bg=C_PANEL, highlightthickness=1,
                       highlightbackground="#dcdfe6")
        top.pack(fill="x", **pad)

        self.led = tk.Label(top, text="  ", bg=C_MUTED, width=2)
        self.led.pack(side="left", padx=(10, 6), pady=8)

        self.lab_conn = tk.Label(top, text="未连接", bg=C_PANEL,
                                 font=("", 10, "bold"))
        self.lab_conn.pack(side="left", padx=(0, 14))

        self.lab_action = tk.Label(top, text="动作：—", bg=C_PANEL)
        self.lab_action.pack(side="left", padx=8)
        self.lab_speed = tk.Label(top, text="目标速度：%d mm/s" % DEFAULT_SPEED,
                                  bg=C_PANEL)
        self.lab_speed.pack(side="left", padx=8)
        self.lab_volt = tk.Label(top, text="电压：—", bg=C_PANEL)
        self.lab_volt.pack(side="left", padx=8)
        self.lab_flag = tk.Label(top, text="急停标志：—", bg=C_PANEL)
        self.lab_flag.pack(side="left", padx=8)

        tk.Label(top, text="按住方向键行驶，松开即停 ｜ 空格 = 急停",
                 bg=C_PANEL, fg=C_MUTED, font=("", 9)).pack(side="right", padx=10)

        # ── 主体三栏 ───────────────────────────────────────
        body = tk.Frame(self.root, bg=C_BG)
        body.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self._body_frame = body      # 急停条要插在它上面（pack before=）

        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        self._build_conn(body, port)
        self._build_control(body)
        self._build_status(body)

        # ── 急停锁定警示条（平时隐藏）───────────────────────
        self._build_estop_bar()

        # ── 语音输入 ───────────────────────────────────────
        self._build_voice()

        # ── 语音播报 / 语音急停 ────────────────────────────
        self._build_speech()

        # ── 底部指令栏 ─────────────────────────────────────
        bottom = tk.Frame(self.root, bg=C_PANEL, highlightthickness=1,
                          highlightbackground="#dcdfe6")
        bottom.pack(fill="x", padx=10, pady=(0, 10))
        tk.Label(bottom, text="指令：", bg=C_PANEL).pack(side="left", padx=(10, 4), pady=8)
        # 「常用指令」下拉：输入法没配好时的兜底，也是演示时的快捷方式
        self.cmb_cmd = ttk.Combobox(
            bottom, width=12, state="readonly",
            values=("常用指令 ▾",) + tuple(COMMON_COMMANDS))
        self.cmb_cmd.set("常用指令 ▾")
        self.cmb_cmd.pack(side="left", padx=(0, 4), pady=8)
        self.cmb_cmd.bind("<<ComboboxSelected>>", self.on_pick_cmd)
        self.ent_cmd = tk.Entry(bottom)
        self.ent_cmd.pack(side="left", fill="x", expand=True, pady=8)
        self.ent_cmd.bind("<Return>", lambda e: self.on_send_cmd())
        tk.Button(bottom, text="发送", command=self.on_send_cmd,
                  width=8).pack(side="left", padx=6)

    # ── 左：串口 ───────────────────────────────────────────
    # ⚠️ `port` 必须当参数传进来：它原本是 `_build()` 的局部变量，
    #    在这里直接引用会 NameError —— **本 Tk 备用界面之前根本起不来**。
    #    （离线回归只覆盖 PySide 版，所以一直没被发现。）
    def _build_conn(self, parent, port=None):
        g = tk.LabelFrame(parent, text=" 串口连接 ", bg=C_PANEL, fg=C_MUTED,
                          padx=10, pady=8, width=220)
        g.grid(row=0, column=0, sticky="ns", padx=(0, 10))
        g.grid_propagate(False)

        tk.Label(g, text="设备", bg=C_PANEL).pack(anchor="w")
        self.cmb_port = ttk.Combobox(g, width=22)
        self.cmb_port.pack(fill="x", pady=(2, 6))
        self.refresh_ports(prefer=port)

        tk.Button(g, text="刷新设备列表", command=lambda: self.refresh_ports()
                  ).pack(fill="x", pady=2)

        tk.Label(g, text="波特率", bg=C_PANEL).pack(anchor="w", pady=(10, 0))
        self.cmb_baud = ttk.Combobox(g, width=22, state="readonly",
                                     values=["9600", "19200", "38400", "57600",
                                             "115200", "230400", "460800"])
        self.cmb_baud.set(str(DEFAULT_BAUD))
        self.cmb_baud.pack(fill="x", pady=(2, 6))

        self.btn_conn = tk.Button(g, text="连接", command=self.on_connect)
        self.btn_conn.pack(fill="x", pady=2)
        self.btn_disc = tk.Button(g, text="断开", command=self.on_disconnect,
                                  state="disabled")
        self.btn_disc.pack(fill="x", pady=2)

        # ── 离线演示模式（2026-09-27）──────────────────────────────
        # 没接小车时也要能用：指令照常解析 + 语音应答，但绝不发串口。
        tk.Frame(g, bg="#d9d9d9", height=1).pack(fill="x", pady=(10, 6))
        self.var_demo = tk.BooleanVar(value=False)
        tk.Checkbutton(g, text="离线演示模式", variable=self.var_demo,
                       command=self.on_toggle_demo, bg=C_PANEL,
                       activebackground=C_PANEL, selectcolor=C_PANEL,
                       anchor="w").pack(fill="x")
        tk.Label(g, text="没接小车也能语音对话\n（不发串口，车不会动）",
                 bg=C_PANEL, fg=C_MUTED, font=("", 8), justify="left"
                 ).pack(anchor="w")

        tk.Label(g, text="提示：需将用户加入 dialout 组\n"
                         "sudo usermod -aG dialout $USER\n"
                         "（改完要重新登录）",
                 bg=C_PANEL, fg=C_MUTED, font=("", 8), justify="left"
                 ).pack(anchor="w", pady=(12, 0))

    # ── 中：控制 ───────────────────────────────────────────
    def _build_control(self, parent):
        g = tk.LabelFrame(parent, text=" 手动控制 ", bg=C_PANEL, fg=C_MUTED,
                          padx=12, pady=10)
        g.grid(row=0, column=1, sticky="nsew")

        pad = tk.Frame(g, bg=C_PANEL)
        pad.pack()

        def dirbtn(text, action):
            b = tk.Button(pad, text=text, width=16, height=2,
                          font=("", 11, "bold"))
            b.bind("<ButtonPress-1>", lambda e, a=action: self.on_dir_press(a))
            b.bind("<ButtonRelease-1>", lambda e, a=action: self.on_dir_release(a))
            return b

        self.btn_fwd = dirbtn("↑  前进  (W)", "forward")
        self.btn_left = dirbtn("←  左转  (A)", "turn_left")
        self.btn_right = dirbtn("→  右转  (D)", "turn_right")
        self.btn_back = dirbtn("↓  后退  (S)", "backward")
        self.btn_sl = dirbtn("←  左移  (Q)", "strafe_left")
        self.btn_sr = dirbtn("→  右移  (E)", "strafe_right")

        self.btn_stop = tk.Button(pad, text="■  停  止   （空格）", width=16, height=2,
                                  font=("", 12, "bold"), bg=C_DANGER, fg="white",
                                  activebackground="#b83e3d",
                                  command=self.on_stop)
        self.btn_stop.grid(row=1, column=1, padx=6, pady=6)
        self.btn_fwd.grid(row=0, column=1, padx=6, pady=6)
        self.btn_left.grid(row=1, column=0, padx=6, pady=6)
        self.btn_right.grid(row=1, column=2, padx=6, pady=6)
        self.btn_back.grid(row=2, column=1, padx=6, pady=6)
        self.btn_sl.grid(row=3, column=0, padx=6, pady=6)
        self.btn_sr.grid(row=3, column=2, padx=6, pady=6)

        self.var_latch = tk.BooleanVar(value=False)
        tk.Checkbutton(g, text="锁定持续运动（松开方向按钮后不停，再点一次停止）",
                       variable=self.var_latch, bg=C_PANEL,
                       activebackground=C_PANEL, anchor="w"
                       ).pack(fill="x", pady=(12, 4))

        sp = tk.LabelFrame(g, text=" 速度 ", bg=C_PANEL, fg=C_MUTED, padx=10, pady=8)
        sp.pack(fill="x", pady=(8, 0))

        row = tk.Frame(sp, bg=C_PANEL)
        row.pack(fill="x")
        tk.Label(row, text="线速度 / 角速度幅值", bg=C_PANEL).pack(side="left")
        self.lab_spd = tk.Label(row, text="%d mm/s" % DEFAULT_SPEED, bg=C_PANEL,
                                font=("", 10, "bold"))
        self.lab_spd.pack(side="right")

        self.scale = tk.Scale(sp, from_=SPEED_MIN, to=SPEED_MAX,
                              orient="horizontal", resolution=SPEED_STEP,
                              showvalue=False, bg=C_PANEL, highlightthickness=0,
                              command=self.on_slider)
        self.scale.set(DEFAULT_SPEED)
        self.scale.pack(fill="x", pady=4)

        prow = tk.Frame(sp, bg=C_PANEL)
        prow.pack(fill="x")
        for v in SPEED_PRESETS:
            tk.Button(prow, text=str(v), width=4,
                      command=lambda x=v: self.set_speed(x)).pack(side="left", padx=2)
        tk.Button(prow, text="−", width=3,
                  command=lambda: self.set_speed(self.ctrl.speed - SPEED_STEP)
                  ).pack(side="left", padx=2)
        tk.Button(prow, text="＋", width=3,
                  command=lambda: self.set_speed(self.ctrl.speed + SPEED_STEP)
                  ).pack(side="left", padx=2)

        self._enable_controls(False)

    # ── 右：状态 ───────────────────────────────────────────
    def _build_status(self, parent):
        right = tk.Frame(parent, bg=C_BG, width=290)
        right.grid(row=0, column=2, sticky="ns", padx=(10, 0))
        right.grid_propagate(False)

        g = tk.LabelFrame(right, text=" 实时状态 ", bg=C_PANEL, fg=C_MUTED,
                          padx=10, pady=8)
        g.pack(fill="x")

        self.vals = {}
        items = [
            ("Vx", "X 轴速度", "mm/s"), ("Vy", "Y 轴速度", "mm/s"),
            ("Vz", "Z 轴速度", "mm/s"), ("volt", "电池电压", "V"),
            ("ax", "加速度 X", "m/s²"), ("ay", "加速度 Y", "m/s²"),
            ("az", "加速度 Z", "m/s²"), ("gz", "角速度 Z", "rad/s"),
        ]
        for i, (key, title, unit) in enumerate(items):
            r, c = divmod(i, 2)
            cell = tk.Frame(g, bg=C_PANEL)
            cell.grid(row=r, column=c, sticky="w", padx=6, pady=4)
            tk.Label(cell, text=title, bg=C_PANEL, fg=C_MUTED,
                     font=("", 8)).pack(anchor="w")
            lab = tk.Label(cell, text="0", bg=C_PANEL, font=("", 12, "bold"))
            lab.pack(anchor="w")
            tk.Label(cell, text=unit, bg=C_PANEL, fg=C_MUTED,
                     font=("", 8)).pack(anchor="w")
            self.vals[key] = lab

        g2 = tk.LabelFrame(right, text=" 运行日志 ", bg=C_PANEL, fg=C_MUTED)
        g2.pack(fill="both", expand=True, pady=(10, 0))
        self.txt_log = tk.Text(g2, height=12, wrap="none", font=("Consolas", 9),
                               bg="#fbfbfc")
        sb = ttk.Scrollbar(g2, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt_log.pack(fill="both", expand=True, padx=4, pady=4)

        tk.Button(right, text="清空日志",
                  command=lambda: self.txt_log.delete("1.0", "end")
                  ).pack(fill="x", pady=(6, 0))

    # ── 急停锁定警示条 ─────────────────────────────────────
    def _build_estop_bar(self):
        """
        急停锁定时的醒目警示条（**初始不 pack**，锁定时才出现）。

        为什么要单独一条：
        语音（或手动）急停后车会进入**锁定态**，之后所有运动指令都被拒绝。
        界面上必须同时给出**原因**和**解除入口**，否则操作者会以为程序坏了。
        """
        self.bar_estop = tk.Frame(self.root, bg="#fdecec", highlightthickness=2,
                                  highlightbackground=C_RED)

        self.lab_estop_lock = tk.Label(
            self.bar_estop, text="🔒 急停锁定中", bg="#fdecec", fg=C_RED,
            font=("", 12, "bold"))
        self.lab_estop_lock.pack(side="left", padx=12, pady=8)

        tk.Button(self.bar_estop, text="解除急停锁定", command=self.on_release_estop,
                  bg=C_RED, fg="white", font=("", 10, "bold"),
                  padx=14, pady=2).pack(side="right", padx=12, pady=8)

    def _show_estop_bar(self, show):
        """
        按需显示/隐藏急停条。

        ⚠️ Tk 没有 `setVisible`。而且 `pack_forget()` 之后再 `pack()` 会跑到
        末尾 —— 所以**重新 pack 时必须显式指定 `before=self._body_frame`**，
        否则这条警示会跑到窗口底部去。
        """
        try:
            mapped = bool(self.bar_estop.winfo_ismapped())
        except Exception:
            mapped = False
        if show and not mapped:
            self.bar_estop.pack(fill="x", padx=10, pady=(0, 8),
                                before=self._body_frame)
        elif not show and mapped:
            self.bar_estop.pack_forget()

    # ── 语音播报 / 语音急停 ─────────────────────────────────
    def _build_speech(self):
        g = tk.LabelFrame(self.root, text=" 语音播报 / 语音急停 ", bg=C_PANEL,
                          fg=C_MUTED, padx=10, pady=6)
        g.pack(fill="x", padx=10, pady=(0, 6))

        r1 = tk.Frame(g, bg=C_PANEL)
        r1.pack(fill="x")
        self.led_tts = tk.Label(r1, text="  ", bg=C_MUTED, width=2)
        self.led_tts.pack(side="left", padx=(0, 6))
        self.lab_tts = tk.Label(r1, text="播报：未启用", bg=C_PANEL, anchor="w")
        self.lab_tts.pack(side="left")

        self.var_speak = tk.BooleanVar(value=True)
        tk.Checkbutton(r1, text="播报", variable=self.var_speak, bg=C_PANEL,
                       command=self.on_toggle_speak).pack(side="right", padx=6)
        self.var_announce = tk.BooleanVar(value=True)
        tk.Checkbutton(r1, text="动作播报", variable=self.var_announce,
                       bg=C_PANEL,
                       command=self.on_toggle_announce).pack(side="right", padx=6)

        r2 = tk.Frame(g, bg=C_PANEL)
        r2.pack(fill="x", pady=(4, 0))
        self.led_kws = tk.Label(r2, text="  ", bg=C_MUTED, width=2)
        self.led_kws.pack(side="left", padx=(0, 6))
        self.lab_kws = tk.Label(r2, text="语音急停：未启用", bg=C_PANEL, anchor="w")
        self.lab_kws.pack(side="left")

        self.var_kws = tk.BooleanVar(value=False)
        self.chk_kws = tk.Checkbutton(r2, text="开启语音急停", variable=self.var_kws,
                                      bg=C_PANEL, command=self.on_toggle_kws)
        self.chk_kws.pack(side="right", padx=6)
        self.bar_mic = ttk.Progressbar(r2, length=100, maximum=100)
        self.bar_mic.pack(side="right", padx=6)
        self.lab_hits = tk.Label(r2, text="命中 0 次", bg=C_PANEL, fg=C_MUTED)
        self.lab_hits.pack(side="right", padx=6)

        r3 = tk.Frame(g, bg=C_PANEL)
        r3.pack(fill="x", pady=(2, 0))
        tk.Label(r3, text="声音输出", bg=C_PANEL, fg=C_MUTED).pack(side="left")
        self.lab_web = tk.Label(r3, text="—", bg=C_PANEL, fg=C_MUTED, anchor="w")
        self.lab_web.pack(side="left", padx=6)

        # ── 第 4 行：本地 LLM（指令纠错 + 对话）─────────────────────
        # 定位：项目重点是**人机交互**。LLM 把"听错 → 没听懂"变成"猜出你想干什么"。
        r4 = tk.Frame(g, bg=C_PANEL)
        r4.pack(fill="x", pady=(6, 0))
        self.led_llm = tk.Label(r4, text="  ", bg=C_MUTED, width=2)
        self.led_llm.pack(side="left")
        self.lab_llm = tk.Label(r4, text="LLM：未启用", bg=C_PANEL,
                                fg=C_MUTED, anchor="w")
        self.lab_llm.pack(side="left", padx=6)
        self.var_llm = tk.BooleanVar(value=True)
        tk.Checkbutton(r4, text="启用", variable=self.var_llm,
                       command=self.on_toggle_llm, bg=C_PANEL,
                       activebackground=C_PANEL, selectcolor=C_PANEL
                       ).pack(side="left", padx=6)
        self.var_chat = tk.BooleanVar(value=False)
        tk.Checkbutton(r4, text="纯聊天", variable=self.var_chat,
                       command=self.on_toggle_chat, bg=C_PANEL,
                       activebackground=C_PANEL, selectcolor=C_PANEL
                       ).pack(side="left", padx=6)

        # 对话记录
        self.lst_chat = tk.Listbox(g, height=3, font=("", 9),
                                   bg=C_PANEL, fg="black",
                                   highlightthickness=1)
        self.lst_chat.pack(fill="x", pady=(4, 0))

    # ── 语音输入 ───────────────────────────────────────────
    def _build_voice(self):
        g = tk.LabelFrame(self.root, text=" 语音输入 ", bg=C_PANEL, fg=C_MUTED,
                          padx=10, pady=8)
        g.pack(fill="x", padx=10, pady=(0, 8))

        # 第 1 行：模型目录
        r1 = tk.Frame(g, bg=C_PANEL)
        r1.pack(fill="x")
        tk.Label(r1, text="模型目录", bg=C_PANEL).pack(side="left")
        self.ent_model = tk.Entry(r1)
        self.ent_model.pack(side="left", fill="x", expand=True, padx=6)
        if HAS_ASR_MODULE:
            self.ent_model.insert(0, guess_model_dir())
        self.btn_load = tk.Button(r1, text="加载模型", command=self.on_load_model)
        self.btn_load.pack(side="left")
        self.lab_asr = tk.Label(r1, text="未加载", bg=C_PANEL, fg=C_MUTED)
        self.lab_asr.pack(side="left", padx=10)

        # 第 2 行：麦克风 + 选项 + 录音按钮
        r2 = tk.Frame(g, bg=C_PANEL)
        r2.pack(fill="x", pady=(8, 0))
        tk.Label(r2, text="麦克风", bg=C_PANEL).pack(side="left")
        self.cmb_mic = ttk.Combobox(r2, width=34, state="readonly")
        self.cmb_mic.pack(side="left", padx=6)
        tk.Button(r2, text="刷新", width=6,
                  command=self.refresh_voice_devices).pack(side="left")

        self.var_exec = tk.BooleanVar(value=True)
        tk.Checkbutton(r2, text="识别后自动执行", variable=self.var_exec,
                       bg=C_PANEL, activebackground=C_PANEL
                       ).pack(side="left", padx=10)

        self.btn_ptt = tk.Button(r2, text="按住说话（或按住 F2）", width=22,
                                 state="disabled", font=("", 10, "bold"))
        self.btn_ptt.bind("<ButtonPress-1>", lambda e: self.on_ptt_press())
        self.btn_ptt.bind("<ButtonRelease-1>", lambda e: self.on_ptt_release())
        self.btn_ptt.pack(side="left", padx=6)

        self.btn_auto = tk.Button(r2, text="开始聆听", width=10, state="disabled",
                                  command=self.on_auto_toggle)
        self.btn_auto.pack(side="left", padx=6)

        # 第 3 行：电平 + 识别结果
        r3 = tk.Frame(g, bg=C_PANEL)
        r3.pack(fill="x", pady=(8, 0))
        tk.Label(r3, text="输入电平", bg=C_PANEL).pack(side="left")
        self.bar_level = ttk.Progressbar(r3, maximum=100, value=0, length=140)
        self.bar_level.pack(side="left", padx=6)
        self.lab_heard = tk.Label(r3, text="识别结果：—", bg=C_PANEL,
                                  font=("", 10, "bold"), anchor="w")
        self.lab_heard.pack(side="left", fill="x", expand=True, padx=10)

        if not HAS_ASR_MODULE:
            self.btn_load.config(state="disabled")
            self.lab_asr.config(text="语音模块不可用", fg=C_RED)
            self.ent_model.config(state="disabled")

    def _submit(self, fn, cb):
        """后台线程执行 fn()，结果经队列回到界面线程由 cb 处理。"""
        def worker():
            try:
                ok, res = fn()
            except Exception as e:
                ok, res = False, "异常：%s" % e
            self._results.put((cb, ok, res))
        threading.Thread(target=worker, daemon=True).start()

    def refresh_voice_devices(self):
        if not HAS_ASR_MODULE:
            return
        devs = list_input_devices()
        self._mic_map = [d["index"] for d in devs]
        self.cmb_mic["values"] = ["[%d] %s" % (d["index"], d["name"]) for d in devs]
        if not devs:
            self.cmb_mic.set("")
            self.ctrl.log("没有找到录音设备。插好 USB 声卡后点「刷新」。")
            return
        want = guess_input_device()
        if want in self._mic_map:
            self.cmb_mic.current(self._mic_map.index(want))
        else:
            self.cmb_mic.current(0)

    def _current_mic(self):
        i = self.cmb_mic.current()
        m = getattr(self, "_mic_map", [])
        return m[i] if 0 <= i < len(m) else None

    def on_load_model(self):
        if not self.asr:
            messagebox.showwarning("无法加载",
                                   "语音模块不可用：%s" % (ASR_IMPORT_ERROR or "未知原因"),
                                   parent=self.root)
            return
        if self.asr.ready:
            return
        self.btn_load.config(state="disabled")
        self.lab_asr.config(text="加载中…（约 10–30 秒）", fg=C_MUTED)
        self.ctrl.log("开始加载语音模型…")
        md = self.ent_model.get().strip() or None
        self._submit(lambda: self.asr.load(md), self._after_load_model)

    def _after_load_model(self, ok, res):
        self.btn_load.config(state="normal")
        if ok:
            self.lab_asr.config(text="已就绪 · 断句:%s" % self.asr.vad_mode, fg=C_GREEN)
            self.btn_ptt.config(state="normal")
            self.btn_auto.config(state="normal")
        else:
            self.lab_asr.config(text="加载失败", fg=C_RED)
            messagebox.showwarning("模型加载失败", res, parent=self.root)

    def on_ptt_press(self):
        if not self.asr or not self.asr.ready or self._auto_listen:
            return
        # ★ 录音前先让位（与 PySide6 版一致，原因见那边的注释）：
        #   ① 本板 USB 麦克风只有 1 个 subdevice，KWS 常驻监听会占住它；
        #   ② 本板播放(card1)与采集(card0)在同一 xHCI 控制器上，
        #      **播报期间麦克风完全打不开**（实测 -9999），
        #      所以还要等扬声器空闲下来。
        self._mic_yielded = self.ctrl._yield_mic()[0]
        # ★ 2026-10-11：只看"声卡是否在响"，**不等合成**（合成不碰音频设备）
        if not self.ctrl.playback_idle():
            self.btn_ptt.config(text="⏳ 等播报结束…")
            self.root.update_idletasks()
            quiet, why = self.ctrl.yield_speaker(max_wait=3.0)
            if why:
                self.ctrl.log("🔊 录音前等播报：%s" % why)
            if not quiet:
                self.btn_ptt.config(text="按住说话（或按住 F2）")
                self.ctrl.log("录音启动失败：播报还没结束，麦克风被占用"
                              "（本板播放时麦克风打不开）")
                self.ctrl._restore_mic(self._mic_yielded)
                self._mic_yielded = False
                return
        ok, msg = self.asr.ptt_start(device=self._current_mic())
        self._ptt_ok = ok
        if ok:
            self.btn_ptt.config(text="● 录音中…松开结束")
        else:
            self.ctrl.log("录音启动失败：%s" % msg)
            self.ctrl._restore_mic(self._mic_yielded)
            self._mic_yielded = False

    def on_ptt_release(self):
        if not self._ptt_ok or not self.asr:
            return
        self._ptt_ok = False
        self.btn_ptt.config(text="识别中…", state="disabled")
        self._submit(lambda: self.asr.ptt_stop(), self._after_recognize)

    def _after_recognize(self, ok, res):
        self.btn_ptt.config(text="按住说话（或按住 F2）", state="normal")
        if ok:
            self.lab_heard.config(text="识别结果：%s" % res)
            if self.var_exec.get():
                self.ctrl.run_text_command(res)
        else:
            self.lab_heard.config(text="识别结果：—（%s）" % res)

    def on_auto_toggle(self):
        if not self.asr or not self.asr.ready:
            return
        if not self._auto_listen:
            self._auto_listen = True
            self.btn_auto.config(text="停止聆听")
            self.btn_ptt.config(state="disabled")
            self.ctrl.log("自动聆听已开启，直接说话即可（停顿后自动识别）")
            dev = self._current_mic()
            threading.Thread(target=self._auto_loop, args=(dev,), daemon=True).start()
        else:
            self._auto_listen = False
            self.btn_auto.config(text="开始聆听")
            self.btn_ptt.config(state="normal")
            self.ctrl.log("自动聆听已关闭")

    def _auto_loop(self, dev):
        """自动聆听主循环（后台线程）。

        ⚠️ 与 pyside 版保持同样的三条纪律（2026-10-11，见那里的详细说明）：
          ① 等播报/执行结束再开麦（本板播放/采集硬件互斥）；
          ② 失败要**指数退避**（紧密重试会把 PortAudio 打到断言 abort = 闪退）；
          ③ `Recorder.start()` 内部已做 ALSA 空闲预检。
        """
        while self._auto_listen:
            if getattr(self, "_auto_exec", None) is not None \
                    and self._auto_exec.is_set():
                time.sleep(0.03)
                continue
            if not self.ctrl.speak_idle():
                time.sleep(0.05)
                continue
            try:
                ok, res = self.asr.listen_auto(device=dev)
            except Exception as e:
                ok, res = False, "异常：%s" % e
                self._auto_listen = False
            if not self._auto_listen:
                break
            if not ok:
                self._auto_backoff = min(
                    max(getattr(self, "_auto_backoff", 0.0) * 1.8, 0.3), 2.0)
                if ("没听到" not in res) and ("超时" not in res):
                    if res != getattr(self, "_auto_last_err", None):
                        self._auto_last_err = res
                        self.ctrl.log("自动聆听受阻：%s（退避重试中）" % res)
                    self._results.put((self._after_auto_result, ok, res))
                time.sleep(self._auto_backoff)
                continue
            self._auto_backoff = 0.0
            self._auto_last_err = None
            if getattr(self, "_auto_exec", None) is not None:
                self._auto_exec.set()
            self._results.put((self._after_auto_result, ok, res))

    def _after_auto_result(self, ok, res):
        try:
            if ok:
                self.lab_heard.config(text="识别结果：%s" % res)
                if self.var_exec.get():
                    self.ctrl.run_text_command(res)
            elif ("没听到" not in res) and ("超时" not in res):
                self.lab_heard.config(text="识别结果：—（%s）" % res)
        finally:
            ev = getattr(self, "_auto_exec", None)
            if ev is not None:
                ev.clear()

    # ══════════════════════════════════════════════════════
    #  日志
    # ══════════════════════════════════════════════════════
    def log(self, text):
        self.txt_log.insert("end", text + "\n")
        self.txt_log.see("end")

    # ══════════════════════════════════════════════════════
    #  槽函数
    # ══════════════════════════════════════════════════════
    def refresh_ports(self, prefer=None):
        cur = prefer or self.cmb_port.get().strip() or guess_default_port()
        ports = list_serial_ports()
        self.cmb_port["values"] = ports
        self.cmb_port.set(cur if cur else guess_default_port())
        if not ports:
            self.ctrl.log("未枚举到串口设备。请检查 USB-TTL，或手填设备名。")

    def on_connect(self):
        port = self.cmb_port.get().strip()
        try:
            baud = int(self.cmb_baud.get())
        except ValueError:
            baud = DEFAULT_BAUD
        ok, msg = self.ctrl.connect(port, baud)
        if ok:
            self.btn_conn.config(state="disabled")
            self.btn_disc.config(state="normal")
            self.cmb_port.config(state="disabled")
            self._enable_controls(True)
        else:
            messagebox.showwarning("连接失败", msg, parent=self.root)

    def on_disconnect(self):
        self.ctrl.disconnect()
        self.btn_conn.config(state="normal")
        self.btn_disc.config(state="disabled")
        self.cmb_port.config(state="normal")
        self._enable_controls(False)

    def _enable_controls(self, on):
        st = "normal" if on else "disabled"
        for b in (self.btn_fwd, self.btn_left, self.btn_right, self.btn_back,
                  self.btn_sl, self.btn_sr, self.btn_stop):
            b.config(state=st)

    def on_dir_press(self, action):
        if not self.ctrl.connected:
            return
        self.hold.add(action)
        self.ctrl.move(action)
        self._highlight(action)

    def on_dir_release(self, action):
        self.hold.discard(action)
        if self.var_latch.get():
            return
        if self.ctrl.connected:
            self.ctrl.stop()
            self._highlight(None)

    def on_stop(self):
        self.ctrl.stop()
        self.hold.clear()
        self._highlight(None)

    def on_slider(self, value):
        v = self.ctrl.set_speed_value(float(value))
        self.lab_spd.config(text="%d mm/s" % v)
        if self.ctrl.connected and self.ctrl.action != "stop":
            self.ctrl.move(self.ctrl.action, v)

    def set_speed(self, value):
        v = self.ctrl.set_speed_value(value)
        self.scale.set(v)
        self.lab_spd.config(text="%d mm/s" % v)
        if self.ctrl.connected and self.ctrl.action != "stop":
            self.ctrl.move(self.ctrl.action, v)

    def on_pick_cmd(self, event=None):
        """「常用指令」下拉选中 → 填进输入框（不自动执行，避免误动作）。"""
        v = self.cmb_cmd.get()
        if not v or v.endswith("▾"):
            return
        self.ent_cmd.delete(0, "end")
        self.ent_cmd.insert(0, v)
        self.ent_cmd.focus_set()
        self.cmb_cmd.set("常用指令 ▾")   # 复位

    def on_send_cmd(self):
        text = self.ent_cmd.get().strip()
        if not text:
            return
        self.ent_cmd.delete(0, "end")
        self.ctrl.log("指令输入：%s" % text)
        if getattr(self, "_chat_only", False):
            self._start_llm(text, chat_only=True)     # 纯聊天：只回话
            return
        if self.ctrl.llm_ready():
            # 规则能命中的就别等 LLM（快路径零延迟）
            from car_gui_core import parse_text_command
            if parse_text_command(text, self.ctrl.speed)[0] is not None:
                self.ctrl.run_text_command(text)
                return
            # LLM 要几秒，放后台线程；结果回到界面线程再执行
            self._start_llm(text, chat_only=False)
            return
        self.ctrl.run_text_command(text)

    def _start_llm(self, text, chat_only=False):
        """后台线程问 LLM，回到界面线程执行/播报（Tk 版，同 PySide）。"""
        try:
            self.lab_llm.config(text="本地 LLM：思考中…", fg=C_AMBER)
        except Exception:
            pass
        if chat_only:
            self._submit(lambda: (True, self.ctrl._ask_llm_chat(text)),
                         lambda ok, rep: self.ctrl.ask_chat(text, say=rep))
        else:
            self._submit(lambda: (True, self.ctrl._ask_llm(text)),
                         lambda ok, rep: self.ctrl.run_text_command(text, llm_hint=rep))

    def _highlight(self, action):
        mapping = {
            "forward": self.btn_fwd, "backward": self.btn_back,
            "turn_left": self.btn_left, "turn_right": self.btn_right,
            "strafe_left": self.btn_sl, "strafe_right": self.btn_sr,
        }
        for a, btn in mapping.items():
            if not btn.winfo_exists():
                continue
            if a == action:
                btn.config(bg="#dbeafe", fg=C_BLUE, relief="sunken")
            else:
                btn.config(bg="SystemButtonFace", fg="black", relief="raised")

    # ══════════════════════════════════════════════════════
    #  键盘
    # ══════════════════════════════════════════════════════
    # ══════════════════════════════════════════════════════
    #  语音播报 / 急停 槽函数
    # ══════════════════════════════════════════════════════
    def on_toggle_speak(self):
        self.ctrl.set_speak_enabled(self.var_speak.get())

    def on_toggle_announce(self):
        self.ctrl.set_announce_actions(self.var_announce.get())

    def on_toggle_demo(self):
        """离线演示模式：没接小车也能语音应答（不发串口）。"""
        self.ctrl.set_demo_mode(self.var_demo.get())
        self._refresh_title()

    def on_toggle_llm(self):
        self.ctrl.set_llm_enabled(self.var_llm.get())

    def on_toggle_chat(self):
        """纯聊天模式：只闲聊，绝不执行指令。"""
        self._chat_only = bool(self.var_chat.get())
        self.ctrl.log("纯聊天模式已%s" % ("开启（只回话、不执行指令）"
                                          if self._chat_only else "关闭"))

    def _refresh_title(self):
        """标题反映「离线演示中」/「急停锁定中」，避免误以为车连上了。"""
        pre = ""
        if self.ctrl.status().get("demo_mode"):
            pre += "🎤 离线演示中（未接小车）— "
        if self.ctrl.status().get("estop_locked"):
            pre += "🔒 急停锁定中 — "
        self.root.title(pre + self._title0)

    def on_toggle_kws(self):
        if self.var_kws.get():
            ok, msg = self.ctrl.enable_voice_emergency()
            self.ctrl.log("开启语音急停：%s" % msg)
            if not ok:
                self.var_kws.set(False)
        else:
            self.ctrl.disable_voice_emergency()

    def on_release_estop(self):
        """人工解除急停锁定 —— 锁定后这是唯一的出口。"""
        reason = self.ctrl.release_estop()
        if reason:
            self.ctrl.log("已解除急停锁定（原由：%s）" % reason)
        else:
            self.ctrl.log("当前没有急停锁定")

    def _bind_keys(self):
        self.root.bind_all("<KeyPress>", self.on_key_press)
        self.root.bind_all("<KeyRelease>", self.on_key_release)

    def _typing(self):
        return isinstance(self.root.focus_get(), (tk.Entry, ttk.Entry))

    def on_key_press(self, event):
        if self._typing():
            return
        k = event.keysym.lower()
        if k == "space":
            self.on_stop()
        elif k == "f2":
            self.on_ptt_press()
        elif k == "bracketright":
            self.set_speed(self.ctrl.speed + SPEED_STEP)
        elif k == "bracketleft":
            self.set_speed(self.ctrl.speed - SPEED_STEP)
        elif k in KEY_MAP and self.ctrl.connected:
            act = KEY_MAP[k]
            if act not in self.hold:
                self.on_dir_press(act)

    def on_key_release(self, event):
        if self._typing():
            return
        k = event.keysym.lower()
        if k == "f2":
            self.on_ptt_release()
            return
        if k in KEY_MAP and self.ctrl.connected:
            self.on_dir_release(KEY_MAP[k])

    # ══════════════════════════════════════════════════════
    #  定时刷新
    # ══════════════════════════════════════════════════════
    def tick(self):
        # 先把后台线程（模型加载 / 识别）的结果取回来处理
        while not self._results.empty():
            try:
                cb, ok, res = self._results.get_nowait()
            except queue.Empty:
                break
            try:
                cb(ok, res)
            except Exception as e:
                self.ctrl.log("语音回调异常：%s" % e)

        s = self.ctrl.status()

        if s["connected"]:
            if s["stale"]:
                self.led.config(bg=C_AMBER)
                self.lab_conn.config(text="已连接（收不到回传）")
            else:
                self.led.config(bg=C_GREEN)
                self.lab_conn.config(text="已连接 %s" % s["port"])
        else:
            self.led.config(bg=C_MUTED)
            self.lab_conn.config(text="未连接")

        self.lab_action.config(text="动作：%s" % s["action_label"])
        self.lab_speed.config(text="目标速度：%d mm/s" % s["speed"])

        self.vals["Vx"].config(text=str(s["vx"]))
        self.vals["Vy"].config(text=str(s["vy"]))
        self.vals["Vz"].config(text=str(s["vz"]))
        ax, ay, az = s["accel_ms2"]
        self.vals["ax"].config(text="%.2f" % ax)
        self.vals["ay"].config(text="%.2f" % ay)
        self.vals["az"].config(text="%.2f" % az)
        self.vals["gz"].config(text="%.3f" % s["gyro_rads"][2])

        if s["have_feedback"]:
            v = s["voltage"]
            self.vals["volt"].config(text="%.2f" % v)
            if s["volt_level"] == "danger":
                self.vals["volt"].config(fg=C_RED)
                self.lab_volt.config(text="电压：%.2f V ⚠ 过低" % v, fg=C_RED)
            elif s["volt_level"] == "warn":
                self.vals["volt"].config(fg=C_AMBER)
                self.lab_volt.config(text="电压：%.2f V ⚠ 偏低" % v, fg=C_AMBER)
            else:
                self.vals["volt"].config(fg=C_GREEN)
                self.lab_volt.config(text="电压：%.2f V" % v, fg="black")

            if s["flag_stop"]:
                self.lab_flag.config(text="急停标志：已触发", fg=C_RED)
            else:
                self.lab_flag.config(text="急停标志：正常", fg=C_GREEN)
        else:
            self.lab_volt.config(text="电压：—")
            self.lab_flag.config(text="急停标志：—")

        for ln in self.ctrl.drain_logs():
            self.log(ln)

        # 语音播报 / 语音急停 / 急停锁定
        self._refresh_speech(s)

        # 语音输入电平
        if self.asr is not None:
            if self.asr.recording:
                self.bar_level["value"] = min(100.0, self.asr.level * 500.0)
            elif self.bar_level["value"] != 0:
                self.bar_level["value"] = 0

        self.root.after(100, self.tick)

    # ══════════════════════════════════════════════════════
    #  语音播报 / 急停 的界面刷新
    # ══════════════════════════════════════════════════════
    def _refresh_speech(self, s):
        """刷新 TTS 灯、语音急停灯、急停锁定条、网络音箱地址。"""
        # ① TTS 播报
        ts = self.ctrl.tts_status()
        st = ts["state"]
        if st == "ready":
            self.led_tts.config(bg=C_GREEN)
            pend = "（待播 %d）" % ts["pending"] if ts["pending"] else ""
            self.lab_tts.config(text="播报：就绪　%s%s" % (ts["msg"], pend),
                                fg="black")
        elif st == "loading":
            self.led_tts.config(bg=C_AMBER)
            self.lab_tts.config(text="播报：正在加载模型…（约 5~7 秒）", fg=C_AMBER)
        elif st == "failed":
            self.led_tts.config(bg=C_RED)
            self.lab_tts.config(text="播报：不可用 —— %s" % ts["msg"], fg=C_RED)
        else:
            self.led_tts.config(bg=C_MUTED)
            self.lab_tts.config(text="播报：未启用", fg=C_MUTED)

        # 没连小车时的状态要说清楚，否则会以为车连上了
        if s.get("demo_mode") and not s.get("connected"):
            self.lab_web.config(text="🔌 离线演示中（未接小车）：只语音应答，不发串口",
                                fg=C_AMBER)
        elif not s.get("connected"):
            self.lab_web.config(text="未连接小车（左侧可开「离线演示模式」）", fg=C_MUTED)
        elif ts["has_output"]:
            self.lab_web.config(text="板载喇叭（USB 音箱）", fg="black")
        elif ts["web_url"]:
            self.lab_web.config(text="%s　← 浏览器打开当喇叭" % ts["web_url"],
                                fg=C_BLUE)
        else:
            self.lab_web.config(text="无（没喇叭，网络音箱也没起来）", fg=C_MUTED)

        # ①b LLM（指令纠错 + 对话）—— 本地或云端
        ls = self.ctrl.llm_status()
        lst = ls.get("state")
        # 后端标签：云端/本地要分清（早期一律写"本地"，接云端后会误导）
        _tag = "云端 LLM" if ls.get("mode") == "api" else "本地 LLM"
        if lst == "ready":
            if not ls.get("enabled"):
                self.led_llm.config(bg=C_MUTED)
                self.lab_llm.config(text="%s：已就绪但被关闭" % _tag, fg=C_MUTED)
            else:
                self.led_llm.config(bg=C_GREEN)
                stats = ls.get("stats") or {}
                extra = ("　已问 %d 次" % stats["calls"]) if stats.get("calls") else ""
                if getattr(self, "_chat_only", False):
                    extra += "　【纯聊天】"
                self.lab_llm.config(
                    text="%s：%s%s" % (_tag, ls.get("model") or "就绪", extra),
                    fg="black")
        elif lst == "failed":
            self.led_llm.config(bg=C_AMBER)      # 不是错误：没有它也能用
            self.lab_llm.config(text="LLM：不可用（规则解析照常）", fg=C_AMBER)
            if self.var_llm.get():
                self.var_llm.set(False)
        else:
            self.led_llm.config(bg=C_MUTED)
            self.lab_llm.config(text="LLM：未启用", fg=C_MUTED)

        reps = ls.get("replies") or []
        if len(reps) != getattr(self, "_chat_shown", -1):
            self._chat_shown = len(reps)
            try:
                self.lst_chat.delete(0, "end")
                for u, a in reps[-6:]:
                    self.lst_chat.insert("end", "你：%s" % u)
                    self.lst_chat.insert("end", "车：%s" % a)
            except Exception:
                pass

        # ② 语音急停
        vs = self.ctrl.voice_status()
        kst = vs["state"]
        if kst == "ready":
            if vs["watchdog_ok"] is False:
                # ★ 麦克风链路死了 → 语音急停已失效，必须红字报警
                self.led_kws.config(bg=C_RED)
                self.lab_kws.config(text="语音急停：⚠️ 麦克风链路已断！急停已失效",
                                    fg=C_RED)
                if not self._kws_warned:
                    self._kws_warned = True
                    self.ctrl.log("⚠️ 语音急停的麦克风链路已断开 —— "
                                  "语音急停已失效，请检查麦克风！")
            else:
                self.led_kws.config(bg=C_GREEN)
                self.lab_kws.config(text="语音急停：监听中　%s" % vs["msg"],
                                    fg="black")
                self._kws_warned = False
            if not self.var_kws.get():
                self.var_kws.set(True)
            self.chk_kws.config(text="关闭语音急停")
        elif kst == "loading":
            self.led_kws.config(bg=C_AMBER)
            self.lab_kws.config(text="语音急停：正在启动…", fg=C_AMBER)
        elif kst == "failed":
            self.led_kws.config(bg=C_RED)
            self.lab_kws.config(text="语音急停：不可用 —— %s" % vs["msg"],
                                fg=C_RED)
            if self.var_kws.get():
                self.var_kws.set(False)
            self.chk_kws.config(text="开启语音急停")
        else:
            self.led_kws.config(bg=C_MUTED)
            self.lab_kws.config(text="语音急停：未启用", fg=C_MUTED)
            if self.var_kws.get():
                self.var_kws.set(False)
            self.chk_kws.config(text="开启语音急停")

        self.lab_hits.config(text="命中 %d 次" % s.get("voice_hit_count", 0))
        self.bar_mic["value"] = min(100.0, (vs["level"] or 0.0) * 500.0)

        # ③ 急停锁定条
        self._show_estop_bar(bool(s["estop_locked"]))
        if s["estop_locked"]:
            self.lab_estop_lock.config(
                text="🔒 急停锁定中 —— %s（已拒绝一切运动指令）" % s["estop_reason"])
        # 标题统一由 _refresh_title 维护（离线演示 + 急停锁定两种前缀）
        self._refresh_title()

        # ④ 语音事件
        for kind, detail, _ts in self.ctrl.poll_voice_events():
            if kind == "estop":
                self.ctrl.log("★ 语音急停！%s —— 已停车并进入锁定态" % detail)
            elif kind == "estop_released":
                self.ctrl.log("急停锁定已解除（原由：%s）" % detail)
            else:
                self.ctrl.log("[语音] %s：%s" % (kind, detail))

    # ══════════════════════════════════════════════════════
    def on_close(self):
        try:
            self._auto_listen = False
            if self.asr is not None:
                self.asr.cancel_recording()
            if self.ctrl.connected:
                self.ctrl.emergency_stop()
            self.ctrl.disconnect()
            # 关掉语音急停监听 + 播报队列 + 网络音箱（会占麦克风/串口/端口）
            self.ctrl.shutdown_voice()
        except Exception:
            pass
        self.root.destroy()


def main():
    ap = argparse.ArgumentParser(description=APP_NAME + "（Tkinter 版）")
    ap.add_argument("--port", default=None, help="串口设备名，默认自动探测")
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    ap.add_argument("--model-dir", default=None, help="语音模型目录")
    ap.add_argument("--no-tts", action="store_true",
                    help="不预加载语音播报（省 5~7 秒启动时间和约 600MB 内存）")
    ap.add_argument("--demo", action="store_true",
                    help="离线演示模式：没接小车也接受文本指令并语音应答（不发串口）")
    ap.add_argument("--no-llm", action="store_true",
                    help="不启用 LLM（规则听不懂时直接回「没听懂」）")
    ap.add_argument("--llm-mode", default="auto",
                    choices=("auto", "api", "server", "cli"),
                    help="auto=有云端key且网络通就用云端否则本地(默认) / "
                         "api=强制云端 / server|cli=强制本地")
    args = ap.parse_args()

    root = tk.Tk()
    App(root, port=args.port, baud=args.baud, model_dir=args.model_dir,
        auto_tts=not args.no_tts, demo=args.demo, auto_llm=not args.no_llm,
        llm_mode=args.llm_mode)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
