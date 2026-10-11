# -*- coding: utf-8 -*-
"""
car_gui_pyside.py —— 智能小车手动控制台（PySide6 版，主推）

界面分区
--------
  顶部：状态条（连接状态 / 串口 / 速度 / 当前动作 / 电压 / 急停标志）
  左侧：串口连接面板
  中间：方向控制盘 + 速度调节 + 急停
  右侧：实时状态（速度 / IMU / 电压）+ 运行日志
  底部：语音输入面板（按住说话 / 自动聆听）+ 文本指令输入框

运行：
    python3 car_gui_pyside.py
    python3 car_gui_pyside.py --port /dev/ttyCAR
    python3 car_gui_pyside.py --model-dir ~/models/sense-voice

快捷键：
    W / ↑  前进        S / ↓  后退
    A / ←  左转        D / →  右转
    Q      左移        E      右移
    空格   急停
    F2     按住说话（松开开始识别）
    [ / ]  减速 / 加速
    F11    全屏切换    Esc    退出全屏
"""

import argparse
import os
import queue
import sys
import threading
import time

# ── 依赖检查（给 Linux 上常见缺库一个明确提示）──────────────────────
try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QFont, QFontDatabase, QKeySequence
    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QComboBox, QFrame, QGridLayout, QGroupBox,
        QHBoxLayout, QLabel, QLineEdit, QListWidget, QMainWindow, QMessageBox,
        QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QSlider,
        QVBoxLayout, QWidget,
    )
except Exception as e:  # pragma: no cover
    sys.stderr.write(
        "\n[错误] 无法导入 PySide6：%s\n\n"
        "请先在开发板上安装：\n"
        "    bash install_deps.sh\n"
        "或手动：\n"
        "    sudo apt install -y libgl1 libegl1 libxkbcommon-x11-0 libxcb-cursor0\n"
        "    pip3 install PySide6 -i https://pypi.tuna.tsinghua.edu.cn/simple\n\n"
        "如果实在装不上，用备用的 Tkinter 版本（零依赖）：\n"
        "    python3 car_gui_tk.py\n\n" % e)
    raise SystemExit(1)

from car_gui_core import (
    APP_NAME, APP_VERSION, DEFAULT_BAUD, DEFAULT_SPEED,
    SPEED_MIN, SPEED_MAX, SPEED_STEP, COMMON_COMMANDS,
    PORT_CANDIDATES, HAS_LLM,
    CarController, guess_default_port, list_serial_ports,
)
#: 关键词闸门的提示函数（只用于界面提醒，不做安全判断）
try:
    from llm_engine import looks_like_command
except Exception:                                       # pragma: no cover
    def looks_like_command(_t):
        return True

# ── 语音识别（可选）────────────────────────────────────────────────────
# asr_engine 对 numpy / sounddevice / sherpa-onnx 全部懒加载，
# 即使一个都没装，本模块也能正常 import，只是语音面板会提示"依赖缺失"。
HAS_ASR_MODULE = False
ASR_IMPORT_ERROR = ""
try:
    from asr_engine import (
        AsrEngine, dependency_status, guess_input_device, guess_model_dir,
        list_input_devices, save_preferred_input,
    )
    HAS_ASR_MODULE = True
except Exception as _e:                       # pragma: no cover
    ASR_IMPORT_ERROR = str(_e)

    def save_preferred_input(_x):
        """asr_engine 不可用时的空实现（保持界面代码不用到处判 None）。"""
        return ""

# ── 配色 ────────────────────────────────────────────────────────────
C_BG = "#f5f6f8"
C_PANEL = "#ffffff"
C_BORDER = "#dcdfe6"
C_TEXT = "#2c2c2a"
C_MUTED = "#5f5e5a"
C_BLUE = "#185fa5"
C_GREEN = "#3b6d11"
C_AMBER = "#854f0b"
C_RED = "#a32d2d"
C_DANGER_FILL = "#e24b4a"

SPEED_PRESETS = (100, 200, 300, 400, 500)

STYLESHEET = """
QWidget { background: %(bg)s; color: %(text)s; }
QGroupBox {
    background: %(panel)s; border: 1px solid %(border)s; border-radius: 8px;
    margin-top: 14px; padding: 10px 8px 8px 8px; font-weight: 500;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: %(muted)s; }
QLabel { background: transparent; }
QPushButton {
    background: %(panel)s; border: 1px solid %(border)s; border-radius: 6px;
    padding: 8px 10px; font-size: 13px;
}
QPushButton:hover { border-color: %(blue)s; color: %(blue)s; }
QPushButton:pressed { background: #e6f1fb; }
QPushButton:disabled { color: #b4b2a9; border-color: #ececec; }
QPushButton#dirBtn { font-size: 15px; font-weight: 500; min-height: 40px; }
QPushButton#stopBtn {
    background: %(danger)s; color: #ffffff; border: none; border-radius: 8px;
    font-size: 17px; font-weight: 500; min-height: 52px;
}
QPushButton#stopBtn:hover { background: #b83e3d; }
QPushButton#stopBtn:pressed { background: #8f2f2e; }
QPushButton#pttBtn {
    background: %(panel)s; border: 2px solid %(green)s; border-radius: 8px;
    color: %(green)s; font-size: 14px; font-weight: 500; min-height: 34px;
}
QPushButton#pttBtn:pressed { background: #eaf3de; }
QPushButton#pttBtn:disabled { border-color: #dcdcdc; color: #b4b2a9; }
QPushButton#autoBtn {
    border: 1px solid %(border)s; border-radius: 8px; min-height: 34px;
}
QPushButton#autoBtn:checked {
    background: #e6f1fb; border: 2px solid %(blue)s; color: %(blue)s; font-weight: 500;
}
QPushButton#kwsBtn { border: 1px solid %(border)s; border-radius: 8px; min-height: 34px; }
QPushButton#kwsBtn:checked {
    background: #eaf3de; border: 2px solid %(green)s; color: %(green)s; font-weight: 500;
}
QProgressBar { border: 1px solid %(border)s; border-radius: 4px; background: #fbfbfc; }
QProgressBar::chunk { background: %(green)s; border-radius: 3px; }
QComboBox, QLineEdit { background: %(panel)s; border: 1px solid %(border)s;
    border-radius: 6px; padding: 5px 6px; }
QSlider::groove:horizontal { height: 6px; background: #e6e6e6; border-radius: 3px; }
QSlider::handle:horizontal { width: 18px; margin: -7px 0; border-radius: 9px;
    background: %(blue)s; }
QPlainTextEdit { background: #fbfbfc; border: 1px solid %(border)s;
    border-radius: 6px; font-family: monospace; }
""" % {"bg": C_BG, "panel": C_PANEL, "border": C_BORDER, "text": C_TEXT,
       "muted": C_MUTED, "blue": C_BLUE, "green": C_GREEN, "danger": C_DANGER_FILL}


def pick_cjk_font():
    """挑一个系统里存在的中文字体，避免中文显示成方框。"""
    try:
        families = set(QFontDatabase.families())
    except Exception:
        return None
    for name in ("Noto Sans CJK SC", "Noto Sans CJK JP", "Source Han Sans SC",
                 "WenQuanYi Micro Hei", "WenQuanYi Zen Hei", "Ubuntu",
                 "DejaVu Sans", "Sans Serif"):
        if name in families:
            return name
    return None


def make_led(color):
    lab = QLabel()
    lab.setFixedSize(12, 12)
    lab.setStyleSheet("background:%s; border-radius:6px;" % color)
    return lab


class StatBox(QWidget):
    """一个「标题 + 数值」的小卡片。"""

    def __init__(self, title, value="-", unit="", color=None):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.title = QLabel(title)
        self.title.setStyleSheet("color:%s; font-size:11px;" % C_MUTED)
        self.value = QLabel(value)
        f = self.value.font()
        f.setPointSize(f.pointSize() + 4)
        f.setBold(True)
        self.value.setFont(f)
        if color:
            self.value.setStyleSheet("color:%s;" % color)
        self.unit = QLabel(unit)
        self.unit.setStyleSheet("color:%s; font-size:11px;" % C_MUTED)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        row.addWidget(self.value)
        row.addWidget(self.unit)
        row.addStretch(1)
        lay.addWidget(self.title)
        lay.addLayout(row)

    def set_value(self, text, color=None):
        self.value.setText(str(text))
        self.value.setStyleSheet("color:%s;" % (color or C_TEXT))


class MainWindow(QMainWindow):

    def __init__(self, port=None, baud=DEFAULT_BAUD, model_dir=None,
                 auto_tts=True, demo=False, auto_llm=True, llm_mode="auto"):
        super().__init__()
        self.ctrl = CarController()
        self._hold_actions = set()
        self._kws_warned = False        # 麦克风断链只提示一次，别刷屏
        self._chat_only = False         # 「纯聊天」模式：只回话、不执行指令
        self._chat_shown = -1           # 对话记录已渲染的条数（避免重复重建）

        # ── 语音识别状态 ──────────────────────────────────────────
        self.asr = None
        self._results = queue.Queue()      # 后台线程 → 界面线程 的结果回传
        self._auto_listen = False
        self._auto_thread = None
        self._ptt_ok = False
        #: 录音时是否已让 KWS 让出麦克风（录完要还回去，见 ctrl._yield_mic）
        self._mic_yielded = False
        #: ★ 自动聆听期间"主线程正在执行指令/播报"——聆听循环要等它做完再开麦，
        #:   否则播报与录音抢同一个 subdevice（本板硬件互斥）→ 开麦失败
        self._auto_exec = threading.Event()
        #: ★ 自动聆听的失败退避（秒）。见 `_auto_loop`：
        #:   开麦失败后**绝不能立刻重试** —— 反复失败的 AlsaOpen 会让
        #:   PortAudio 断言 abort（闪退），try/except 抓不住。
        self._auto_backoff = 0.0
        #: 上一条自动聆听错误（用于日志节流，见 `_auto_loop`）
        self._auto_last_err = None
        if HAS_ASR_MODULE:
            self.asr = AsrEngine(log=self.ctrl.log)

        self.setWindowTitle("%s  v%s" % (APP_NAME, APP_VERSION))
        self.resize(1120, 830)

        self._build_ui()
        self.refresh_ports(prefer=port)
        self.refresh_voice_devices()
        if model_dir:
            self.txt_model.setText(model_dir)

        # 离线演示模式（--demo）：没接小车也能用语音交互。
        # 用 setChecked 而不是直接调 ctrl，保证界面与核心状态一致。
        if demo:
            self.chk_demo.setChecked(True)

        # LLM（指令纠错 + 对话）：探测很快（云端只查配置；本地不加载权重），可直接同步做。
        # mode="auto"：配了云端 key 且网络通 → 用云端（实测 2.7~4.5s）；
        #              否则自动回退本地 llama.cpp（16~35s）—— 离线场景不受影响。
        if auto_llm:
            ok, msg = self.ctrl.init_llm(mode=llm_mode)
            if not ok:
                self.ctrl.log("LLM 未启用：%s（规则解析与播报不受影响）" % msg)

        # 语音播报（TTS）：**后台**预加载 —— 模型要 5~7 秒，同步做会把界面冻死。
        # 进度看 tick() 里刷新的 TTS 指示灯。
        if auto_tts:
            ok, msg = self.ctrl.init_tts(background=True)
            self.ctrl.log("TTS：%s" % msg)
        else:
            self.ctrl.set_speak_enabled(False)
            self.ctrl.log("TTS：已按 --no-tts 参数关闭播报")

        # 状态 / 日志轮询
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(100)

        self.ctrl.log("就绪。请选择串口并点击「连接」。")
        self.ctrl.log("设备名提示：板载 CH9102 通常枚举为 /dev/ttyUSB0；已配置 udev 则为 /dev/ttyCAR")
        if not HAS_ASR_MODULE:
            self.ctrl.log("语音模块导入失败：%s" % ASR_IMPORT_ERROR)
        elif self.asr and self.asr.ready:
            self.ctrl.log("语音模型已就绪")

    # ══════════════════════════════════════════════════════
    #  界面构建
    # ══════════════════════════════════════════════════════
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(10)

        outer.addWidget(self._build_statusbar())

        # 急停锁定警示条：平时隐藏，锁定时才出现（要足够刺眼 + 就地可解除）
        self.bar_estop = self._build_estop_bar()
        self.bar_estop.setVisible(False)
        outer.addWidget(self.bar_estop)

        mid = QHBoxLayout()
        mid.setSpacing(10)
        mid.addWidget(self._build_conn_panel(), 0)
        mid.addWidget(self._build_control_panel(), 1)
        mid.addWidget(self._build_status_panel(), 0)
        outer.addLayout(mid, 1)

        outer.addWidget(self._build_voice_panel())
        outer.addWidget(self._build_speech_panel())
        outer.addWidget(self._build_cmd_bar())

    # ── 顶部状态条 ──────────────────────────────────────────────────
    def _build_statusbar(self):
        box = QFrame()
        box.setStyleSheet("QFrame{background:%s;border:1px solid %s;border-radius:8px;}"
                          % (C_PANEL, C_BORDER))
        lay = QHBoxLayout(box)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(14)

        self.led_conn = make_led(C_MUTED)
        self.lab_conn = QLabel("未连接")
        self.lab_conn.setStyleSheet("font-weight:500;")
        lay.addWidget(self.led_conn)
        lay.addWidget(self.lab_conn)

        lay.addWidget(self._vsep())

        self.lab_action = QLabel("动作：—")
        self.lab_speed = QLabel("目标速度：%d mm/s" % DEFAULT_SPEED)
        self.lab_volt = QLabel("电压：—")
        self.lab_stopflag = QLabel("急停标志：—")
        for w in (self.lab_action, self.lab_speed, self.lab_volt, self.lab_stopflag):
            lay.addWidget(w)
            lay.addWidget(self._vsep())

        lay.addStretch(1)
        self.lab_hint = QLabel("按住方向键行驶，松开即停 ｜ 空格 = 急停 ｜ F2 = 按住说话")
        self.lab_hint.setStyleSheet("color:%s; font-size:11px;" % C_MUTED)
        lay.addWidget(self.lab_hint)
        return box

    def _vsep(self):
        s = QFrame()
        s.setFrameShape(QFrame.VLine)
        s.setStyleSheet("color:%s;" % C_BORDER)
        s.setFixedWidth(1)
        return s

    # ── 左侧：串口 ──────────────────────────────────────────────────
    def _build_conn_panel(self):
        g = QGroupBox("串口连接")
        g.setFixedWidth(232)
        v = QVBoxLayout(g)
        v.setSpacing(8)

        v.addWidget(QLabel("设备"))
        self.cmb_port = QComboBox()
        self.cmb_port.setEditable(True)
        # 误输入防护：见 _on_port_edited 的说明（实测踩到中文漏进设备名）
        self._port_guard = False
        self._port_last = guess_default_port()
        self._port_ok = list(PORT_CANDIDATES)
        self.cmb_port.editTextChanged.connect(self._on_port_edited)
        self.cmb_port.currentTextChanged.connect(self._on_port_text_changed)
        v.addWidget(self.cmb_port)

        self.btn_refresh = QPushButton("刷新设备列表")
        self.btn_refresh.clicked.connect(lambda: self.refresh_ports())
        v.addWidget(self.btn_refresh)

        v.addWidget(QLabel("波特率"))
        self.cmb_baud = QComboBox()
        for b in (9600, 19200, 38400, 57600, 115200, 230400, 460800):
            self.cmb_baud.addItem(str(b))
        self.cmb_baud.setCurrentText(str(DEFAULT_BAUD))
        v.addWidget(self.cmb_baud)

        self.btn_connect = QPushButton("连接")
        self.btn_connect.clicked.connect(self.on_connect)
        v.addWidget(self.btn_connect)

        self.btn_disconnect = QPushButton("断开")
        self.btn_disconnect.clicked.connect(self.on_disconnect)
        self.btn_disconnect.setEnabled(False)
        v.addWidget(self.btn_disconnect)

        # ── 离线演示模式 ────────────────────────────────────────────
        # 需求来源（2026-09-27）：人不在实验室、接不上小车时，这个界面也要能用 ——
        # 当作一个"能听会说"的对话程序。开启后指令照常被接受并**语音应答**，
        # 但**绝不发串口**（move()/stop() 的未连接判断没有改动）。
        v.addWidget(self._vsep())
        self.chk_demo = QCheckBox("离线演示模式")
        self.chk_demo.setToolTip(
            "没接小车时也能用：文本指令照常解析并语音应答，但不会发串口。\n"
            "车不动 —— 安全上不可能让车动。")
        self.chk_demo.toggled.connect(self.on_toggle_demo)
        v.addWidget(self.chk_demo)
        lab_demo = QLabel("不开则未连车时指令只记日志、不出声")
        lab_demo.setWordWrap(True)
        lab_demo.setStyleSheet("color:%s;font-size:11px;" % C_MUTED)
        v.addWidget(lab_demo)

        tip = QLabel("提示：需要把当前用户加入 dialout 组\n"
                     "才有权限打开串口：\n"
                     "sudo usermod -aG dialout $USER")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:%s; font-size:11px;" % C_MUTED)
        v.addWidget(tip)

        v.addStretch(1)
        return g

    # ── 中间：方向控制 ──────────────────────────────────────────────
    def _build_control_panel(self):
        g = QGroupBox("手动控制")
        v = QVBoxLayout(g)
        v.setSpacing(10)

        pad = QGridLayout()
        pad.setSpacing(8)
        pad.setContentsMargins(0, 0, 0, 0)

        def dirbtn(text, action, keys):
            b = QPushButton(text)
            b.setObjectName("dirBtn")
            b.setFocusPolicy(Qt.NoFocus)      # 防止空格误触发按钮
            b.setToolTip("快捷键：%s" % keys)
            b.pressed.connect(lambda a=action: self.on_dir_press(a))
            b.released.connect(lambda a=action: self.on_dir_release(a))
            return b

        self.btn_fwd = dirbtn("↑  前进  (W)", "forward", "W / ↑")
        self.btn_left = dirbtn("←  左转  (A)", "turn_left", "A / ←")
        self.btn_right = dirbtn("→  右转  (D)", "turn_right", "D / →")
        self.btn_back = dirbtn("↓  后退  (S)", "backward", "S / ↓")
        self.btn_sl = dirbtn("←  左移  (Q)", "strafe_left", "Q")
        self.btn_sr = dirbtn("→  右移  (E)", "strafe_right", "E")

        self.btn_stop = QPushButton("■  停  止   （空格）")
        self.btn_stop.setObjectName("stopBtn")
        self.btn_stop.setFocusPolicy(Qt.NoFocus)
        self.btn_stop.clicked.connect(self.on_stop)

        pad.addWidget(self.btn_fwd, 0, 1)
        pad.addWidget(self.btn_left, 1, 0)
        pad.addWidget(self.btn_stop, 1, 1)
        pad.addWidget(self.btn_right, 1, 2)
        pad.addWidget(self.btn_back, 2, 1)
        pad.addWidget(self.btn_sl, 3, 0)
        pad.addWidget(self.btn_sr, 3, 2)
        for c in range(3):
            pad.setColumnStretch(c, 1)
        v.addLayout(pad)

        self.chk_latch = QCheckBox("锁定持续运动（松开方向按钮后不停，再点一次停止）")
        self.chk_latch.setToolTip(
            "不勾选 = 点动模式：按住才走、松开即停（更安全，默认）\n"
            "勾选   = 锁存模式：点一下就持续走，直到按「停止」")
        v.addWidget(self.chk_latch)

        # 速度
        sp = QGroupBox("速度")
        sv = QVBoxLayout(sp)
        sv.setSpacing(6)

        row = QHBoxLayout()
        row.addWidget(QLabel("线速度 / 角速度幅值"))
        row.addStretch(1)
        self.lab_spd = QLabel("%d mm/s" % DEFAULT_SPEED)
        f = self.lab_spd.font()
        f.setBold(True)
        self.lab_spd.setFont(f)
        row.addWidget(self.lab_spd)
        sv.addLayout(row)

        self.sld = QSlider(Qt.Horizontal)
        self.sld.setFocusPolicy(Qt.NoFocus)
        self.sld.setRange(SPEED_MIN, SPEED_MAX)
        self.sld.setSingleStep(SPEED_STEP)
        self.sld.setPageStep(SPEED_STEP)
        self.sld.setValue(DEFAULT_SPEED)
        self.sld.valueChanged.connect(self.on_slider)
        sv.addWidget(self.sld)

        prow = QHBoxLayout()
        prow.setSpacing(6)
        for v_ in SPEED_PRESETS:
            b = QPushButton(str(v_))
            b.setFocusPolicy(Qt.NoFocus)
            b.setFixedHeight(30)
            b.clicked.connect(lambda _=False, x=v_: self.set_speed(x))
            prow.addWidget(b)
        self.btn_spd_down = QPushButton("−")
        self.btn_spd_down.setFocusPolicy(Qt.NoFocus)
        self.btn_spd_down.setFixedHeight(30)
        self.btn_spd_down.clicked.connect(lambda: self.set_speed(self.ctrl.speed - SPEED_STEP))
        self.btn_spd_up = QPushButton("＋")
        self.btn_spd_up.setFocusPolicy(Qt.NoFocus)
        self.btn_spd_up.setFixedHeight(30)
        self.btn_spd_up.clicked.connect(lambda: self.set_speed(self.ctrl.speed + SPEED_STEP))
        prow.addWidget(self.btn_spd_down)
        prow.addWidget(self.btn_spd_up)
        sv.addLayout(prow)
        v.addWidget(sp)

        v.addStretch(1)
        return g

    # ── 右侧：状态 ──────────────────────────────────────────────────
    def _build_status_panel(self):
        w = QWidget()
        w.setFixedWidth(300)
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        g1 = QGroupBox("实时状态")
        gv = QGridLayout(g1)
        gv.setSpacing(8)

        self.sb_vx = StatBox("X 轴速度(Vx)", "0", "mm/s")
        self.sb_vy = StatBox("Y 轴速度(Vy)", "0", "mm/s")
        self.sb_vz = StatBox("Z 轴速度(Vz)", "0", "mm/s")
        self.sb_volt = StatBox("电池电压", "—", "V", C_MUTED)
        self.sb_ax = StatBox("加速度 X", "0.00", "m/s²")
        self.sb_ay = StatBox("加速度 Y", "0.00", "m/s²")
        self.sb_az = StatBox("加速度 Z", "0.00", "m/s²")
        self.sb_gz = StatBox("角速度 Z", "0.000", "rad/s")

        gv.addWidget(self.sb_vx, 0, 0)
        gv.addWidget(self.sb_vy, 0, 1)
        gv.addWidget(self.sb_vz, 1, 0)
        gv.addWidget(self.sb_volt, 1, 1)
        gv.addWidget(self.sb_ax, 2, 0)
        gv.addWidget(self.sb_ay, 2, 1)
        gv.addWidget(self.sb_az, 3, 0)
        gv.addWidget(self.sb_gz, 3, 1)
        v.addWidget(g1)

        g2 = QGroupBox("运行日志")
        lv = QVBoxLayout(g2)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        lv.addWidget(self.log)
        v.addWidget(g2, 1)

        return w

    # ── 急停锁定警示条 ──────────────────────────────────────────────
    def _build_estop_bar(self):
        """
        急停锁定时的醒目警示条（平时隐藏）。

        为什么要单独一条、而且要这么刺眼：
        语音（或手动）急停后车会进入**锁定态**，之后所有运动指令都被拒绝。
        如果界面上不提示、又没地方解除，操作者会以为"程序坏了"。
        所以这里同时给出**原因**和**解除按钮**。
        """
        box = QFrame()
        box.setStyleSheet(
            "QFrame{background:#fdecec;border:2px solid %s;border-radius:8px;}"
            % C_RED)
        lay = QHBoxLayout(box)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(12)

        self.lab_estop_lock = QLabel("🔒 急停锁定中")
        self.lab_estop_lock.setStyleSheet(
            "color:%s;font-size:15px;font-weight:600;background:transparent;"
            % C_RED)
        lay.addWidget(self.lab_estop_lock, 1)

        self.btn_release = QPushButton("解除急停锁定")
        self.btn_release.setFocusPolicy(Qt.NoFocus)
        self.btn_release.setStyleSheet(
            "QPushButton{background:%s;color:#ffffff;border:none;border-radius:6px;"
            "padding:8px 18px;font-weight:600;}"
            "QPushButton:hover{background:#b83e3d;}" % C_RED)
        self.btn_release.clicked.connect(self.on_release_estop)
        lay.addWidget(self.btn_release)
        return box

    # ── 语音播报 / 语音急停 ─────────────────────────────────────────
    def _build_speech_panel(self):
        g = QGroupBox("语音播报 / 语音急停")
        v = QVBoxLayout(g)
        v.setSpacing(8)

        # ── 第一行：TTS 播报状态 + 开关 ──
        r1 = QHBoxLayout()
        r1.setSpacing(8)
        self.led_tts = make_led(C_MUTED)
        r1.addWidget(self.led_tts)
        self.lab_tts = QLabel("播报：未启用")
        self.lab_tts.setStyleSheet("color:%s;" % C_MUTED)
        r1.addWidget(self.lab_tts, 1)

        self.chk_speak = QCheckBox("播报")
        self.chk_speak.setChecked(True)
        self.chk_speak.setToolTip(
            "播报总开关。关掉后普通播报不再出声；"
            "**急停播报不受影响**（那种提示必须说）")
        self.chk_speak.toggled.connect(self.on_toggle_speak)
        r1.addWidget(self.chk_speak)

        self.chk_announce = QCheckBox("动作播报")
        self.chk_announce.setChecked(True)
        self.chk_announce.setToolTip(
            "是否播报「开始前进」这类动作确认。嫌吵可以关掉")
        self.chk_announce.toggled.connect(self.on_toggle_announce)
        r1.addWidget(self.chk_announce)
        v.addLayout(r1)

        # ── 第二行：语音急停（KWS）──
        r2 = QHBoxLayout()
        r2.setSpacing(8)
        self.led_kws = make_led(C_MUTED)
        r2.addWidget(self.led_kws)
        self.lab_kws = QLabel("语音急停：未启用")
        self.lab_kws.setStyleSheet("color:%s;" % C_MUTED)
        r2.addWidget(self.lab_kws, 1)

        self.btn_kws = QPushButton("开启语音急停")
        self.btn_kws.setObjectName("kwsBtn")
        self.btn_kws.setFocusPolicy(Qt.NoFocus)
        self.btn_kws.setCheckable(True)
        self.btn_kws.setToolTip(
            "常开监听麦克风，听到「急停/停车/刹车…」立刻停车并进入锁定态")
        self.btn_kws.clicked.connect(self.on_toggle_kws)
        r2.addWidget(self.btn_kws)

        self.bar_mic = QProgressBar()
        self.bar_mic.setRange(0, 100)
        self.bar_mic.setValue(0)
        self.bar_mic.setTextVisible(False)
        self.bar_mic.setFixedWidth(110)
        self.bar_mic.setToolTip("语音急停的麦克风电平")
        r2.addWidget(self.bar_mic)

        self.lab_hits = QLabel("命中 0 次")
        self.lab_hits.setStyleSheet("color:%s;" % C_MUTED)
        r2.addWidget(self.lab_hits)
        v.addLayout(r2)

        # ── 第三行：声音从哪出来 ──
        r3 = QHBoxLayout()
        r3.setSpacing(8)
        r3.addWidget(QLabel("声音输出"))
        self.lab_web = QLabel("—")
        self.lab_web.setStyleSheet("color:%s;" % C_MUTED)
        # 本板**没有喇叭**时，播报是经这个地址送到浏览器播的，所以要能选中复制
        self.lab_web.setTextInteractionFlags(Qt.TextSelectableByMouse)
        r3.addWidget(self.lab_web, 1)
        v.addLayout(r3)

        # ── 第四行：本地 LLM（指令纠错 + 对话）────────────────────────
        # 定位：项目重点是**人机交互**。LLM 把"ASR 听错 → 没听懂"变成
        # "猜出你想干什么"，并把生硬的提示换成拟人化回复。
        r4 = QHBoxLayout()
        r4.setSpacing(8)
        self.led_llm = make_led(C_MUTED)
        r4.addWidget(self.led_llm)
        self.lab_llm = QLabel("LLM：未启用")
        self.lab_llm.setStyleSheet("color:%s;" % C_MUTED)
        r4.addWidget(self.lab_llm, 1)
        self.chk_llm = QCheckBox("启用")
        self.chk_llm.setChecked(True)
        self.chk_llm.setToolTip(
            "规则听不懂时交给本地小模型：① 猜出你真正想下的指令；"
            "② 闲聊时给一句拟人化回复。\n关掉就退回原来的「没听懂」。")
        self.chk_llm.toggled.connect(self.on_toggle_llm)
        r4.addWidget(self.chk_llm)
        self.btn_chat = QPushButton("纯聊天")
        self.btn_chat.setFocusPolicy(Qt.NoFocus)
        self.btn_chat.setCheckable(True)
        self.btn_chat.setToolTip(
            "勾上后**只闲聊，绝不执行指令** —— 演示/聊天时怕误动车就用它")
        self.btn_chat.toggled.connect(self.on_toggle_chat)
        r4.addWidget(self.btn_chat)
        v.addLayout(r4)

        # ── 对话记录（一眼看到 LLM 说了什么）──
        self.lst_chat = QListWidget()
        self.lst_chat.setFixedHeight(74)
        self.lst_chat.setToolTip("最近的对话：你说的话 → 小车回的话")
        self.lst_chat.setStyleSheet("font-size:11px;")
        v.addWidget(self.lst_chat)

        return g

    # ── 底部：文本指令 ──────────────────────────────────────────────
    def _build_cmd_bar(self):
        box = QFrame()
        box.setStyleSheet("QFrame{background:%s;border:1px solid %s;border-radius:8px;}"
                          % (C_PANEL, C_BORDER))
        lay = QHBoxLayout(box)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)

        # 「常用指令」下拉：输入法没配好时的兜底，也是演示时的快捷方式
        self.cmb_cmd = QComboBox()
        self.cmb_cmd.setFixedWidth(110)
        self.cmb_cmd.addItem("常用指令 ▾")
        self.cmb_cmd.addItems(list(COMMON_COMMANDS))
        self.cmb_cmd.setToolTip(
            "点一下即可把指令填进输入框（不会直接执行）。\n"
            "中文输入用 ibus：Super+空格 或 Ctrl+空格 切换中/英。")
        self.cmb_cmd.currentIndexChanged.connect(self.on_pick_cmd)
        lay.addWidget(self.cmb_cmd)

        lay.addWidget(QLabel("指令："))
        self.txt_cmd = QLineEdit()
        self.txt_cmd.setPlaceholderText(
            "试试：前进 ｜ 前进一米 ｜ 后退 2 秒 ｜ 左转90度 ｜ 速度 200 ｜ 快一点 ｜ 停止"
            "（中文输入用 Super+空格 切换）")
        self.txt_cmd.setToolTip(
            "直接用拼音输入中文即可；若打不出中文：bash setup_ibus.sh --check\n"
            "切回英文：Super+空格（或 Ctrl+空格）。")
        self.txt_cmd.returnPressed.connect(self.on_send_cmd)
        lay.addWidget(self.txt_cmd, 1)

        b = QPushButton("发送")
        b.setFocusPolicy(Qt.NoFocus)
        b.clicked.connect(self.on_send_cmd)
        lay.addWidget(b)

        b2 = QPushButton("清空日志")
        b2.setFocusPolicy(Qt.NoFocus)
        b2.clicked.connect(self.log.clear)
        lay.addWidget(b2)
        return box

    # ── 语音输入 ────────────────────────────────────────────────────
    def _build_voice_panel(self):
        g = QGroupBox("语音输入")
        v = QVBoxLayout(g)
        v.setSpacing(8)

        r1 = QHBoxLayout()
        r1.setSpacing(8)
        r1.addWidget(QLabel("模型目录"))
        self.txt_model = QLineEdit()
        self.txt_model.setPlaceholderText("例：/home/HwHiAiUser/models/sense-voice")
        if HAS_ASR_MODULE:
            self.txt_model.setText(guess_model_dir())
        r1.addWidget(self.txt_model, 1)
        self.btn_load_model = QPushButton("加载模型")
        self.btn_load_model.setFocusPolicy(Qt.NoFocus)
        self.btn_load_model.clicked.connect(self.on_load_model)
        r1.addWidget(self.btn_load_model)
        self.lab_asr = QLabel("未加载")
        self.lab_asr.setStyleSheet("color:%s;" % C_MUTED)
        self.lab_asr.setMinimumWidth(220)
        r1.addWidget(self.lab_asr)
        v.addLayout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(8)
        r2.addWidget(QLabel("麦克风"))
        self.cmb_mic = QComboBox()
        self.cmb_mic.setMinimumWidth(210)
        #: 注意：**不要**指望用标志位挡住"程序自动选中"——
        #: `addItem()` 就会触发 `currentIndexChanged`（见
        #: `refresh_voice_devices` 里的 `blockSignals` 说明）。
        #: 这里只做"真正由用户改动"的保存。
        self.cmb_mic.currentIndexChanged.connect(self.on_pick_mic)
        r2.addWidget(self.cmb_mic)
        btn_mic = QPushButton("刷新")
        btn_mic.setFocusPolicy(Qt.NoFocus)
        btn_mic.clicked.connect(self.refresh_voice_devices)
        r2.addWidget(btn_mic)

        self.chk_voice_exec = QCheckBox("识别后自动执行")
        self.chk_voice_exec.setChecked(True)
        self.chk_voice_exec.setToolTip(
            "不勾选时只显示识别结果，不下发任何运动指令（调试识别率时用）")
        r2.addWidget(self.chk_voice_exec)

        self.btn_ptt = QPushButton("按住说话（或按住 F2）")
        self.btn_ptt.setObjectName("pttBtn")
        self.btn_ptt.setFocusPolicy(Qt.NoFocus)
        self.btn_ptt.setEnabled(False)
        self.btn_ptt.pressed.connect(self.on_ptt_press)
        self.btn_ptt.released.connect(self.on_ptt_release)
        r2.addWidget(self.btn_ptt, 1)

        self.btn_auto = QPushButton("开始聆听")
        self.btn_auto.setObjectName("autoBtn")
        self.btn_auto.setFocusPolicy(Qt.NoFocus)
        self.btn_auto.setCheckable(True)
        self.btn_auto.setEnabled(False)
        self.btn_auto.setToolTip("自动聆听：说一句、停顿一下，自动断句识别（无需按键）")
        self.btn_auto.toggled.connect(self.on_auto_toggle)
        r2.addWidget(self.btn_auto)
        v.addLayout(r2)

        r3 = QHBoxLayout()
        r3.setSpacing(8)
        r3.addWidget(QLabel("输入电平"))
        self.bar_level = QProgressBar()
        self.bar_level.setRange(0, 100)
        self.bar_level.setValue(0)
        self.bar_level.setTextVisible(False)
        self.bar_level.setFixedWidth(150)
        r3.addWidget(self.bar_level)
        self.lab_heard = QLabel("识别结果：—")
        self.lab_heard.setStyleSheet("font-weight:500;")
        r3.addWidget(self.lab_heard, 1)
        v.addLayout(r3)

        if not HAS_ASR_MODULE:
            self.btn_load_model.setEnabled(False)
            self.lab_asr.setText("语音模块不可用")
            self.lab_asr.setStyleSheet("color:%s;" % C_RED)
            self.txt_model.setEnabled(False)
            self.cmb_mic.setEnabled(False)
        return g

    # ══════════════════════════════════════════════════════
    #  槽函数
    # ══════════════════════════════════════════════════════
    def refresh_ports(self, prefer=None):
        cur = prefer or self.cmb_port.currentText().strip() or guess_default_port()
        ports = list_serial_ports()
        self._port_ok = list(ports) + list(PORT_CANDIDATES)
        self.cmb_port.clear()
        for p in ports:
            self.cmb_port.addItem(p)
        if cur:
            self.cmb_port.setCurrentText(cur)
        if not ports:
            self.cmb_port.setCurrentText(guess_default_port())
            self.ctrl.log("未枚举到串口设备。请检查 USB-TTL 是否插好，或手填设备名。")

    @staticmethod
    def _looks_like_port(t):
        """像串口设备名吗？用于区分"用户在填设备名"和"误输入中文"。

        规则：只允许 ASCII 可见字符（设备名都是 ASCII）。
        这样 `/dev/ttyUSB3`、`COM3`、`ttyS0` 都能手输，
        而「速度200」「左转90度」这类中文指令会被判为误输入。
        """
        t = (t or "").strip()
        if not t:
            return True
        return all(0x21 <= ord(ch) <= 0x7E for ch in t)

    def _on_port_text_changed(self, text):
        """记住最后一个「可用」的设备名，供 _on_port_edited 还原时使用。"""
        if getattr(self, "_port_guard", False):
            return
        t = (text or "").strip()
        known = getattr(self, "_port_ok", None) or list(PORT_CANDIDATES)
        if t and (t in known or any(p.startswith(t) for p in known)
                  or self._looks_like_port(t)):
            self._port_last = t

    def _on_port_edited(self, text):
        """设备下拉被误输入时还原，防止中文等垃圾累积在里面。

        背景（2026-09-27 实测踩到）：串口设备框是 setEditable(True) 的
        （为了让用户能手填 /dev/ttyUSB3），但演示时如果先点中了它再输入中文，
        文本会**累积进设备名**（实测变成 `/dev/ttyCAR速度200`），
        既连不上设备、又让人一头雾水。
        这里在"明显不像设备名且不是已枚举设备的前缀"时直接还原。
        """
        if getattr(self, "_port_guard", False):
            return
        t = (text or "").strip()
        if not t:
            return
        known = [p for p in (getattr(self, "_port_ok", None)
                             or list(PORT_CANDIDATES))]
        if (t in known) or any(p.startswith(t) for p in known):
            return                                 # 正常枚举值 / 正在手输前缀
        if self._looks_like_port(t):
            return                                 # 用户在填别的路径，放行
        # 明显不是设备名 → 还原
        self._port_guard = True
        try:
            self.cmb_port.setCurrentText(self._port_last)
        finally:
            self._port_guard = False
        self.ctrl.log("「设备」框不接受「%s」—— 这是串口设备名输入框，"
                      "指令请填在下面的指令栏" % t[:20])

    def on_connect(self):
        port = self.cmb_port.currentText().strip()
        try:
            baud = int(self.cmb_baud.currentText())
        except ValueError:
            baud = DEFAULT_BAUD
        ok, msg = self.ctrl.connect(port, baud)
        if ok:
            self.btn_connect.setEnabled(False)
            self.btn_disconnect.setEnabled(True)
            self.cmb_port.setEnabled(False)
            self.cmb_baud.setEnabled(False)
            self._enable_controls(True)
        else:
            QMessageBox.warning(self, "连接失败", msg)

    def on_disconnect(self):
        self.ctrl.disconnect()
        self.btn_connect.setEnabled(True)
        self.btn_disconnect.setEnabled(False)
        self.cmb_port.setEnabled(True)
        self.cmb_baud.setEnabled(True)
        self._enable_controls(False)

    def _enable_controls(self, on):
        for b in (self.btn_fwd, self.btn_left, self.btn_right, self.btn_back,
                  self.btn_sl, self.btn_sr, self.btn_stop):
            b.setEnabled(on)

    def on_dir_press(self, action):
        if not self.ctrl.connected:
            return
        self._hold_actions.add(action)
        self.ctrl.move(action)
        self._highlight(action)

    def on_dir_release(self, action):
        self._hold_actions.discard(action)
        if self.chk_latch.isChecked():
            return          # 锁存模式：松开不停
        if self.ctrl.connected:
            self.ctrl.stop()
            self._highlight(None)

    def on_stop(self):
        self.ctrl.stop()
        self._hold_actions.clear()
        self._highlight(None)

    def on_slider(self, value):
        v = self.ctrl.set_speed_value(value)
        self.lab_spd.setText("%d mm/s" % v)
        # 正在持续运动时，实时改速
        if self.ctrl.connected and self.ctrl.action != "stop":
            self.ctrl.move(self.ctrl.action, v)

    def set_speed(self, value):
        v = self.ctrl.set_speed_value(value)
        self.sld.setValue(v)
        self.lab_spd.setText("%d mm/s" % v)
        if self.ctrl.connected and self.ctrl.action != "stop":
            self.ctrl.move(self.ctrl.action, v)

    def on_pick_cmd(self, idx):
        """「常用指令」下拉选中 → 填进输入框（不自动执行，避免误动作）。"""
        if idx <= 0:
            return
        text = self.cmb_cmd.itemText(idx)
        self.txt_cmd.setText(text)
        self.txt_cmd.setFocus()
        self.cmb_cmd.setCurrentIndex(0)   # 复位，下次还能选同一条

    def on_send_cmd(self):
        text = self.txt_cmd.text().strip()
        if not text:
            return
        self.txt_cmd.clear()
        self.ctrl.log("指令输入：%s" % text)
        self._dispatch_text(text)

    def _dispatch_text(self, text, done=None):
        """把一句文本送上「规则快路径 → 后台 LLM」这条生产路径。

        ★ 抽出来是为了让**自动聆听复用同一条路径**（2026-10-11）：
          原来自动聆听直接 `self.ctrl.run_text_command(res)`，
          规则没命中时 `_ask_llm()` 就在**界面线程**跑网络
          （实测 2.7~4.5s），整个界面冻住 ——
          这正是用户说的「说完话过两三秒才识别到、车动了几秒才出语音」。

        `done`：整条链（含 LLM 后台推理 + 执行）走完后回调，
                用于清 `_auto_exec`、让聆听循环恢复。
        """
        if getattr(self, "_chat_only", False):
            # 纯聊天：**只回话，绝不执行指令**
            self._start_llm(text, chat_only=True, done=done)
            return
        if self.ctrl.llm_ready():
            # 规则能直接命中的话不该等 LLM —— 先试纯规则解析（不碰硬件）
            from car_gui_core import parse_text_command
            if parse_text_command(text, self.ctrl.speed)[0] is not None:
                # ★ 规则命中：同步执行（零网络延迟；急停/停止永远走这条）
                try:
                    self.ctrl.run_text_command(text)
                finally:
                    if done is not None:
                        done()
                return
            # 只有"规则没命中"才走 LLM，且**在后台线程**推理，避免界面冻住
            self._start_llm(text, chat_only=False, done=done)
            return
        try:
            self.ctrl.run_text_command(text)
        finally:
            if done is not None:
                done()

    def _start_llm(self, text, chat_only=False, done=None):
        """在后台线程问 LLM，回到界面线程再执行（LLM 一次要几秒，不能冻界面）。

        `done`：执行完毕（含把播报投出去）后回调。见 `_dispatch_text`。
        """
        self.lab_llm.setText("本地 LLM：思考中…")
        self.lab_llm.setStyleSheet("color:%s;" % C_AMBER)
        if chat_only:
            self._submit(lambda: (True, self.ctrl._ask_llm_chat(text)),
                         lambda ok, rep: self._llm_done(
                             done, self.ctrl.ask_chat, text, ok=ok, say=rep))
        else:
            self._submit(lambda: (True, self.ctrl._ask_llm(text)),
                         lambda ok, rep: self._llm_done(
                             done, self.ctrl.run_text_command, text,
                             ok=ok, llm_hint=rep))

    @staticmethod
    def _llm_done(done, fn, text, ok=True, **kw):
        """LLM 结果回到界面线程后：执行 → 无论成败都调 `done`。

        ⚠️ `ok=False` 时 `kw` 里是**错误字符串**而不是 Reply 对象，
           直接塞给 `run_text_command(llm_hint=...)` 会在
           `reply.error` 上抛 AttributeError（回调异常）。所以先挡一道。
        """
        try:
            if not ok:
                raise RuntimeError(kw.get("llm_hint") or "LLM 后台任务失败")
            fn(text, **kw)
        except Exception as e:
            # 出错也不能吞掉 `done`，否则聆听循环会永远卡在 `_auto_exec`
            print("LLM 结果处理异常：%r" % (e,), flush=True)
        finally:
            if done is not None:
                try:
                    done()
                except Exception:
                    pass

    # ── 后台任务：结果统一回传界面线程 ──────────────────────────────
    def _submit(self, fn, cb):
        """在后台线程执行 fn()（返回 (ok, res)），完成后在界面线程调用 cb。"""
        def worker():
            try:
                ok, res = fn()
            except Exception as e:
                ok, res = False, "异常：%s" % e
            self._results.put((cb, ok, res))
        threading.Thread(target=worker, daemon=True).start()

    # ── 语音：设备与模型 ────────────────────────────────────────────
    def refresh_voice_devices(self):
        if not HAS_ASR_MODULE:
            return
        devs = list_input_devices()
        # 🔴 必须**全程屏蔽信号**（2026-10-09 踩到）：
        #   Qt 的 `QComboBox.addItem()` 会**自动把第一个条目设为当前项**
        #   并发射 `currentIndexChanged` —— 于是"填充列表"这个动作
        #   会被 `on_pick_mic` 当成"用户手动选择"，把**错的设备**
        #   写进偏好文件（实测存成了 `usb audio`，而真正的麦是
        #   `usb pnp sound device: audio`）。
        #   我原来只在 `setCurrentIndex` 前后设 `_mic_autoselect` 标志，
        #   但 `addItem` 发生在**更早**，标志还没生效 → 漏了。
        #   ⇒ 用 blockSignals 把整段重建都罩住，最省心也最可靠。
        blocked = self.cmb_mic.blockSignals(True)
        try:
            self.cmb_mic.clear()
            for d in devs:
                self.cmb_mic.addItem("[%d] %s" % (d["index"], d["name"]),
                                     d["index"])
            if not devs:
                self.ctrl.log("没有找到录音设备。插好 USB 声卡后点「刷新」。")
                return
            i = self.cmb_mic.findData(guess_input_device())
            if i >= 0:
                self.cmb_mic.setCurrentIndex(i)
        finally:
            self.cmb_mic.blockSignals(blocked)
        # ★ 顺手打一句日志说明选中了谁 —— 之前"选错设备"完全无声无息，
        #   用户只能看到录不到音（2026-10-08 那次排查绕了很久）。
        try:
            d = devs[i] if i >= 0 else None
            if d:
                self.ctrl.log("🎤 录音设备：%s" % d["name"])
        except Exception:
            pass

    def on_pick_mic(self, idx):
        """用户手动切换麦克风 → **记住它**（换口/重启后索引会变，
        所以存的是设备身份而不是索引）。

        ⚠️ 本方法只会在**用户真的改动下拉框**时被调用：
        `refresh_voice_devices()` 全程 `blockSignals(True)`，
        所以程序填充列表/自动选中都不会走到这里（见那里的说明）。
        """
        if idx < 0:
            return
        name = self.cmb_mic.itemText(idx)
        if not HAS_ASR_MODULE:
            return
        saved = save_preferred_input(name)
        if saved:
            self.ctrl.log("🎤 已记住麦克风：%s（下次自动选它）" % name)
        else:
            self.ctrl.log("⚠ 麦克风选择没能记住：%s" % name)

    def _current_mic(self):
        return self.cmb_mic.currentData()

    def on_load_model(self):
        if not self.asr:
            QMessageBox.warning(self, "无法加载",
                                "语音模块不可用：%s" % (ASR_IMPORT_ERROR or "未知原因"))
            return
        if self.asr.ready:
            return
        self.btn_load_model.setEnabled(False)
        self.lab_asr.setText("加载中…（228MB，约 10–30 秒）")
        self.lab_asr.setStyleSheet("color:%s;" % C_MUTED)
        self.ctrl.log("开始加载语音模型…")
        md = self.txt_model.text().strip() or None
        self._submit(lambda: self.asr.load(md), self._after_load_model)

    def _after_load_model(self, ok, res):
        self.btn_load_model.setEnabled(True)
        if ok:
            self.lab_asr.setText("已就绪 · 断句:%s" % self.asr.vad_mode)
            self.lab_asr.setStyleSheet("color:%s;" % C_GREEN)
            self.btn_ptt.setEnabled(True)
            self.btn_auto.setEnabled(True)
        else:
            self.lab_asr.setText("加载失败")
            self.lab_asr.setStyleSheet("color:%s;" % C_RED)
            self.btn_load_model.setEnabled(True)
            QMessageBox.warning(self, "模型加载失败", res)

    # ── 语音：按住说话 ──────────────────────────────────────────────
    def on_ptt_press(self):
        if not self.asr or not self.asr.ready or self._auto_listen:
            return
        # ★ 关键：本板 USB 麦克风**只有 1 个 subdevice（硬件级互斥）**，
        #   KWS 常驻监听占着它时，直接开录音必然报
        #   "Unanticipated host error -9999: Input/output error"。
        #   所以先把麦克风"租"过来 —— 停 KWS、等它释放、录完再还回去。
        self._mic_yielded = self.ctrl._yield_mic()[0]
        # ★★ 还要把**扬声器**让出来（2026-09-30 修）：
        #   本板播放(card1)与采集(card0)挂在同一 xHCI 控制器上，
        #   **播报期间麦克风完全打不开**（实测 -9999），用户按 F2 会看到
        #   「录音启动失败」，等播报结束才恢复。
        #   → 先丢掉过期的排队播报，再让**扬声器**让位。
        #   ★ 2026-10-11：判据改成 `playback_idle()`（只看声卡，不看合成）——
        #     合成不碰音频设备，把它的 1.11s 算进等待纯属浪费。
        #     配合"合成后丢弃"检查，开麦依然安全。
        #   max_wait 从 8.0 收到 3.0：正常路径几十毫秒就返回，
        #     真等满 3 秒说明声卡被别的进程霸占，再等也没意义。
        if not self.ctrl.playback_idle():
            self.btn_ptt.setText("⏳ 等播报结束…")
            QApplication.processEvents()      # 让上面那行提示立刻显示出来
            quiet, why = self.ctrl.yield_speaker(max_wait=3.0)
            if why:
                self.ctrl.log("🔊 录音前等播报：%s" % why)
            if not quiet:
                self.btn_ptt.setText("按住说话（或按住 F2）")
                self.ctrl.log("录音启动失败：播报还没结束，麦克风被占用"
                              "（本板播放时麦克风打不开）")
                self.ctrl._restore_mic(self._mic_yielded)
                self._mic_yielded = False
                return
        ok, msg = self.asr.ptt_start(device=self._current_mic())
        self._ptt_ok = ok
        if ok:
            self.btn_ptt.setText("● 录音中…松开结束")
        else:
            self.ctrl.log("录音启动失败：%s" % msg)
            # 录音没起来就把麦克风立刻还给 KWS（别让它白白失效）
            self.ctrl._restore_mic(self._mic_yielded)
            self._mic_yielded = False

    def on_ptt_release(self):
        if not self._ptt_ok or not self.asr:
            # 没在录音也要确保 KWS 恢复（例如上面 start 失败的分支已处理）
            return
        self._ptt_ok = False
        self.btn_ptt.setText("识别中…")
        self.btn_ptt.setEnabled(False)
        # 识别要 1–3 秒，必须放后台线程，否则界面会冻住
        self._submit(self._ptt_stop_and_release, self._after_recognize)

    def _ptt_stop_and_release(self):
        """后台线程：结束录音 → 取音频 → **交还麦克风** → 识别。"""
        try:
            res = self.asr.ptt_stop()
        finally:
            # ★ 先还麦克风再识别：识别是纯 CPU 计算，不需要占用录音设备，
            #   早点还回去能让语音急停尽早恢复（安全优先）。
            self.ctrl._restore_mic(getattr(self, "_mic_yielded", False))
            self._mic_yielded = False
        return res

    def _after_recognize(self, ok, res):
        self.btn_ptt.setEnabled(True)
        self.btn_ptt.setText("按住说话（或按住 F2）")
        if ok:
            self.lab_heard.setText("识别结果：%s" % res)
            if self.chk_voice_exec.isChecked():
                self.ctrl.run_text_command(res)
        else:
            self.lab_heard.setText("识别结果：—（%s）" % res)

    # ── 语音：自动聆听 ──────────────────────────────────────────────
    def on_auto_toggle(self, checked):
        if not self.asr or not self.asr.ready:
            self.btn_auto.setChecked(False)
            return
        if checked:
            self._auto_listen = True
            self._auto_exec.clear()          # 干净起步
            self._auto_backoff = 0.0
            self.btn_auto.setText("停止聆听")
            self.btn_ptt.setEnabled(False)
            # ★ 自动聆听是"反复开关麦克风"的循环，而本板麦克风只有 1 个 subdevice，
            #   与 KWS 常驻监听**硬件级互斥** → 在这里整体让位一次
            #   （而不是每句话都让/还，那样既慢又容易在缝隙里失败）。
            self._mic_yielded = self.ctrl._yield_mic()[0]
            if self._mic_yielded:
                self.ctrl.log("自动聆听期间语音急停已让位（停止聆听后自动恢复）")
            self.ctrl.log("自动聆听已开启，直接说话即可（停顿后自动识别）")
            dev = self._current_mic()          # 在界面线程取好设备号
            self._auto_thread = threading.Thread(
                target=self._auto_loop, args=(dev,), daemon=True)
            self._auto_thread.start()
        else:
            self._auto_listen = False
            self._auto_exec.clear()          # ★ 别让残留标记卡住下一轮
            self.btn_auto.setText("开始聆听")
            self.btn_ptt.setEnabled(True)
            # 交还麦克风（在后台做，避免 stop 阻塞界面）
            self._submit(self._auto_stop_and_release, lambda ok, res: None)
            self.ctrl.log("自动聆听已关闭")

    def _auto_stop_and_release(self):
        """后台线程：等自动聆听线程落定 → 交还麦克风给语音急停。"""
        t = getattr(self, "_auto_thread", None)
        if t is not None and t.is_alive():
            try:
                t.join(timeout=6.0)
            except Exception:
                pass
        self._auto_thread = None
        self.ctrl._restore_mic(getattr(self, "_mic_yielded", False))
        self._mic_yielded = False
        return True

    def _auto_loop(self, dev):
        """自动聆听主循环（后台线程）。每轮：等系统安静 → 开麦 → 听一句 → 交给界面线程。

        🔴 2026-10-11 重写。用户报的三个问题都在这个循环里：
          ①「说完话过两三秒才识别到」→ 规则未命中时 LLM 在**界面线程**跑网络，
             冻住界面，结果全堵在队列里 → 已改走 `_dispatch_text`（后台 LLM）；
          ②「车动了几秒才出语音」→ 本循环**不等播报**就立刻又开麦，
             而本板播放/采集抢同一个 subdevice（硬件互斥）→ 播报被顶掉 →
             已加"等 `speak_idle()`"；
          ③「和急停一起开就闪退」→ 开麦失败后**紧密重试**，反复失败的
             AlsaOpen 让 PortAudio **断言 abort**（try/except 抓不住）
             → 已加**指数退避** + `Recorder.start()` 里的 ALSA 空闲预检。
        """
        while self._auto_listen:
            # ① 上一轮的"执行 + 播报"还没结束 → 等它（**不抢占**，用户正在听）
            if self._auto_exec.is_set():
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
                # ② ★★ 失败退避（0.3 → 0.6 → 1.2 → 2.0s 封顶）。
                #    绝不紧密重试：反复失败的 AlsaOpen 会把 PortAudio 打到 abort。
                self._auto_backoff = min(max(self._auto_backoff * 1.8, 0.3), 2.0)
                # "没听到声音/超时"是**正常待机**，别去刷界面也别刷日志
                if ("没听到" not in res) and ("超时" not in res):
                    # ★ 日志节流：同一条错误只报一次（退避 2s 时不然会刷屏）
                    if res != getattr(self, "_auto_last_err", None):
                        self._auto_last_err = res
                        self.ctrl.log("自动聆听受阻：%s（退避重试中）" % res)
                    self._results.put((self._after_auto_result, ok, res))
                time.sleep(self._auto_backoff)
                continue

            # 成功 → 退避清零；先标记"要执行了"，再回传界面线程
            # （标记放在 put **之前**，堵住"界面线程还没处理、循环已开麦"的竞态）
            self._auto_backoff = 0.0
            self._auto_last_err = None
            self._auto_exec.set()
            self._results.put((self._after_auto_result, ok, res))

    def _after_auto_result(self, ok, res):
        """界面线程：处理自动聆听的一轮结果。"""
        if not ok:
            if ("没听到" not in res) and ("超时" not in res):
                self.lab_heard.setText("识别结果：—（%s）" % res)
            self._auto_exec.clear()
            return
        self.lab_heard.setText("识别结果：%s" % res)
        if not self.chk_voice_exec.isChecked():
            self._auto_exec.clear()          # 只看不听 → 立刻回去听下一句
            return
        # ★ 复用生产路径：规则命中同步执行，没命中才后台跑 LLM
        #   `done` 在整条链走完后清标记，聆听循环据此恢复
        self._dispatch_text(res, done=self._auto_exec.clear)

    # ══════════════════════════════════════════════════════
    #  语音播报 / 急停 槽函数
    # ══════════════════════════════════════════════════════
    def on_toggle_speak(self, on):
        self.ctrl.set_speak_enabled(on)

    def on_toggle_announce(self, on):
        self.ctrl.set_announce_actions(on)

    def on_toggle_demo(self, on):
        """离线演示模式：没接小车也能语音应答（不发串口）。"""
        self.ctrl.set_demo_mode(on)
        self._refresh_demo_title()

    def on_toggle_llm(self, on):
        self.ctrl.set_llm_enabled(on)

    def on_toggle_chat(self, on):
        """纯聊天模式：只闲聊，绝不执行指令（防演示时误动车）。"""
        self._chat_only = bool(on)
        self.ctrl.log("纯聊天模式已%s（%s）" % (
            "开启" if on else "关闭",
            "只回话、不执行任何指令" if on else "恢复正常：会执行识别出的指令"))

    def _refresh_demo_title(self):
        """窗口标题反映"离线演示中"，避免误以为车连上了。"""
        base = "%s  v%s" % (APP_NAME, APP_VERSION)
        if self.ctrl.status().get("demo_mode"):
            base = "🎤 离线演示中（未接小车）— " + base
        if self.ctrl.status().get("estop_locked"):
            base = "🔒 急停锁定中 — " + base
        self.setWindowTitle(base)

    def on_toggle_kws(self):
        """开启 / 关闭语音急停（KWS 常开监听）。"""
        if self.btn_kws.isChecked():
            ok, msg = self.ctrl.enable_voice_emergency()
            self.ctrl.log("开启语音急停：%s" % msg)
            if not ok:
                self.btn_kws.setChecked(False)
        else:
            self.ctrl.disable_voice_emergency()

    def on_release_estop(self):
        """
        人工解除急停锁定 —— 这是**唯一的出口**（锁定后所有运动指令都被拒绝）。
        """
        reason = self.ctrl.release_estop()
        if reason:
            self.ctrl.log("已解除急停锁定（原由：%s）" % reason)
        else:
            self.ctrl.log("当前没有急停锁定")

    def _highlight(self, action):
        base = "QPushButton#dirBtn { font-size:15px; font-weight:500; min-height:40px; }"
        hot = ("QPushButton { background:#dbeafe; border:2px solid %s; border-radius:6px;"
               " font-size:15px; font-weight:500; min-height:40px; color:%s; }"
               % (C_BLUE, C_BLUE))
        mapping = {
            "forward": self.btn_fwd, "backward": self.btn_back,
            "turn_left": self.btn_left, "turn_right": self.btn_right,
            "strafe_left": self.btn_sl, "strafe_right": self.btn_sr,
        }
        for a, btn in mapping.items():
            btn.setStyleSheet(hot if a == action else "")

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

        # 连接状态
        if s["connected"]:
            if s["stale"]:
                self.led_conn.setStyleSheet("background:%s;border-radius:6px;" % C_AMBER)
                self.lab_conn.setText("已连接（收不到回传）")
            else:
                self.led_conn.setStyleSheet("background:%s;border-radius:6px;" % C_GREEN)
                self.lab_conn.setText("已连接 %s" % s["port"])
        else:
            self.led_conn.setStyleSheet("background:%s;border-radius:6px;" % C_MUTED)
            self.lab_conn.setText("未连接")

        self.lab_action.setText("动作：%s" % s["action_label"])
        self.lab_speed.setText("目标速度：%d mm/s" % s["speed"])

        # 电压
        v = s["voltage"]
        if s["have_feedback"]:
            if s["volt_level"] == "danger":
                self.lab_volt.setText("电压：%.2f V ⚠ 过低" % v)
                self.lab_volt.setStyleSheet("color:%s; font-weight:500;" % C_RED)
                self.sb_volt.set_value("%.2f" % v, C_RED)
            elif s["volt_level"] == "warn":
                self.lab_volt.setText("电压：%.2f V ⚠ 偏低" % v)
                self.lab_volt.setStyleSheet("color:%s;" % C_AMBER)
                self.sb_volt.set_value("%.2f" % v, C_AMBER)
            else:
                self.lab_volt.setText("电压：%.2f V" % v)
                self.lab_volt.setStyleSheet("")
                self.sb_volt.set_value("%.2f" % v, C_GREEN)
        else:
            self.lab_volt.setText("电压：—")
            self.lab_volt.setStyleSheet("")

        # 急停标志
        if s["have_feedback"]:
            if s["flag_stop"]:
                self.lab_stopflag.setText("急停标志：已触发")
                self.lab_stopflag.setStyleSheet("color:%s; font-weight:500;" % C_RED)
            else:
                self.lab_stopflag.setText("急停标志：正常")
                self.lab_stopflag.setStyleSheet("color:%s;" % C_GREEN)

        # 速度与 IMU
        self.sb_vx.set_value(s["vx"])
        self.sb_vy.set_value(s["vy"])
        self.sb_vz.set_value(s["vz"])
        ax, ay, az = s["accel_ms2"]
        self.sb_ax.set_value("%.2f" % ax)
        self.sb_ay.set_value("%.2f" % ay)
        self.sb_az.set_value("%.2f" % az)
        self.sb_gz.set_value("%.3f" % s["gyro_rads"][2])

        # 日志
        lines = self.ctrl.drain_logs()
        if lines:
            for ln in lines:
                self.log.appendPlainText(ln)
            sb = self.log.verticalScrollBar()
            sb.setValue(sb.maximum())

        # 语音播报 / 语音急停 / 急停锁定
        self._refresh_speech(s)

        # 语音输入电平（录音或自动聆听时才刷新）
        if self.asr is not None:
            if self.asr.recording:
                self.bar_level.setValue(int(min(100.0, self.asr.level * 500.0)))
            elif self.bar_level.value() != 0:
                self.bar_level.setValue(0)

    # ══════════════════════════════════════════════════════
    #  语音播报 / 急停 的界面刷新
    # ══════════════════════════════════════════════════════
    @staticmethod
    def _led(lab, color):
        lab.setStyleSheet("background:%s;border-radius:6px;" % color)

    def _refresh_speech(self, s):
        """刷新 TTS 播报灯、语音急停灯、急停锁定条、网络音箱地址。"""
        # ── ① TTS 播报状态 ──
        ts = self.ctrl.tts_status()
        st = ts["state"]
        if st == "ready":
            self._led(self.led_tts, C_GREEN)
            pend = "（播报中 %d 条）" % ts["pending"] if ts["pending"] else ""
            self.lab_tts.setText("播报：就绪　%s%s" % (ts["msg"], pend))
            self.lab_tts.setStyleSheet("color:%s;" % C_TEXT)
        elif st == "loading":
            self._led(self.led_tts, C_AMBER)
            self.lab_tts.setText("播报：正在加载模型…（约 5~7 秒）")
            self.lab_tts.setStyleSheet("color:%s;" % C_AMBER)
        elif st == "failed":
            self._led(self.led_tts, C_RED)
            self.lab_tts.setText("播报：不可用 —— %s" % ts["msg"])
            self.lab_tts.setStyleSheet("color:%s;" % C_RED)
        else:
            self._led(self.led_tts, C_MUTED)
            self.lab_tts.setText("播报：未启用")
            self.lab_tts.setStyleSheet("color:%s;" % C_MUTED)

        # 没连小车时的状态要在"输出"这一行说清楚，否则会以为车连上了
        if s.get("demo_mode") and not s.get("connected"):
            self.lab_web.setText("🔌 离线演示中（未接小车）：指令只语音应答，不发串口")
            self.lab_web.setStyleSheet("color:%s;" % C_AMBER)
        elif not s.get("connected"):
            self.lab_web.setText("未连接小车（下方可开「离线演示模式」）")
            self.lab_web.setStyleSheet("color:%s;" % C_MUTED)
        elif ts["has_output"]:
            self.lab_web.setText("板载喇叭（USB 音箱）")
            self.lab_web.setStyleSheet("color:%s;" % C_TEXT)
        elif ts["web_url"]:
            self.lab_web.setText("%s　← 浏览器打开当喇叭" % ts["web_url"])
            self.lab_web.setStyleSheet("color:%s;" % C_TEXT)
        else:
            self.lab_web.setText("无（没喇叭，网络音箱也没起来）")
            self.lab_web.setStyleSheet("color:%s;" % C_MUTED)

        # ── ② 语音急停（KWS）──
        vs = self.ctrl.voice_status()
        kst = vs["state"]
        # ★ 我们是**主动让位**给录音的吗？那期间"链路断开"是预期行为，不是故障。
        #   （2026-09-29：本板麦克风只有 1 个 subdevice，录音与 KWS 硬件级互斥，
        #    所以录音前必须让 KWS 停一会儿 —— 不该因此弹红字吓用户。）
        yielding = bool(getattr(self.ctrl, "_kws_yielded", False))
        if kst == "ready":
            if yielding:
                self._led(self.led_kws, C_AMBER)
                self.lab_kws.setText("语音急停：录音中暂停（松开后自动恢复）")
                self.lab_kws.setStyleSheet("color:%s;" % C_AMBER)
                self._kws_warned = False      # 主动让位不算告警，别留下告警状态
            elif vs.get("opening"):
                # ★ 正在重连麦克风（多半刚播报完）：**不是故障**，别报红字。
                #   本板播放/采集硬件互斥，播报期间打不开是正常的瞬时状态。
                self._led(self.led_kws, C_AMBER)
                self.lab_kws.setText("语音急停：正在恢复监听…")
                self.lab_kws.setStyleSheet("color:%s;" % C_AMBER)
                self._kws_warned = False
            elif vs["watchdog_ok"] is False:
                # ★ 麦克风链路死了 —— 这时候"语音急停"已经名存实亡，必须红字报警
                self._led(self.led_kws, C_RED)
                self.lab_kws.setText("语音急停：⚠️ 麦克风链路已断！急停已失效")
                self.lab_kws.setStyleSheet("color:%s;font-weight:600;" % C_RED)
                if not self._kws_warned:
                    self._kws_warned = True
                    self.ctrl.log("⚠️ 语音急停的麦克风链路已断开 —— "
                                  "语音急停已失效，请检查麦克风！")
            else:
                self._led(self.led_kws, C_GREEN)
                self.lab_kws.setText("语音急停：监听中　%s" % vs["msg"])
                self.lab_kws.setStyleSheet("color:%s;" % C_TEXT)
                self._kws_warned = False
            if not self.btn_kws.isChecked():
                self.btn_kws.setChecked(True)
            self.btn_kws.setText("关闭语音急停")
        elif kst == "loading":
            self._led(self.led_kws, C_AMBER)
            self.lab_kws.setText("语音急停：正在启动…")
            self.lab_kws.setStyleSheet("color:%s;" % C_AMBER)
        elif kst == "failed":
            self._led(self.led_kws, C_RED)
            self.lab_kws.setText("语音急停：不可用 —— %s" % vs["msg"])
            self.lab_kws.setStyleSheet("color:%s;" % C_RED)
            if self.btn_kws.isChecked():
                self.btn_kws.setChecked(False)
            self.btn_kws.setText("开启语音急停")
        else:
            self._led(self.led_kws, C_MUTED)
            self.lab_kws.setText("语音急停：未启用")
            self.lab_kws.setStyleSheet("color:%s;" % C_MUTED)
            if self.btn_kws.isChecked():
                self.btn_kws.setChecked(False)
            self.btn_kws.setText("开启语音急停")

        n = s.get("voice_hit_count", 0)
        self.lab_hits.setText("命中 %d 次" % n)
        self.bar_mic.setValue(int(min(100.0, (vs["level"] or 0.0) * 500.0)))

        # ── ③ LLM（指令纠错 + 对话）—— 本地或云端 ──
        ls = self.ctrl.llm_status()
        lst = ls.get("state")
        # 后端标签：云端就写"云端"，本地就写"本地"（早期一律写"本地"，
        # 接上云端后会显示成"本地 LLM：glm-4-flash"，误导 —— 2026-09-29 修）
        _bk = ls.get("mode")
        _tag = "云端 LLM" if _bk == "api" else "本地 LLM"
        if lst == "ready":
            if not ls.get("enabled"):
                self._led(self.led_llm, C_MUTED)
                self.lab_llm.setText("%s：已就绪但被关闭" % _tag)
                self.lab_llm.setStyleSheet("color:%s;" % C_MUTED)
            else:
                self._led(self.led_llm, C_GREEN)
                stats = ls.get("stats") or {}
                extra = ""
                if stats.get("calls"):
                    extra = "　已问 %d 次/缓存命中 %d" % (stats["calls"], stats.get("hits", 0))
                if getattr(self, "_chat_only", False):
                    extra += "　【纯聊天模式】"
                self.lab_llm.setText("%s：%s%s" % (_tag, ls.get("model") or "就绪", extra))
                self.lab_llm.setStyleSheet("color:%s;" % C_TEXT)
                # 提示"这句话没有控车词" —— 免得用户以为 LLM 坏了
                # （有关键词闸门在，没有证据的动作一律被丢弃，见 llm_engine.ACTION_EVIDENCE）
                txt = self.txt_cmd.text().strip()
                if txt and HAS_LLM and not looks_like_command(txt):
                    self.lab_llm.setText(
                        "%s：这句话里没有控车词，只会当闲聊处理" % _tag)
                    self.lab_llm.setStyleSheet("color:%s;" % C_MUTED)
        elif lst == "failed":
            self._led(self.led_llm, C_AMBER)   # 不是错误：没有它是允许的
            self.lab_llm.setText("LLM：不可用（规则解析照常工作）")
            self.lab_llm.setStyleSheet("color:%s;" % C_AMBER)
            if self.chk_llm.isChecked():
                self.chk_llm.setChecked(False)
        else:
            self._led(self.led_llm, C_MUTED)
            self.lab_llm.setText("LLM：未启用")
            self.lab_llm.setStyleSheet("color:%s;" % C_MUTED)

        # 对话记录（只在有新增时重建，避免每 100ms 刷新抢走滚动位置）
        reps = ls.get("replies") or []
        if len(reps) != getattr(self, "_chat_shown", -1):
            self._chat_shown = len(reps)
            self.lst_chat.clear()
            for u, a in reps[-6:]:
                self.lst_chat.addItem("你：%s" % u)
                self.lst_chat.addItem("车：%s" % a)

        # ── ③ 急停锁定条 ──
        # 锁定时顶到最显眼的位置，并让窗口标题也带上，避免被忽略
        if s["estop_locked"]:
            if not self.bar_estop.isVisible():
                self.bar_estop.setVisible(True)
            self.lab_estop_lock.setText(
                "🔒 急停锁定中 —— %s（已拒绝一切运动指令）" % s["estop_reason"])
        else:
            if self.bar_estop.isVisible():
                self.bar_estop.setVisible(False)
        # 标题统一由 _refresh_demo_title 维护（急停锁定 + 离线演示两种前缀）
        self._refresh_demo_title()

        # ── ④ 语音事件（命中 / 就绪 / 断链）──
        for kind, detail, _ts in self.ctrl.poll_voice_events():
            if kind == "estop":
                self.ctrl.log("★ 语音急停！%s —— 已停车并进入锁定态" % detail)
            elif kind == "estop_released":
                self.ctrl.log("急停锁定已解除（原由：%s）" % detail)
            else:
                self.ctrl.log("[语音] %s：%s" % (kind, detail))

    # ══════════════════════════════════════════════════════
    #  键盘
    # ══════════════════════════════════════════════════════
    KEY_MAP = {
        Qt.Key_W: "forward", Qt.Key_Up: "forward",
        Qt.Key_S: "backward", Qt.Key_Down: "backward",
        Qt.Key_A: "turn_left", Qt.Key_Left: "turn_left",
        Qt.Key_D: "turn_right", Qt.Key_Right: "turn_right",
        Qt.Key_Q: "strafe_left",
        Qt.Key_E: "strafe_right",
    }

    #: ★ 「按住说话」可以按的键（2026-10-11 扩充）。
    #:
    #: 🔴 为什么不止 F2：用户想用**便宜的无线遥控设备**远程触发录音
    #:    （2.4G 无线键盘/翻页笔/脚踏开关），但那些设备发的是**固定的几个键**，
    #:    各不相同、且大多不可改。多认几个就能开箱即用：
    #:      · F2                    —— 默认，也是板载键盘最顺手的
    #:      · PageUp / PageDown     —— 无线**翻页笔**（演讲笔）最常发的两个键
    #:      · F13~F15               —— USB **脚踏开关**最常见的映射（不少还能改）
    #:      · Insert / Pause        —— 部分遥控小键盘/脚踏板
    #:      · Enter                 —— 数字小键盘回车（无线小键盘好按）
    #:    ⚠️ 刻意避开已占用的键：Space(急停)、Esc、F11(全屏)、方括号(调速)、
    #:      方向键/WASD(手动控制)、数字键。
    #:
    #: ⚠️ **必须用 `getattr` 取**（2026-10-11 自己踩的坑）：
    #:    一开始写了 `Qt.Key_KP_Enter`，但 PySide6 6.8 **没有这个常量** ——
    #:    类体在 import 时就求值，于是**整个模块 import 失败、界面起不来**。
    #:    `py_compile` 查不出（它是运行期的属性访问）。
    #:    ⇒ 取不到就跳过，任何 Qt 版本都不会因此崩。
    PTT_KEYS = frozenset(
        v for v in (
            getattr(Qt, "Key_F2", None),
            getattr(Qt, "Key_PageUp", None),
            getattr(Qt, "Key_PageDown", None),
            getattr(Qt, "Key_F13", None),
            getattr(Qt, "Key_F14", None),
            getattr(Qt, "Key_F15", None),
            getattr(Qt, "Key_Insert", None),
            getattr(Qt, "Key_Pause", None),
            getattr(Qt, "Key_Enter", None),          # 小键盘回车
            getattr(Qt, "Key_ScrollLock", None),
        ) if v is not None
    )

    def keyPressEvent(self, event):
        if event.isAutoRepeat():
            return
        if self.txt_cmd.hasFocus():
            super().keyPressEvent(event)
            return

        key = event.key()
        if key == Qt.Key_Escape:
            if self.isFullScreen():
                self.showNormal()
                return
        if key == Qt.Key_F11:
            self.showNormal() if self.isFullScreen() else self.showFullScreen()
            return
        if key in self.PTT_KEYS:
            # 用非 F2 的键触发时提示一下，方便用户确认自己的遥控设备生效了
            if key != Qt.Key_F2:
                try:
                    self.ctrl.log("🎤 按住说话（键 %s）"
                                  % QKeySequence(key).toString())
                except Exception:
                    pass
            self.on_ptt_press()
            return
        if key == Qt.Key_Space:
            self.on_stop()
            return
        if key == Qt.Key_BracketRight:
            self.set_speed(self.ctrl.speed + SPEED_STEP)
            return
        if key == Qt.Key_BracketLeft:
            self.set_speed(self.ctrl.speed - SPEED_STEP)
            return
        if key in self.KEY_MAP and self.ctrl.connected:
            act = self.KEY_MAP[key]
            if act not in self._hold_actions:
                self.on_dir_press(act)
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.isAutoRepeat():
            return
        if self.txt_cmd.hasFocus():
            super().keyReleaseEvent(event)
            return
        key = event.key()
        if key in self.PTT_KEYS:
            self.on_ptt_release()
            return
        if key in self.KEY_MAP and self.ctrl.connected:
            self.on_dir_release(self.KEY_MAP[key])
            return
        super().keyReleaseEvent(event)

    # ══════════════════════════════════════════════════════
    def closeEvent(self, event):
        try:
            self.timer.stop()
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
        event.accept()


def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--port", default=None, help="串口设备名，默认自动探测")
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    ap.add_argument("--model-dir", default=None, help="语音模型目录")
    ap.add_argument("--fullscreen", action="store_true", help="启动即全屏")
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

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(STYLESHEET)

    fam = pick_cjk_font()
    if fam:
        f = QFont(fam)
        f.setPointSize(10)
        app.setFont(f)

    win = MainWindow(port=args.port, baud=args.baud, model_dir=args.model_dir,
                     auto_tts=not args.no_tts, demo=args.demo,
                     auto_llm=not args.no_llm, llm_mode=args.llm_mode)
    if args.fullscreen:
        win.showFullScreen()
    else:
        win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
