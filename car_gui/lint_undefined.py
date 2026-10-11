#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轻量"未定义名字"检查器（py_compile 查不出这类问题）

🔴 为什么需要它（2026-10-11 血的教训）：
   `_auto_loop` 里写了 `time.sleep(...)` 但模块**没有 import time**。
   `py_compile` 只查语法 → 通过；只有**真的跑到那一行**才抛 NameError。
   结果：自动聆听线程在"第二次循环"就静默死掉，用户以为还在听。

做法：用 `symtable` 拿到每个作用域，
  把"被当成全局引用、但模块层也没定义、也不是内置"的名字报出来。
"""
import builtins
import os
import symtable
import sys


def module_defined_names(src):
    """模块层真正定义/导入的名字（含星号导入的兜底）"""
    st = symtable.symtable(src, "<mod>", "exec")
    names = set()
    for sym in st.get_symbols():
        if sym.is_assigned() or sym.is_imported() or sym.is_namespace():
            names.add(sym.get_name())
    return names, st


def walk(table, mod_names, out, path, seen=None):
    for sym in table.get_symbols():
        n = sym.get_name()
        if sym.is_global() and not sym.is_assigned() and not sym.is_imported():
            if n not in mod_names and not hasattr(builtins, n) and n != "__file__":
                out.append((path, table.get_name(), table.get_lineno(), n))
    for child in table.get_children():
        walk(child, mod_names, out, path)


def check(path):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    try:
        mod_names, st = module_defined_names(src)
    except SyntaxError as e:
        return [("<语法错误>", "<module>", getattr(e, "lineno", 0) or 0,
                 "SyntaxError: %s" % e.msg)]
    out = []
    walk(st, mod_names, out, path)
    # 去重
    uniq, seen = [], set()
    for item in out:
        k = (item[1], item[3])
        if k not in seen:
            seen.add(k)
            uniq.append(item)
    return uniq


def main():
    files = sys.argv[1:]
    if not files:
        print("用法: python _undef_check.py <file.py> ...")
        return 1
    bad = 0
    for p in files:
        for scope, name, line, n in check(p):
            print("  ❌ %s:%s  作用域 %s  未定义的名字: %r"
                  % (os.path.basename(p), line, scope, n))
            bad += 1
    if bad:
        print("\n  共 %d 处可疑的未定义名字" % bad)
        return 1
    print("  ✅ 未发现未定义名字")
    return 0


if __name__ == "__main__":
    sys.exit(main())
