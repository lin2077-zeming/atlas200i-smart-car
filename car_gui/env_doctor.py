#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
env_doctor.py —— 找出「该用哪个 python3 跑小车脚本」

为什么需要这个
--------------
本板（Atlas 200I DK A2）上同时存在多套 Python：

    /usr/bin/python3                        系统自带（3.10，Ubuntu 22.04）
    /home/HwHiAiUser/miniconda3/bin/python3 conda base（3.9，装 ASR 依赖的那套）
    /home/HwHiAiUser/miniconda3/envs/xxx/... 其它 conda 环境

依赖（numpy / sounddevice / sherpa-onnx）只装在其中**一个**环境里。
如果你 conda 激活了 base，而依赖恰好装在系统 python3，就会看到：

    缺少 sounddevice。

这个脚本不做安装，只负责**告诉你哪把解释器能用**，以及把可用解释器
写进 `car_gui/.python_for_car`，让其它脚本（install_deps.sh / run_gui.sh /
kws_record.py …）自动跟着走。

用法
----
    python3 env_doctor.py             # 扫描并给出建议
    python3 env_doctor.py --write     # 扫描并把最佳解释器写进 .python_for_car
    python3 env_doctor.py --json      # 机器可读输出
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PIN_FILE = os.path.join(HERE, ".python_for_car")

# 关心三组依赖：手动控车 / 语音识别 / 图形界面
MODULES = [
    ("serial", "pyserial"),
    ("numpy", "numpy"),
    ("sounddevice", "sounddevice"),
    ("sherpa_onnx", "sherpa-onnx"),
    ("PySide6", "PySide6"),
]

# 探测顺序：先常用位置，再 PATH 里能找到的
CANDIDATE_GLOBS = [
    "/usr/bin/python3",
    "/usr/local/bin/python3",
    # ⚠️ 本板实测：conda 装在 /usr/local/miniconda3（不是 ~/miniconda3），
    #    且 .bashrc 里有 `conda init` → 交互登录会**自动激活 base**，
    #    python3 于是指向 3.9.2（没有 sounddevice）。这是最高频的踩坑点，
    #    所以这里把 /usr/local 与 /opt 两个位置都覆盖到。
    "/usr/local/miniconda3/bin/python3",
    "/usr/local/anaconda3/bin/python3",
    "/usr/local/conda/bin/python3",
    "/opt/miniconda3/bin/python3",
    "/opt/anaconda3/bin/python3",
    "/opt/conda/bin/python3",
    os.path.expanduser("~/miniconda3/bin/python3"),
    os.path.expanduser("~/anaconda3/bin/python3"),
]

# conda 环境的扫描根目录（envs/*/bin/python3）
CONDA_ROOTS = [
    "/usr/local/miniconda3", "/usr/local/anaconda3", "/usr/local/conda",
    "/opt/miniconda3", "/opt/anaconda3", "/opt/conda",
    os.path.expanduser("~/miniconda3"), os.path.expanduser("~/anaconda3"),
]


def _probe(py):
    """返回该解释器的依赖清单 + 版本；解释器不存在返回 None。"""
    if not py or not os.path.exists(py):
        return None
    code = (
        "import json,sys\n"
        "mods=%r\n"
        "out={'version':'%%d.%%d.%%d'%%sys.version_info[:3],\n"
        "     'executable':sys.executable,\n"
        "     'prefix':sys.prefix,\n"
        "     'conda':__import__('os').environ.get('CONDA_PREFIX',''),\n"
        "     'mods':{}}\n"
        "import importlib\n"
        "for m,_ in mods:\n"
        "    try:\n"
        "        mod=importlib.import_module(m)\n"
        "        out['mods'][m]=getattr(mod,'__version__','?')\n"
        "    except Exception as e:\n"
        "        out['mods'][m]=None\n"
        "print(json.dumps(out))\n" % (MODULES,)
    )
    try:
        r = subprocess.run([py, "-c", code], capture_output=True, text=True,
                           timeout=25)
    except Exception:
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        d = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return None
    d["path"] = py
    return d


def _score(d):
    """越大越好。语音链路（sherpa+sounddevice）权重最高。"""
    m = d["mods"]
    s = 0
    s += 100 if m.get("sherpa_onnx") else 0
    s += 40 if m.get("sounddevice") else 0
    s += 20 if m.get("numpy") else 0
    s += 10 if m.get("serial") else 0
    s += 5 if m.get("PySide6") else 0
    # 同分时偏好系统 python3（不依赖 conda 激活状态，最省心）
    if d["path"] in ("/usr/bin/python3", "/usr/local/bin/python3"):
        s += 2
    if "/envs/" in d["path"]:
        s -= 1          # conda 附属环境每次都得起名字，麻烦一点
    return s


def _which_pythons():
    found = []
    for p in CANDIDATE_GLOBS:
        if os.path.exists(p) and p not in found:
            found.append(p)
    for name in ("python3.10", "python3.9", "python3", "python"):
        p = shutil.which(name)
        if p and p not in found:
            found.append(p)
    # conda 环境们（含 /usr/local 与 /opt 下的安装）
    for base in CONDA_ROOTS:
        envs = os.path.join(base, "envs")
        if os.path.isdir(envs):
            for e in sorted(os.listdir(envs)):
                p = os.path.join(envs, e, "bin", "python3")
                if os.path.exists(p) and p not in found:
                    found.append(p)
    return found


def _read_pin():
    try:
        with open(PIN_FILE) as f:
            p = f.read().strip()
        return p or None
    except Exception:
        return None


def _write_pin(path):
    with open(PIN_FILE, "w") as f:
        f.write(path + "\n")


def _mark(ok):
    return "✅" if ok else "  "


def main():
    ap = argparse.ArgumentParser(description="找出该用哪个 python3 跑小车脚本")
    ap.add_argument("--write", action="store_true",
                    help="把最佳解释器写进 .python_for_car")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    print("=" * 74)
    print("  Python 环境体检（Atlas 200I DK A2）")
    print("=" * 74)
    print("  当前解释器      : %s" % sys.executable)
    print("  版本            : %d.%d.%d" % sys.version_info[:3])
    print("  CONDA_PREFIX    : %s" % (os.environ.get("CONDA_PREFIX") or "-"))
    print("  VIRTUAL_ENV     : %s" % (os.environ.get("VIRTUAL_ENV") or "-"))
    print("  已固定(文件)    : %s" % (_read_pin() or "（无）"))
    print()

    cands = []
    for p in _which_pythons():
        d = _probe(p)
        if d:
            d["score"] = _score(d)
            cands.append(d)

    if not cands:
        print("  ❌ 没扫到任何可用的 Python 解释器")
        return 1

    cands.sort(key=lambda x: -x["score"])

    # 表头
    head = "  %-42s %-9s" % ("解释器", "版本")
    for _, label in MODULES:
        head += " %-14s" % label
    head += " 分"
    print(head)
    print("  " + "-" * (len(head) + 20))
    for d in cands:
        line = "  %-42s %-9s" % (d["path"], d["version"])
        for m, _ in MODULES:
            v = d["mods"].get(m)
            line += " %-14s" % (("v%s" % v)[:13] if v else "—")
        line += " %d" % d["score"]
        print(line)

    best = cands[0]
    print()
    print("=" * 74)
    print("  建议使用的解释器")
    print("=" * 74)
    print("  %s   （Python %s）" % (best["path"], best["version"]))
    miss = [label for m, label in MODULES if not best["mods"].get(m)]
    if miss:
        print("  仍缺：%s" % ", ".join(miss))
        print("  补齐命令：")
        print("      %s -m pip install -U %s \\" % (
            best["path"], " ".join(m for m, _ in MODULES if not best["mods"].get(m))))
        print("          -i https://pypi.tuna.tsinghua.edu.cn/simple")
    else:
        print("  三组依赖齐全 ✅")

    # 当前解释器是不是最佳？
    cur_ok = any(d["path"] == sys.executable for d in cands[:1])
    print()
    if cur_ok and not miss:
        print("  ✅ 你现在这个解释器就是最佳的，直接跑：")
        print("       python3 kws_record.py --diag")
    else:
        print("  ⚠️  你当前这个解释器不是最佳的（或缺依赖）。改用：")
        print("       %s kws_record.py --diag" % best["path"])
        print("     或者先固定下来（之后所有脚本自动跟随）：")
        print("       %s env_doctor.py --write" % best["path"])

    r = _read_pin()
    if r and r != best["path"]:
        print()
        print("  ⚠️  .python_for_car 里固定的是 %s，与建议不一致，建议重跑 --write" % r)

    if args.write:
        _write_pin(best["path"])
        print()
        print("  ✅ 已写入 %s" % PIN_FILE)
        print("     内容：%s" % best["path"])

    if args.json:
        print()
        print(json.dumps({"best": best, "all": cands}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
