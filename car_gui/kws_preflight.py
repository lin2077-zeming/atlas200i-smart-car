#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kws_preflight.py —— KWS 语音急停环境预检（L0~L5）

对标 asr_preflight.py 的风格：分层体检，硬失败=0 即环境侧无遗留。

层级
  L0 依赖        numpy / sounddevice / sherpa_onnx
  L1 模型文件     encoder/decoder/joiner/tokens 四个文件齐全 + 大小合理
  L2 关键词表     静态体检（# 注释 / 缺 @ / 拼音非法字符 / 单字词）
  L3 模型加载     KeywordSpotter 能否建起来 + 耗时
  L4 官方样本     用官方 test_wavs + 官方关键词表验证引擎真的能命中
  L5 麦克风       声卡存在 + 增益 > 0 + 实时监听能启动

设计要点
  · 声卡/麦克风缺失算 **soft**（软失败），不计入退出码——
    因为 KWS 代码可以在无声卡时先写好、用 wav 验证。
  · L4 是**关键的真值校验**：官方 test_wavs 是真人录音，配套官方关键词表，
    如果这里不命中，说明引擎接线有问题（而不是关键词写错）。

用法
    python3 kws_preflight.py                 # 全量
    python3 kws_preflight.py --no-mic        # 跳过 L5（麦克风）
    python3 kws_preflight.py --threshold 0.20
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kws_engine as K                                    # noqa: E402

_results = []


def item(name, ok, note="", soft=False):
    """记一条检查项。soft=True 时失败不计入硬失败。"""
    mark = "✅" if ok else ("⏳" if soft else "❌")
    _results.append((name, ok, note, soft))
    line = "  %s %-34s" % (mark, name)
    if note:
        line += " %s" % note
    print(line)


def section(title):
    print("\n" + "─" * 70)
    print("  %s" % title)
    print("─" * 70)


# ══════════════════════════════════════════════════════════════════════
#  L0 依赖
# ══════════════════════════════════════════════════════════════════════
def check_interpreter():
    """
    检查「是不是在用对的那套 Python」。

    本板多套 Python 并存，依赖只装在其中一套里 —— 这是实测最高频的
    假故障来源（`缺少 sounddevice` / `No module named sherpa_onnx`）。
    所以在 L0 之前先报一次解释器身份，出问题时一眼能看出来。
    """
    section("L0- 解释器")
    import os
    import sys as _sys
    item("当前解释器", True, _sys.executable)
    ver = "%d.%d.%d" % _sys.version_info[:3]
    cond = os.environ.get("CONDA_PREFIX")
    item("Python 版本", True, ver + (("  COND=%s" % cond) if cond else ""))

    pin = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       ".python_for_car")
    if os.path.isfile(pin):
        try:
            fixed = open(pin).read().strip()
        except Exception:
            fixed = ""
        same = (os.path.realpath(fixed) == os.path.realpath(_sys.executable)
                if fixed else False)
        item(".python_for_car", same,
             ("已固定：%s" % fixed) if same else
             ("固定的是 %s，但当前用的是 %s → 请用 bash run_py.sh 运行"
              % (fixed or "(空)", _sys.executable)))
    else:
        item(".python_for_car", True, "未固定（可选）→ bash run_py.sh env_doctor.py --write")


def check_deps():
    section("L0 依赖")
    st = K.dependency_status()
    item("numpy", st["numpy"], "" if st["numpy"] else "缺 → pip install numpy")
    item("sounddevice", st["sounddevice"],
         "" if st["sounddevice"] else "缺 → apt install libportaudio2 + pip install sounddevice")
    item("sherpa_onnx", st["sherpa_onnx"],
         ("v%s" % st.get("version", "?")) if st["sherpa_onnx"]
         else ("缺：%s" % st["kws_error"]))
    ok = st["numpy"] and st["sounddevice"] and st["sherpa_onnx"]
    if not ok:
        need = [m for m in ("numpy", "sounddevice", "sherpa_onnx") if not st.get(m)]
        try:
            import asr_engine as _E
            _, hint = _E.dependency_hint(need)
            print()
            print(hint)
        except Exception:
            print("  提示：先跑 bash install_deps.sh；"
                  "或用 bash run_py.sh env_doctor.py 确认解释器。")
    return ok


# ══════════════════════════════════════════════════════════════════════
#  L1 模型文件
# ══════════════════════════════════════════════════════════════════════
def check_model_files(model_dir=None):
    section("L1 模型文件")
    md = model_dir or K.guess_kws_dir()
    print("  模型目录：%s" % md)
    enc, dec, joi, tok = K.find_kws_files(md)

    specs = (("encoder", enc, 8, 15),       # 期望 8~15 MB
             ("decoder", dec, 0.3, 1.5),
             ("joiner", joi, 0.1, 0.5))
    all_ok = True
    for name, path, lo, hi in specs:
        if not path:
            item("%s 文件" % name, False, "缺失")
            all_ok = False
            continue
        mb = os.path.getsize(path) / 1048576.0
        good = lo <= mb <= hi
        item("%s 文件" % name, good, "%.2f MB%s" % (
            mb, "" if good else " ← 大小异常（期望 %.1f~%.1f MB）" % (lo, hi)))
        all_ok = all_ok and good
        if "int8" in os.path.basename(path):
            item("%s 是 int8" % name, False,
                 "本板不支持 int8（会报 attention_dims），必须用 FP32")
            all_ok = False

    if tok:
        item("tokens.txt", True, "%.1f KB" % (os.path.getsize(tok) / 1024.0))
    else:
        item("tokens.txt", False, "缺失")
        all_ok = False

    tw = os.path.join(md, "test_wavs")
    if os.path.isdir(tw):
        n = len([f for f in os.listdir(tw) if f.endswith(".wav")])
        item("test_wavs（官方验证样本）", n >= 4, "%d 个 wav" % n)
    else:
        item("test_wavs（官方验证样本）", False, "缺失 → L4 无法验证", soft=True)

    return md, all_ok


# ══════════════════════════════════════════════════════════════════════
#  L2 关键词表
# ══════════════════════════════════════════════════════════════════════
def check_keywords(path=None):
    section("L2 关键词表")
    kw = path or K.DEFAULT_KEYWORDS_FILE
    print("  关键词文件：%s" % kw)
    if not os.path.isfile(kw):
        item("文件存在", False, "找不到")
        return False

    item("文件存在", True)
    kws = K.load_keywords(kw)
    print("     共 %d 个关键词：" % len(kws))
    for lb, py in kws:
        print("       %-10s <= %s" % (lb, py))

    problems = K.check_keywords_file(kw)
    fatal = [p for p in problems if "单字词" not in p[2]]
    warn_only = [p for p in problems if "单字词" in p[2]]

    if fatal:
        for ln, txt, why in fatal:
            item("第%d行" % ln, False, "%r → %s" % (txt, why))
        return False
    item("格式合法（无 # 注释 / 有 @ / 拼音字符合法）", True)

    if warn_only:
        for ln, txt, why in warn_only:
            item("第%d行 单字词风险" % ln, True, "⚠️  %s" % why, soft=True)

    # 安全检查：有没有单字"停"
    single = [lb for lb, py in kws if len(py.split()) < 2]
    if single:
        item("不含单字急停词（安全）", False,
             "发现：%s（日常对话误触发风险高）" % "、".join(single))
    else:
        item("不含单字急停词（安全）", True)

    return True


# ══════════════════════════════════════════════════════════════════════
#  L3 模型加载
# ══════════════════════════════════════════════════════════════════════
def check_load(model_dir=None, keywords=None, threads=K.DEFAULT_THREADS,
               threshold=K.DEFAULT_THRESHOLD):
    section("L3 模型加载")
    eng = K.KwsEngine()
    t0 = time.time()
    ok, msg = eng.load(kws_dir=model_dir, keywords_file=keywords,
                       num_threads=threads, threshold=threshold)
    cost = time.time() - t0
    if not ok:
        item("KeywordSpotter 构建", False, msg)
        return None
    item("KeywordSpotter 构建", True, "%.2f 秒" % cost)
    item("关键词数量", len(eng.keywords) > 0, "%d 个" % len(eng.keywords))
    if cost > 10:
        item("加载耗时", False, "%.1f 秒偏慢（正常 2~4 秒）" % cost)
    else:
        item("加载耗时", True, "%.2f 秒" % cost)
    return eng


# ══════════════════════════════════════════════════════════════════════
#  L4 官方样本验证（真值校验）
# ══════════════════════════════════════════════════════════════════════
def check_official_samples(model_dir=None, threshold=K.DEFAULT_THRESHOLD):
    section("L4 官方样本验证（真人录音，真值校验）")
    md = model_dir or K.guess_kws_dir()
    tw = os.path.join(md, "test_wavs")
    kw = os.path.join(tw, "test_keywords.txt")
    if not os.path.isfile(kw):
        item("官方关键词表", False, "找不到 %s" % kw, soft=True)
        return False

    eng = K.KwsEngine()
    ok, msg = eng.load(kws_dir=md, keywords_file=kw, threshold=threshold)
    if not ok:
        item("加载官方关键词表", False, msg)
        return False
    item("加载官方关键词表", True, "%d 个词" % len(eng.keywords))

    wavs = sorted(f for f in os.listdir(tw) if f.endswith(".wav"))
    n_hit = 0
    for f in wavs:
        hits = eng.detect_file(os.path.join(tw, f))
        if hits:
            n_hit += 1
        print("       %-10s → %s" % (f, hits if hits else "（无命中）"))

    # 官方 7 个 wav 里，3/4/5/6 含关键词，0/1/2 是负样本
    # 只要"至少 2 个命中"，就说明引擎接线正常
    good = n_hit >= 2
    item("引擎能命中真人样本", good,
         "%d/%d 个 wav 命中%s" % (n_hit, len(wavs),
                                  "" if good else " ← 引擎接线可能有问题！"))
    return good


# ══════════════════════════════════════════════════════════════════════
#  L5 麦克风
# ══════════════════════════════════════════════════════════════════════
def check_microphone(engine=None, live_seconds=0.0):
    section("L5 麦克风")
    devs = K.list_input_devices()
    if not devs:
        item("录音设备", False, "一个都没有（板子无音频硬件，需 USB 声卡）", soft=True)
        return False

    # 只有 hw: 开头的才是真硬件，其余是 ALSA 插件
    real = [d for d in devs if "hw:" in d["name"]]
    item("录音设备", len(real) > 0,
         "%d 个（其中真硬件 %d 个）" % (len(devs), len(real)),
         soft=len(real) == 0)
    if not real:
        print("       ⚠️  列出的都是 ALSA 软件插件，没有真实录音硬件")
        return False

    for d in real:
        print("       [%d] %-38s %dHz" % (d["index"], d["name"], d["samplerate"]))

    dev = K.guess_input_device()
    item("自动选中设备", dev is not None, "设备 %s" % dev)

    # 增益检查（PCM2902 的经典坑）
    gain_ok, gain_note = _check_gain()
    item("录音增益 > 0", gain_ok, gain_note, soft=not gain_ok)
    if not gain_ok:
        print("       ⚠️  跑 bash setup_mic_gain.sh 修复（PCM2902 默认 0%）")

    # 实际开一次流，看能不能读到音频
    if engine is not None and engine.ready:
        lis = K.KwsListener(engine=engine, device=dev)
        ok, msg = lis.start()
        if not ok:
            item("实时监听启动", False, msg, soft=True)
            return False
        item("实时监听启动", True, msg)
        try:
            # 等一会儿，看有没有音频块进来 + 测 RTF
            t0 = time.time()
            waited = 0.0
            target = max(live_seconds, 1.5)
            while time.time() - t0 < target:
                time.sleep(0.1)
                waited = time.time() - t0
            alive = lis.watchdog_ok()
            item("麦克风持续出数据", alive,
                 "%.1fs 内%s收到音频" % (waited, "" if alive else "**未**"),
                 soft=not alive)
            print("       当前电平 %.4f（说话时应明显升高）" % lis.level)
        finally:
            lis.stop()
        return True
    else:
        print("       （KWS 引擎未就绪，跳过实时监听测试）")
        return True


def _check_gain():
    """读 amixer，确认录音增益不是 0%。"""
    import subprocess
    for card in range(4):
        try:
            p = subprocess.run(["amixer", "-c", str(card), "sget", "Mic"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=3)
            if p.returncode != 0:
                continue
            out = p.stdout.decode("utf-8", "replace")
            # 形如： Mono: Capture 16 [100%] [23.81dB] [on]
            import re
            m = re.search(r"\[(\d+)%\]", out)
            if m:
                pct = int(m.group(1))
                return pct > 0, "card %d：%d%%" % (card, pct)
        except Exception:
            continue
    return False, "查不到（可能无声卡）"


# ══════════════════════════════════════════════════════════════════════
#  汇总
# ══════════════════════════════════════════════════════════════════════
def summary():
    print("\n" + "═" * 70)
    print("  KWS 预检汇总")
    print("═" * 70)
    passed = [r for r in _results if r[1]]
    failed = [r for r in _results if not r[1] and not r[3]]
    soft_failed = [r for r in _results if not r[1] and r[3]]

    print("  ✅ 通过 %d 项" % len(passed))
    if soft_failed:
        print("  ⏳ 待硬件 %d 项：%s" % (
            len(soft_failed), "、".join(r[0] for r in soft_failed)))
    if failed:
        print("  ❌ 失败 %d 项：" % len(failed))
        for name, _, note, _ in failed:
            print("       · %s  %s" % (name, note))

    print("─" * 70)
    if not failed:
        if not soft_failed:
            print("  结论：KWS 环境**全部就绪**，可以直接跑真人语音验证：")
            print("        bash run_py.sh kws_record.py            # 录真人样本")
            print("        bash run_py.sh kws_batch_test.py --sweep   # 测召回率/误报率")
            print("        bash run_py.sh kws_emergency.py --beep     # 实时监听")
        else:
            print("  结论：KWS **软件侧就绪**（硬失败 0），但麦克风侧有 %d 项待解决。" % len(soft_failed))
            print("        麦克风可用后即可真人验证：bash run_py.sh kws_emergency.py --beep")
    else:
        print("  结论：有 %d 项硬失败，请先解决再继续。" % len(failed))
    print("═" * 70)
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description="KWS 语音急停环境预检")
    ap.add_argument("--model-dir", default=None)
    ap.add_argument("--keywords", default=None)
    ap.add_argument("--threshold", type=float, default=K.DEFAULT_THRESHOLD)
    ap.add_argument("--threads", type=int, default=K.DEFAULT_THREADS)
    ap.add_argument("--no-mic", action="store_true", help="跳过 L5 麦克风检查")
    ap.add_argument("--no-live", action="store_true", help="只做静态检查，不开麦克风流")
    args = ap.parse_args()

    print("╔" + "═" * 68 + "╗")
    print("║" + "KWS 语音急停 · 环境预检（L0~L5）".center(60) + "║")
    print("╚" + "═" * 68 + "╝")

    check_interpreter()

    if not check_deps():
        print("\n  依赖不全，后续检查会大量跳过。")

    md, files_ok = check_model_files(args.model_dir)
    check_keywords(args.keywords)

    eng = check_load(args.model_dir, args.keywords, args.threads, args.threshold)

    if files_ok:
        check_official_samples(args.model_dir, args.threshold)
    else:
        section("L4 官方样本验证")
        item("跳过（L1 文件不全）", False, "", soft=True)

    if args.no_mic:
        section("L5 麦克风")
        item("跳过（--no-mic）", False, "", soft=True)
    else:
        check_microphone(eng if not args.no_live else None)

    return summary()


if __name__ == "__main__":
    raise SystemExit(main())
