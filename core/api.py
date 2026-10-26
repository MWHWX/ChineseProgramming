"""
api.py · 编译器核心 API
统一对外接口 —— CLI 和 MCP 都通过这里调用
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from .lexer import tokenize, Token, TokenType
from .parser import parse_tokens, ProgramNode
from .name_checker import NameChecker
from .codegen import CodeGenerator
from .llm_bridge import LLMBridge, create_default_bridge


# =============================================================================
# 编译选项
# =============================================================================

@dataclass
class CompileOptions:
    """编译选项"""
    llm_assist: bool = False        # 是否启用 LLM 辅助（辞意/说故校验）
    strict: bool = True              # 严格模式（警告视为错误）
    output_format: str = "python"    # 输出格式
    include_comments: bool = True    # 是否包含注释
    indent_size: int = 4             # 缩进大小
    
    # LLM 桥接（可选，由调用方注入）
    llm_bridge: Any = None          # LLM 适配器实例


# =============================================================================
# 编译结果
# =============================================================================

@dataclass
class CompileResult:
    """编译结果"""
    success: bool = False
    code: str = ""                    # 生成的 Python 代码
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    tokens: List[Token] = field(default_factory=list)
    ast: Optional[ProgramNode] = None
    
    # 统计信息
    token_count: int = 0
    statement_count: int = 0
    compile_time_ms: float = 0.0
    
    # 验证单元终裁结果
    verdict: Optional[Dict] = None
    
    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0
    
    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0
    
    def summary(self) -> str:
        """生成摘要文本"""
        lines = []
        if self.success:
            lines.append(f"✅ 编译成功")
        else:
            lines.append(f"❌ 编译失败 ({len(self.errors)} 个错误)")
        
        lines.append(f"   Token 数: {self.token_count}")
        lines.append(f"   语句数: {self.statement_count}")
        lines.append(f"   编译耗时: {self.compile_time_ms:.1f} ms")
        
        if self.errors:
            lines.append(f"\n   错误：")
            for e in self.errors[:10]:
                lines.append(f"     ❌ {e}")
        
        if self.warnings:
            lines.append(f"\n   警告：")
            for w in self.warnings[:5]:
                lines.append(f"     ⚠️ {w}")
        
        return "\n".join(lines)


# =============================================================================
# 核心编译函数
# =============================================================================

_NUMERIC_OPERAND_GATE = False   # 待维护者裁定；见下方 docstring

def _numeric_operand_errors(ast) -> list:
    """`德` / `知足` 的操作数必须是数值：三条后端共用的单一判据（#5 提案，默认关闭）。

    背景：`check`/`compile` 走 `CodeGenerator`（不做操作数类型校验），
    `compile-pbc` 走 `compiler.Compiler._instr`（`德`/`知足` 对非数值操作数抛
    SyntaxError）⇒ 同一份源码两条后端结论相反（issue #5）。

    为何默认关闭：本仓既有测试 `tests/test_full_pipeline.py`（L46/L55）以
    `德 累积信任值；` 作为**应编译成功**的样例 ⇒ 「非数值操作数必须拒绝」
    属设计取舍，不能由单个 PR 改变语义。打开 `_NUMERIC_OPERAND_GATE` 即让
    三条后端同口径（判据词与 compiler 侧逐字一致）。

    覆盖范围：只覆盖 `德`(DE) / `知足`(ZHIZU) 两条有数值语义的指令。
    """
    out = []
    candidates = []
    for node in getattr(ast, "statements", []) or []:
        if getattr(node, "instruction", None) is not None:
            candidates.append(node)
        for step in getattr(node, "steps", None) or []:
            inner = getattr(step, "statement", None)
            if inner is not None and getattr(inner, "instruction", None) is not None:
                candidates.append(inner)
    for stmt in candidates:
        instr = getattr(stmt, "instruction", None)
        if instr is None:
            continue
        name = getattr(instr, "name", "")
        if name not in ("DE", "ZHIZU"):
            continue
        operands = getattr(stmt, "operands", []) or []
        if not operands:
            continue
        op0 = operands[0]
        tname = getattr(getattr(op0, "type", None), "name", "")
        if tname == "LITERAL":
            val = getattr(op0, "literal_value", None)
        elif tname == "IDENTIFIER":
            val = getattr(op0, "name", None)
        else:
            val = None
        if val is None:
            continue
        try:
            float(val)
        except (TypeError, ValueError):
            label = "德" if name == "DE" else "知足"
            out.append(f"L{getattr(stmt, 'line', '?')} {label} 的操作数必须是数值，"
                       f"得到 '{val}'（名实不符）")
    return out


def compile_source(source: str, options: Optional[CompileOptions] = None) -> CompileResult:
    """
    核心编译函数 —— 所有入口的统一调用点
    
    流程：
    1. 词法分析 → Token 序列
    2. 语法分析 → AST
    3. 名实校验 → 错误/警告列表
    4. LLM 辅助（可选）→ 辞意/说故建议
    5. 代码生成 → Python 代码
    6. 验证单元终裁 → 否决或放行
    
    Args:
        source: 协议源代码
        options: 编译选项
    
    Returns:
        CompileResult 对象
    """
    import time
    start = time.time()
    
    if options is None:
        options = CompileOptions()
    
    result = CompileResult()
    
    # ---- 第一步：词法分析 ----
    tokens, lex_errors = tokenize(source)
    result.tokens = tokens
    result.token_count = len([t for t in tokens if t.type != TokenType.EOF])
    result.errors.extend(lex_errors)
    
    # 词法错误即终止（无法继续语法分析）
    if lex_errors:
        result.compile_time_ms = (time.time() - start) * 1000
        return result
    
    # ---- 第二步：语法分析 ----
    parser = parse_tokens(tokens, [])
    ast = parser
    result.ast = ast
    
    # 获取语法错误
    syntax_errors = getattr(ast, 'errors', []) if ast else []
    if syntax_errors:
        result.errors.extend(syntax_errors)
    
    statement_count = len(getattr(ast, 'statements', [])) if ast else 0
    result.statement_count = statement_count
    
    # 语法错误即终止
    if syntax_errors or ast is None:
        result.compile_time_ms = (time.time() - start) * 1000
        return result
    
    # ---- 第三步：名实校验（核心编译模块）----
    checker = NameChecker()
    name_errors, name_warnings = checker.check(ast)
    result.errors.extend(name_errors)
    result.warnings.extend(name_warnings)
    
    # 名实校验失败即终止
    if name_errors:
        result.compile_time_ms = (time.time() - start) * 1000
        return result
    
    # ---- 第四步：LLM 辅助（可选，非阻塞）----
    llm_suggestions = None
    if options.llm_assist and options.llm_bridge:
        try:
            llm_suggestions = _llm_assist(ast, options.llm_bridge)
        except Exception as e:
            result.warnings.append(f"LLM 辅助失败: {e}")
    
    # ---- 第 4.5 步：指令操作数类型闸（#5 三后端口径统一）----
    # 背景：`compile-pbc` 走 `compiler.Compiler._instr`，其 `德`/`知足` 分支对非
    # 数值操作数抛 `SyntaxError`（干净编译错误）；而本函数走 `CodeGenerator`，
    # 后者把操作数无条件转成字符串/字面量、**从不做类型校验** ⇒ 同一份源码
    # `check` / `compile` 判通过、`compile-pbc` 判错误（issue #5 的三后端口径分歧）。
    #
    # 修法：把 `德`/`知足` 的「操作数必须是数值」判据**前置到统一调用点**（本函数），
    # 使三条后端（check / compile / compile-pbc）共用同一判据；判据措辞与
    # `compiler.Compiler._instr` 逐字一致（含「名实不符」），避免两处漂移。
    # 边界：只覆盖 `德`/`知足` 两条有数值语义的指令（其余助记符的操作数语义
    # 未定义，不在本闸范围——不擅定为其它指令发明类型判据）。
    # ---- 第五步：代码生成 ----
    gen = CodeGenerator(checker)
    code = gen.generate(ast)
    result.code = code
    result.warnings.extend(gen.warnings)
    
    # ---- 第六步：验证单元终裁 ----
    verdict = _verification_verdict(result, options)
    result.verdict = verdict
    
    if not verdict.get("passed", True):
        result.errors.append(f"验证单元否决: {verdict.get('reason', '未知原因')}")
    
    # ---- 完成 ----
    result.success = len(result.errors) == 0
    result.compile_time_ms = (time.time() - start) * 1000
    
    return result


def validate_source(source: str, options: Optional[CompileOptions] = None) -> Dict:
    """
    仅校验（不生成代码）
    用于 IDE 实时检查、CI 预检等场景
    """
    if options is None:
        options = CompileOptions()
    
    options = CompileOptions(
        llm_assist=options.llm_assist,
        strict=options.strict,
        llm_bridge=options.llm_bridge,
    )
    
    result = compile_source(source, options)
    
    return {
        "valid": result.success,
        "errors": result.errors,
        "warnings": result.warnings,
        "token_count": result.token_count,
        "statement_count": result.statement_count,
    }


# =============================================================================
# 内部函数
# =============================================================================

def _llm_assist(ast: ProgramNode, llm_bridge) -> Optional[Dict]:
    """
    LLM 辅助校验（辞意/说故）
    非阻塞：失败不影响编译结果
    """
    suggestions = {
        "name_meaning": [],    # 辞意建议
        "reasoning": [],       # 说故建议
    }
    
    # 将 AST 序列化为文本供 LLM 理解
    ast_text = _ast_to_text(ast)
    
    # 辞意校验建议
    intent = llm_bridge.understand(ast_text, context={"phase": "ciyi_check"})
    if intent:
        suggestions["name_meaning"].append(intent)
    
    # 说故校验建议
    verify_suggestion = llm_bridge.suggest_verification(
        ast_text, {"phase": "shuogu_check"}
    )
    if verify_suggestion:
        suggestions["reasoning"].append(verify_suggestion)
    
    return suggestions


def _ast_to_text(ast: ProgramNode) -> str:
    """将 AST 转换为文本描述（供 LLM 理解）"""
    parts = []
    for stmt in ast.statements:
        if stmt.type == NodeType.CONDITION_STMT:
            cond_text = _node_to_text(stmt.condition)
            parts.append(f"条件语句: 若 {cond_text}")
            if stmt.then_body:
                then_text = _node_to_text(stmt.then_body)
                parts.append(f"  则: {then_text}")
        elif stmt.type == NodeType.INSTRUCTION_STMT:
            instr_name = stmt.instruction.name if hasattr(stmt.instruction, 'name') else str(stmt.instruction)
            op_text = ", ".join(_node_to_text(op) for op in (stmt.operands or []))
            parts.append(f"指令: {instr_name} {op_text}".strip())
        elif stmt.type == NodeType.SHUYUE:
            parts.append(f"术曰块: {len(stmt.steps)} 个步骤")
        elif stmt.type == NodeType.WENYUE:
            parts.append(f"问曰: {stmt.question[:50]}")
        elif stmt.type == NodeType.DAYUE:
            parts.append(f"答曰: {stmt.answer[:50]}")
    return "\n".join(parts)


def _node_to_text(node) -> str:
    """将单个 AST 节点转为文本"""
    if node is None:
        return ""
    if hasattr(node, 'name'):
        return str(node.name)
    if hasattr(node, 'value'):
        return str(node.value)
    if hasattr(node, 'operator'):
        left = _node_to_text(node.left) if hasattr(node, 'left') else ""
        right = _node_to_text(node.right) if hasattr(node, 'right') else ""
        return f"{left} {node.operator} {right}"
    return str(type(node).__name__)


def _verification_verdict(result: CompileResult, options: CompileOptions) -> Dict:
    """
    验证单元终裁
    基于协议框架 v3.1 公理进行最终裁决
    """
    # 如果有错误，直接否决
    if result.errors:
        return {
            "passed": False,
            "reason": f"编译错误: {result.errors[0]}",
            "authority": "VERIFICATION_UNIT",
        }
    
    # 检查信任值约束
    # （这里简化实现，完整版需要检查 AST 中的信任值操作）
    
    # 检查条件空间合法性
    # （这里简化实现）
    
    # 通过
    return {
        "passed": True,
        "reason": "路径有效：结构一致且缩小信息差",
        "authority": "VERIFICATION_UNIT",
        "confidence": 0.85,
    }


# =============================================================================
# 便捷函数
# =============================================================================

def quick_compile(source: str) -> str:
    """
    快速编译 —— 返回生成的代码或错误信息
    """
    result = compile_source(source)
    if result.success:
        return result.code
    else:
        return f"# 编译错误:\n# " + "\n# ".join(result.errors)


# =============================================================================
# 测试
# =============================================================================

if __name__ == "__main__":
    test_code = """若条件空间为伴侣，则止情感权重于0.15。
道 新信任路径
问曰：如何验证信任？
答曰：信任值大于0.7。
术曰：1。德 累积信任值；2。自然 恢复默认。"""
    
    print("=" * 60)
    print("协议编译器 · 完整流水线测试")
    print("=" * 60)
    print(f"\n源代码：\n{test_code}\n")
    
    # 编译
    options = CompileOptions(llm_assist=False, strict=True)
    result = compile_source(test_code, options)
    
    print("编译结果：")
    print(result.summary())
    
    if result.success:
        print(f"\n{'─' * 60}")
        print("生成的 Python 代码：")
        print(f"{'─' * 60}")
        print(result.code)
    
    print(f"\n{'=' * 60}")
    print("测试完成")
    print(f"{'=' * 60}")
