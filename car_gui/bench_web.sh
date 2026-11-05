#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════
#  架空（轮子离地）基础验收 —— 在板子上直接跑，不需要接键盘鼠标
#
#  用法：
#      bash bench_web.sh                 # 默认 http://127.0.0.1:8090
#      BASE=http://192.168.31.128:8090 bash bench_web.sh
#
#  ⚠️ **只用于小车架空时**：脚本会让轮子真的转（每个动作 0.5~0.8 秒）。
#     落地跑之前请确认场地，或改用《装车验收清单.md》里的人工步骤。
#
#  覆盖：连接/电压、四个方向、文本指令、急停+锁定+拒绝、断网看门狗、
#        转向不污染直行速度、多步计划、TTS 播报错误
# ══════════════════════════════════════════════════════════════════════
BASE="${BASE:-http://127.0.0.1:8090}"
J='Content-Type: application/json'
pass=0; fail=0

ck(){ if [ "$2" = "$3" ]; then echo "  [PASS] $1"; pass=$((pass+1));
     else echo "  [FAIL] $1   期望=$3 实际=$2"; fail=$((fail+1)); fi; }
getf(){ curl -s "$BASE/api/status" | python3 -c "import sys,json;print(json.load(sys.stdin).get('$1'))"; }
act(){ curl -s -X POST "$BASE/api/action" -H "$J" -d "{\"action\":\"$1\"}" >/dev/null; }
stop(){ curl -s -X POST "$BASE/api/stop" -H "$J" -d '{}' >/dev/null; }
text(){ curl -s -X POST "$BASE/api/text" -H "$J" -d "{\"text\":\"$1\"}" >/dev/null; }
post(){ curl -s -X POST "$BASE/$1" -H "$J" -d "$2" >/dev/null; }

echo "════════════════════════════════════════════════════════════"
echo " 架空基础验收  BASE=$BASE"
echo "════════════════════════════════════════════════════════════"
stop; sleep 0.5

echo ""
echo "[0] 前置"
ck "串口已连接" "$(getf connected)" "True"
ck "当前=停止" "$(getf action)" "stop"
ck "TTS 就绪" "$(getf tts_state)" "ready"
ck "LLM 就绪（cloud/本地均可）" "$(python3 -c "print('ok' if '$(getf llm_state)'=='ready' else 'no')")" "ok"
ck "语音急停就绪" "$(getf kws_state)" "ready"
ck "电压 >12V" "$(python3 -c "v=$(getf voltage);print('ok' if v and v>12 else 'low')")" "ok"

echo ""
echo "[1] 四个方向（网页按住键，0.5~0.8s 后松开）"
for a in forward backward turn_left turn_right; do
  act "$a"; sleep 0.6
  ck "$a 动作生效" "$(getf action)" "$a"
  stop; sleep 0.35
done
ck "松开后停止" "$(getf action)" "stop"

echo ""
echo "[2] 文本指令（前进一秒，真实计时）"
text "前进一秒"
t0=$(python3 -c 'import time;print("%.2f"%time.time())')
seen=""
while python3 -c "import time;raise SystemExit(0 if time.time()-$t0<3.0 else 1)"; do
  a=$(getf action)
  [ "$a" = "forward" ] && seen="yes"
  if [ "$seen" = "yes" ] && [ "$a" = "stop" ]; then
    echo "  · 实际行进 $(python3 -c "print('%.2f' % ($(python3 -c 'import time;print(time.time())')-$t0))")s"
    break
  fi
  sleep 0.1
done
ck "文本指令驱动了车" "$seen" "yes"

echo ""
echo "[3] 急停 / 锁定 / 拒绝 / 解除"
post "api/estop" '{}'; sleep 0.5
ck "急停→锁定" "$(getf estop_locked)" "True"
ck "急停→停止" "$(getf action)" "stop"
act forward; sleep 0.4
ck "锁定中拒绝动作" "$(getf action)" "stop"
post "api/estop/release" '{}'; sleep 0.4
ck "解除锁定" "$(getf estop_locked)" "False"

echo ""
echo "[4] 断网看门狗（按住但不再心跳，5s 内应自动停）"
act forward; sleep 1
ck "开始前进" "$(getf action)" "forward"
sleep 5
ck "★ 无心跳自动停车" "$(getf action)" "stop"

echo ""
echo "[5] 转向不污染直行速度（2026-10-28 修的 bug）"
post "api/speed" '{"value":30}'; sleep 0.3
text "左转30度"; sleep 2.5
ck "左转后 speed 仍=30" "$(getf speed)" "30"
stop

echo ""
echo "[6] 多步计划（前进半米 + 左转30度）"
text "先前进半米，再左转30度"
sleep 1.5
ck "计划执行中" "$(getf plan_running)" "True"
sleep 7
ck "计划已结束" "$(getf plan_running)" "False"

echo ""
echo "[7] TTS 播报（合成 + 播放；不要求能听见）"
text "前进"; sleep 6
curl -s "$BASE/api/logs?since=0" > /tmp/_bench_logs.json
python3 - <<'PY'
import json
lines = json.load(open('/tmp/_bench_logs.json'))['lines']
bad = [l for l in lines if 'Permission denied' in l or '播报失败' in l]
print("  · 播报失败条数: %d" % len(bad))
for l in bad[-2:]: print("    !", l)
PY

stop
echo ""
echo "════════════════════════════════════════════════════════════"
echo " 结果：通过 $pass · 失败 $fail"
echo "════════════════════════════════════════════════════════════"
[ "$fail" -eq 0 ]