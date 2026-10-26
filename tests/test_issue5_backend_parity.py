# -*- coding: utf-8 -*-
"""#5 守卫：三后端口径分歧的记录 + 提案态判据（默认不改变既有语义）。

背景（issue #5）：`check`/`compile` 走 `api.compile_source → CodeGenerator`（不做
操作数类型校验），`compile-pbc` 走 `compiler.Compiler._instr`（`德`/`知足` 对非数值
操作数抛 SyntaxError）⇒ 同一份源码两条后端结论相反：
`check` 判「✅ 校验通过」、`compile-pbc` 判「编译错误」。

**本守卫不主张哪一侧正确**：本仓既有测试 `tests/test_full_pipeline.py`（L46/L55）
以 `德 累积信任值；` 为**应编译成功**样例 ⇒ 「非数值操作数必须拒绝」属**设计取舍**，
由维护者裁定。因此本守卫只钉两件事：

  ① **分歧事实**（可复跑）：当前默认口径下，`check`/`compile` 与 `compile-pbc`
     对该形态给出相反结论——这条读数本身就是 #5 的缺陷面，须被记录而非默默存在；
  ② **提案判据的行为**：`api._numeric_operand_errors`（默认不接线）对 `德`/`知足`
     标识符各产出 1 条错误、对合法数值不产出——即「一旦裁定启用，判据已就绪」。

运行：
  python -X utf8 -m pytest tests/test_issue5_backend_parity.py -q
  python -X utf8 tests/test_issue5_backend_parity.py
"""
from __future__ import annotations

import os
import sys
import tempfile

_RP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RP)

PASS = FAIL = 0


def _ok(cond, label, detail=""):
    global PASS, FAIL
    PASS += int(bool(cond)); FAIL += int(not cond)
    print(f'[{"✓" if cond else "✘"}] {label}' + (f"  · {detail}" if detail else ""))


def _src(body: str) -> str:
    return "问曰：如何验证信任？\n答曰：信任值大于0.7。\n术曰：\n" + body


def _backends(src_text: str) -> dict:
    from core.api import CompileOptions, compile_source as api_compile, validate_source
    from core.compiler import compile_source as comp_compile
    from core.pbc import compile_to_pbc
    d = tempfile.mkdtemp(prefix="i5_")
    out = {
        "check(validate_source)": bool(
            validate_source(src_text, CompileOptions(strict=False))["valid"]),
        "compile(api)": bool(
            api_compile(src_text, CompileOptions(strict=False)).success),
    }
    _c, r = comp_compile(src_text, strict=False)
    out["pbc(compiler 内核)"] = bool(r["ok"])
    _c2, r2 = compile_to_pbc(src_text, os.path.join(d, "t.pbc"), strict=False)
    out["compile-pbc"] = bool(r2["ok"])
    return out


def test_divergence_is_recorded():
    """① 分歧事实：默认口径下 `德 标识符` 在 check/compile 通过、在 pbc 侧拒绝。

    这条读数正是 issue #5 的缺陷面。它必须在守卫里**被明确记录**——若哪天有人
    把两侧统一了（无论往哪边统一），本断言会红，提醒同步更新守卫与文档。
    """
    res = _backends(_src("1。德 累积信任值；\n2。止。\n"))
    api_side = res["check(validate_source)"] and res["compile(api)"]
    pbc_side = res["pbc(compiler 内核)"] and res["compile-pbc"]
    _ok(api_side is True and pbc_side is False,
         "#5 分歧事实：check/compile 放行、compile-pbc 拒绝（默认口径）", str(res))
    if not (api_side is True and pbc_side is False):
        print("     ↳ 若两侧已统一，请同步更新本守卫与 issue #5 的状态")


def test_proposal_gate_behavior():
    """② 提案判据（默认不接线）对两类输入的行为。"""
    import core.api as api_mod
    from core.lexer import tokenize
    from core.parser import parse_tokens
    _ok(api_mod._NUMERIC_OPERAND_GATE is False,
         "判据默认关闭（语义变更待维护者裁定）")
    for body, label in (("1。德 累积信任值；\n2。止。\n", "德"),
                        ("1。知足 值；\n2。止。\n", "知足")):
        toks, _ = tokenize(_src(body))
        ast = parse_tokens(toks, [])
        errs = api_mod._numeric_operand_errors(ast)
        _ok(len(errs) == 1 and label in errs[0],
             f"判据对 {label} 标识符产出 1 条错误", str(errs[:1]))
    toks, _ = tokenize(_src("1。德 0.5；\n2。止。\n"))
    ast = parse_tokens(toks, [])
    _ok(api_mod._numeric_operand_errors(ast) == [], "判据对合法数值不报错")


def test_numeric_operand_consistent_across_backends():
    """③ 合法数值：三条后端都通过（不误伤）。"""
    for body, label in (("1。德 0.5；\n2。止。\n", "德 0.5"),
                        ("1。知足 0.5；\n2。止。\n", "知足 0.5")):
        res = _backends(_src(body))
        _ok(all(v is True for v in res.values()),
             f"合法数值 {label}：三后端全部通过", str(res))


def _run_all() -> int:
    global FAIL
    before = FAIL
    for fn in (test_divergence_is_recorded,
               test_proposal_gate_behavior,
               test_numeric_operand_consistent_across_backends):
        try:
            fn()
        except Exception as ex:
            FAIL += 1
            print(f"[✘] {fn.__name__}: {type(ex).__name__}: {ex}")
    return FAIL - before


def main(argv) -> int:
    bad = _run_all()
    print(f"#5 守卫：{PASS} 通过 / {bad} 失败")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
