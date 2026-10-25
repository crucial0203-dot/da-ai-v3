"""工具白名单与目录。

**这是权限的唯一硬事实源。** 模型在 Markdown 里写「允许访问数据库」不产生任何权限；
`skill_tools.py` 里配置出来的 permissions 只是第二层。两层都放行才执行。

`network.unrestricted` 永远在禁止表里——联网只能走具名的 `web_search` / `fetch_url`，
受超时、字数、协议白名单约束。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

# ---- 平台硬编码白名单 ----
#
# 分两组：
#   LEGACY_TOOLS —— V3 的技能工具（PPT 工作流在用），语义不变；
#   V4_TOOLS     —— V4.0 新增的具名工具（对话/工作流都能调）。
LEGACY_TOOLS: set[str] = {
    "file.read",          # 读当前任务上传的文件
    "reference.verify",   # 资料核验：在已上传材料中定位出处
    "minimax.generate",   # 模型调用（走额度控制）
    "pptx.create",        # 创建新的 PPTX
    "pptx.modify",        # 修改指定源文件副本
    "pptx.validate",      # 对成果做检查
    "artifact.write",     # 写入当前任务目录（交付物登记）
}

V4_TOOLS: set[str] = {
    "web_search",         # 联网检索（受控：只能走已注册 provider）
    "fetch_url",          # 抓取单个网页正文（≤4000 字，禁内网地址）
    "rag_query",          # 检索知识库
    "memory_query",       # 读用户记忆
    "artifact_create",    # 创建可预览制品
    "doc_generate",       # 生成 Word/PPT 文件（需人工批准）
    "file_list",          # 列出当前任务的文件
}

ALLOWED_TOOLS: set[str] = LEGACY_TOOLS | V4_TOOLS

# 硬编码禁止：任何配置都不能开启
FORBIDDEN_TOOLS: set[str] = {"shell.execute", "secret.read", "network.unrestricted"}

# ---- 插件扩展区（v0.1）----
# 插件注册的工具进这里，名字同步进 ALLOWED_TOOLS（executor 的白名单校验）。
# FORBIDDEN_TOOLS 仍然优先——插件永远不能解锁被禁工具。
PLUGIN_SPECS: dict[str, "ToolSpec"] = {}


def register_plugin_tool(spec: "ToolSpec") -> None:
    if spec.name in FORBIDDEN_TOOLS:
        raise ValueError(f"工具 {spec.name} 被平台禁止，插件不能注册")
    if spec.name in ALLOWED_TOOLS:
        raise ValueError(f"工具 {spec.name} 已存在（内置或已注册），不能覆盖")
    PLUGIN_SPECS[spec.name] = spec
    ALLOWED_TOOLS.add(spec.name)


def unregister_plugin_tool(name: str) -> None:
    PLUGIN_SPECS.pop(name, None)
    if name not in LEGACY_TOOLS | V4_TOOLS:
        ALLOWED_TOOLS.discard(name)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    label: str
    desc: str
    schema: dict[str, Any] = field(default_factory=dict)
    requires_approval: bool = False
    # 对话回路里可以直接被模型调用；工作流节点里只能按名字引用
    conversational: bool = True
    # 审批决策钩子：按参数/上下文动态判定（mastra needs_approval 范式）。
    # 例：doc_generate 只对 pptx 要批、docx 免批。抛异常时执行器 fail-closed。
    needs_approval: Callable[[dict, dict], bool] | None = None
    # 审批豁免：将来会话级「全部需批」开关打开时，这类只读内部工具也不挂起。
    approval_exempt: bool = False


def builtin_specs() -> list[ToolSpec]:
    """内置工具目录。schema 写给模型看，描述要短且不含歧义。"""
    return [
        ToolSpec(
            "web_search", "联网检索",
            "检索公开网页，返回带编号的搜索结果摘要。"
            "调用格式：{\"name\":\"web_search\",\"input\":{\"query\":\"关键词\"}}。"
            "只用于需要最新信息或外部事实的问题。",
            {"query": {"type": "string", "description": "搜索关键词或问题"}},
        ),
        ToolSpec(
            "fetch_url", "抓取网页",
            "抓取指定网页的正文文本（最多 4000 字）。"
            "调用格式：{\"name\":\"fetch_url\",\"input\":{\"url\":\"https://...\"}}。",
            {"url": {"type": "string", "description": "要抓取的网页地址"}},
        ),
        ToolSpec(
            "rag_query", "知识库检索",
            "在用户已建的知识库里检索相关片段，返回带编号的片段。"
            "调用格式：{\"name\":\"rag_query\",\"input\":{\"query\":\"关键词\",\"top_k\":5}}。",
            {
                "query": {"type": "string", "description": "检索关键词"},
                "top_k": {"type": "integer", "description": "返回条数，默认 5"},
            },
        ),
        ToolSpec(
            "memory_query", "读记忆",
            "读取该用户此前记住的偏好、背景信息。"
            "调用格式：{\"name\":\"memory_query\",\"input\":{\"query\":\"要找的记忆关键词\"}}。",
            {"query": {"type": "string", "description": "记忆检索词"}},
        ),
        ToolSpec(
            "artifact_create", "创建制品",
            "创建一个可预览的 HTML 制品（图表、表格、页面草图）。"
            "调用格式：{\"name\":\"artifact_create\",\"input\":{\"title\":\"标题\",\"html\":\"完整 HTML 文本\"}}。",
            {
                "title": {"type": "string", "description": "制品标题"},
                "html": {"type": "string", "description": "完整 HTML 文档"},
            },
        ),
        ToolSpec(
            "doc_generate", "生成文档",
            "生成 Word 或 PPT 文件（用户要 .docx/.pptx 文件时必须用它，不要手写 HTML）。会先请求人工批准。"
            "调用格式：{\"name\":\"doc_generate\",\"input\":{\"doc_type\":\"pptx\",\"goal\":\"要写的内容\"}}。",
            {
                "doc_type": {"type": "string", "description": "docx 或 pptx"},
                "goal": {"type": "string", "description": "生成目标"},
            },
            requires_approval=True,
        ),
        ToolSpec(
            "file_list", "列出文件",
            "列出当前任务已上传的文件名与大小。调用格式：{\"name\":\"file_list\",\"input\":{}}。",
            {},
        ),
        *PLUGIN_SPECS.values(),
    ]


def tool_spec(name: str) -> ToolSpec | None:
    for spec in builtin_specs():
        if spec.name == name:
            return spec
    return None


def assert_allowed(cfg: dict, tool: str) -> None:
    """执行器每个动作入口调用。未授权直接抛 PermissionError。

    与 V3 的 `skill_tools.assert_allowed` 语义一致，只是白名单扩大了。
    """
    if tool in FORBIDDEN_TOOLS:
        raise PermissionError(f"工具 {tool} 被平台禁止")
    if tool not in ALLOWED_TOOLS:
        raise PermissionError(f"工具 {tool} 不在平台白名单内")
    perms = cfg.get("permissions") or {}
    # 只有配置里显式授权的工具才放行；未列出的按未授权处理。
    # 注：对话回路的具名工具由 catalog_for_prompt 决定是否暴露给模型，
    # 那层已经做过程序级授权，这里的 permissions 是技能的额外声明。
    if perms and not perms.get(tool):
        raise PermissionError(f"技能未授权工具：{tool}")


def _schema_brief(schema: dict | None) -> str:
    """把参数 schema 压成给模型看的一行。

    两种形状都要支持：
      扁平式   {"query": {"type": "string"}}                      —— 内置工具这么写
      标准式   {"type":"object","properties":{...},"required":[]}  —— 市场工具包这么写

    以前只认前一种：遍历标准式时 k="type"、v="object"（字符串），"object".get()
    抛 AttributeError，被 chat_context 的 except 吞掉，降级成空目录——
    症状正是「装了工具包但模型完全看不到」（2026-10-24 修）。
    """
    if not schema:
        return ""
    props = schema.get("properties")
    if isinstance(props, dict):
        required = set(schema.get("required") or [])
        return ", ".join(
            f"{k}{'*' if k in required else ''}: "
            f"{(v.get('type', 'string') if isinstance(v, dict) else 'string')}"
            for k, v in props.items()
        )
    # 扁平式：值不是 dict 的直接跳过，别让一条脏数据炸掉整个目录
    return ", ".join(
        f"{k}: {v.get('type', 'string')}" for k, v in schema.items() if isinstance(v, dict)
    )


def catalog_for_prompt(enabled: list[str]) -> str:
    """给模型看的工具目录。只列 enabled 里的，避免模型调用没开的工具。"""
    specs = [s for s in builtin_specs() if s.name in enabled]
    if not specs:
        return ""
    lines = [
        "可用工具（需要时输出一个 ```tool 代码块，内容必须是完整的调用对象：",
        "{\"name\":\"工具名\",\"input\":{参数}}，name 字段必填，input 里放参数）：",
    ]
    for spec in specs:
        lines.append(f"- {spec.name}（{spec.label}）：{spec.desc} 参数：{_schema_brief(spec.schema) or '无'}")
    lines.append("一次只能调用一个工具。需要多个时按顺序逐次调用。")
    return "\n".join(lines)
