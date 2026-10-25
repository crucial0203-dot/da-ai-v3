"""技能工具白名单：权限不由配置文本说了算，由这里强制执行。

原则（照 V3.1 方案）：
- 模型在 Markdown 里写「允许访问数据库」不产生任何权限；
- 真正生效的只有 meta_json.permissions 里、且在本模块白名单内的工具；
- shell.execute / secret.read 永远禁止，配置里写了也无效。
"""
from __future__ import annotations

# 允许配置的工具全集（第一版）
ALLOWED_TOOLS: set[str] = {
    "file.read",          # 读当前任务上传的文件
    "reference.verify",   # 资料核验：在已上传材料中定位出处
    "minimax.generate",   # 模型调用（走额度控制）
    "pptx.create",        # 创建新的 PPTX
    "pptx.modify",        # 修改指定源文件副本
    "pptx.validate",      # 对成果做检查
    "artifact.write",     # 写入当前任务目录（交付物登记）
}

# 硬编码禁止：任何配置都不能开启
FORBIDDEN_TOOLS: set[str] = {"shell.execute", "secret.read", "network.unrestricted"}

# 节点里允许出现的执行器 = 允许配置的工具
_NODE_KEYS_REQUIRED = {"key", "label", "deps", "tools"}


def validate_permissions(perms: dict) -> list[str]:
    """配置校验：返回错误列表，空 = 通过。FORBIDDEN 出现即报错。"""
    errors: list[str] = []
    if not isinstance(perms, dict):
        return ["permissions 必须是对象"]
    for tool in perms:
        if tool in FORBIDDEN_TOOLS:
            errors.append(f"工具 {tool} 被禁止，不能配置")
        elif tool not in ALLOWED_TOOLS:
            errors.append(f"未知工具 {tool}，可选：{', '.join(sorted(ALLOWED_TOOLS))}")
    return errors


def assert_allowed(cfg: dict, tool: str) -> None:
    """执行器每个节点动作入口调用。未授权直接抛 PermissionError。"""
    if tool in FORBIDDEN_TOOLS:
        raise PermissionError(f"工具 {tool} 被平台禁止")
    if tool not in ALLOWED_TOOLS:
        raise PermissionError(f"工具 {tool} 不在平台白名单内")
    perms = cfg.get("permissions") or {}
    if not perms.get(tool):
        raise PermissionError(f"技能未授权工具：{tool}")


def _has_cycle(nodes: list[dict]) -> bool:
    graph = {n["key"]: list(n.get("deps") or []) for n in nodes}
    state: dict[str, int] = {}  # 0= visiting, 1= done

    def visit(key: str) -> bool:
        if state.get(key) == 0:
            return True
        if state.get(key) == 1:
            return False
        state[key] = 0
        for dep in graph.get(key, []):
            if dep in graph and visit(dep):
                return True
        state[key] = 1
        return False

    return any(visit(key) for key in graph)


def validate_workflow_config(meta: dict) -> list[str]:
    """发布前对 skill.json 的完整性校验。"""
    errors: list[str] = []
    nodes = meta.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return ["nodes 必须是非空数组"]

    keys: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            errors.append("nodes 里每一项必须是对象")
            return errors
        missing = _NODE_KEYS_REQUIRED - set(node)
        if missing:
            errors.append(f"节点缺少字段：{', '.join(sorted(missing))}")
            return errors
        keys.append(node["key"])
    if len(keys) != len(set(keys)):
        errors.append("节点 key 重复")
        return errors

    for node in nodes:
        for dep in node.get("deps") or []:
            if dep not in keys:
                errors.append(f"节点 {node['key']} 依赖了不存在的 {dep}")
        for tool in node.get("tools") or []:
            if tool in FORBIDDEN_TOOLS:
                errors.append(f"节点 {node['key']} 使用了被禁止的工具 {tool}")
            elif tool not in ALLOWED_TOOLS:
                errors.append(f"节点 {node['key']} 使用了未知工具 {tool}")
        retries = node.get("retries", 2)
        if not isinstance(retries, int) or retries < 0 or retries > 5:
            errors.append(f"节点 {node['key']} 的 retries 必须是 0–5")

    if not errors and _has_cycle(nodes):
        errors.append("节点依赖存在环")

    # 权限整体校验
    errors.extend(validate_permissions(meta.get("permissions") or {}))

    # 运行策略
    policy = meta.get("policy") or {}
    approval_after = policy.get("approval_after")
    if approval_after is not None and approval_after not in keys:
        errors.append(f"policy.approval_after 指向不存在的节点 {approval_after}")
    retries = policy.get("max_retries_per_step", 2)
    if not isinstance(retries, int) or not (0 <= retries <= 5):
        errors.append("policy.max_retries_per_step 必须是 0–5")

    # 参数 schema：只认 page_count 这类简单约束
    schema = meta.get("params_schema") or {}
    for name, rule in schema.items():
        if not isinstance(rule, dict) or "type" not in rule:
            errors.append(f"params_schema.{name} 缺少 type")
    return errors
