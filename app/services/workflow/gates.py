"""逻辑门真值表。

8 种门：AND / OR / NOT / XOR / NAND / NOR / IF。
真值表逐行写死，不靠表达式求值——求值会引入 eval 类的安全面。

`IF` 是三输入门：条件为真走 on_true，为假走 on_false。
"""
from __future__ import annotations

GATE_OPS: set[str] = {"AND", "OR", "NOT", "XOR", "NAND", "NOR", "IF"}


def eval_gate(op: str, inputs: list[bool]) -> bool:
    """按 op 计算逻辑门。

    空输入：AND 视为真（无前置约束），OR/NAND/NOR 视为假，NOT/XOR 视为假。
    """
    op = (op or "").upper().strip()
    values = [bool(v) for v in (inputs or [])]

    if op == "AND":
        return all(values) if values else True
    if op == "OR":
        return any(values)
    if op == "NOT":
        return not (any(values))
    if op == "XOR":
        # 奇数个真为真
        return sum(1 for v in values if v) % 2 == 1
    if op == "NAND":
        return not (all(values) if values else True)
    if op == "NOR":
        return not any(values)
    if op == "IF":
        # inputs = [cond, true_branch, false_branch]
        if not values:
            return False
        cond = values[0]
        branch = values[1] if cond and len(values) > 1 else (values[2] if len(values) > 2 else False)
        return bool(branch)
    raise ValueError(f"未知的逻辑门：{op}")


def gate_truth_table() -> list[dict]:
    """每个门的完整真值表：4 个输入共 16 种组合（给测试与前端展示用）。

    用 4 输入而不是 2 输入：引擎的门都是变长的（AND/OR/NOT/XOR/NAND/NOR
    对输入列表归约，IF 取前三个），16 行才能覆盖「多上游」的真实形态。
    2 输入只有 4 行，NOT/XOR 的变长语义根本测不出来。
    """
    out: list[dict] = []
    for op in sorted(GATE_OPS):
        rows = []
        for a in (False, True):
            for b in (False, True):
                for c in (False, True):
                    for d in (False, True):
                        values = [a, b, c, d]
                        try:
                            result = eval_gate(op, values)
                        except ValueError:
                            result = None
                        rows.append({"inputs": values, "result": result})
        out.append({"op": op, "rows": rows})
    return out
