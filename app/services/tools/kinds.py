"""工具语义分类（kind）——SSE `tool` 事件带哪个图标、什么配色，由这里定。

十类：read / edit / delete / move / search / execute / think / fetch /
switch_mode / other。

为什么放在后端算：前端只认 kind，不认工具名。市场工具包随时能加新工具，
加的时候只要在 `_TOOL_KINDS` 里补一行（或让它落到 other），前端不用动，
也不会出现「后端不算、前端按名字猜一套」的两处漂移。前端 `toolKind.ts`
里那份同源映射只是兜底——旧后端没带 kind 字段时按名字猜。
"""
from __future__ import annotations

KINDS: tuple[str, ...] = (
    "read",        # 读：看文件、看记忆、看列表
    "edit",        # 写：改文件、造制品、渲染模板
    "delete",      # 删：删文件、去重
    "move",        # 移：移动/重命名
    "search",      # 检索：联网、知识库、出处核验
    "execute",     # 执行：计算、转换、生成随机数
    "think",       # 思考：模型调用
    "fetch",       # 抓取：抓单个网页
    "switch_mode", # 切换：改模式/改状态
    "other",       # 兜底
)

DEFAULT_KIND = "other"

_TOOL_KINDS: dict[str, str] = {
    # ---- V3 技能工具 ----
    "file.read": "read",
    "reference.verify": "search",
    "minimax.generate": "think",
    "pptx.create": "edit",
    "pptx.modify": "edit",
    "pptx.validate": "read",
    "artifact.write": "edit",
    # ---- V4.0 具名工具 ----
    "web_search": "search",
    "fetch_url": "fetch",
    "rag_query": "search",
    "memory_query": "read",
    "artifact_create": "edit",
    "doc_generate": "edit",
    "file_list": "read",
    # ---- 市场工具包（12 个，与 catalog.json 一一对应）----
    "calc": "execute",
    "text_stats": "read",
    "render_template": "edit",
    "date_calc": "execute",
    "unit_convert": "execute",
    "json_format": "edit",
    "regex_test": "execute",
    "md_table": "edit",
    "text_dedupe": "delete",
    "cn_number": "execute",
    "rand_gen": "execute",
    "encode_text": "edit",
    # ---- 合成事件：模型吐的工具块没解析出来 ----
    "_parse": "other",
}

# kind → 中文短名。卡片图标行用；执行状态另行标注。
KIND_LABELS: dict[str, str] = {
    "read": "读取",
    "edit": "编辑",
    "delete": "清理",
    "move": "移动",
    "search": "检索",
    "execute": "计算",
    "think": "思考",
    "fetch": "抓取",
    "switch_mode": "切换",
    "other": "调用",
}


def kind_of(name: str) -> str:
    """工具名 → kind。未知名字落 other，永不抛异常。"""
    kind = _TOOL_KINDS.get(name, DEFAULT_KIND)
    return kind if kind in KINDS else DEFAULT_KIND


def label_of(name: str) -> str:
    """工具名 → 给人看的中文名。优先取注册表里的 label，取不到就用名字本身。"""
    if name == "_parse":
        return "工具调用解析"
    try:
        from . import registry

        spec = registry.tool_spec(name)
        if spec is not None and spec.label:
            return spec.label
    except Exception:  # noqa: BLE001 —— 分类是展示层，不能因它崩掉整条流
        pass
    return name


def tool_meta(name: str) -> dict[str, str]:
    """SSE `tool` 事件要附带的 kind/label 两个字段。"""
    return {"kind": kind_of(name), "label": label_of(name)}
