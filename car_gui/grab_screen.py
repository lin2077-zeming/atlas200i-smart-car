#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把板上**当前屏幕**抓成 PNG（不用显示器也能抓：抓的是 X 服务器的画面）。

用途：验收 / 写文档时证明"屏上真的画出来了"。
比手机拍照片准 —— 没有反光、没有摩尔纹，还能直接量像素。

    # 在板子桌面/VNC 终端里
    bash run_py.sh grab_screen.py /tmp/shot.png

    # 或从电脑上（板子 IP 换成你自己的）
    ssh HwHiAiUser@192.168.31.128 'DISPLAY=:0 XAUTHORITY=~/.Xauthority \
        /usr/bin/python3 ~/car_gui/grab_screen.py /tmp/shot.png'

注意：抓的是**整屏**（含任务栏），所以它也能用来确认展示屏是不是真的全屏盖满了。
"""
import sys

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

app = QApplication(sys.argv[:1])
scr = QGuiApplication.primaryScreen()
if scr is None:
    print("[错误] 没有屏幕（DISPLAY 没设？）")
    sys.exit(1)
geo = scr.geometry()
pix = scr.grabWindow(0)
out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/shot.png"
ok = pix.save(out)
print("屏幕 %dx%d 缩放 %.2f → 已保存 %s（%dx%d，%s）"
      % (geo.width(), geo.height(), scr.devicePixelRatio(), out,
         pix.width(), pix.height(), "OK" if ok else "失败"))