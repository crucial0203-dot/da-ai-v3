"""内置工作流定义。

PPT 六节点从 V3 的 `PPT_WORKFLOW_META` 搬过来，翻译成画布图结构。
其余是几条常用的小流程，演示「工作流不是只有 PPT」。
"""
from __future__ import annotations


def _node(node_id: str, node_type: str, x: float, y: float, label: str, **data) -> dict:
    return {
        "id": node_id,
        "type": node_type,
        "position": {"x": x, "y": y},
        "data": {"label": label, **data},
    }


def _edge(source: str, target: str) -> dict:
    return {"id": f"{source}->{target}", "source": source, "target": target}


def ppt_workflow_graph() -> dict:
    """PPT 制作流程：拆纲 → 检索 → 内容 → 人工确认 → 渲染 → 校验 → 交付。

    与 V3 的 `PPT_WORKFLOW_META` 同构（六阶段 + content 后人工确认），
    但节点结构对齐画布，且 approval 是显式节点而不是 policy 字段。
    """
    return {
        "nodes": [
            _node("start", "start", 0, 0, "开始"),
            _node("plan", "llm", 240, 0, "拆纲",
                  prompt="任务目标：{{goal}}\n页数：{{page_count}}\n"
                         "输出 JSON：{\"title\":\"...\",\"sections\":[{\"heading\":\"...\",\"points\":[\"...\"]}]}"),
            _node("research", "rag", 480, 0, "资料检索",
                  query="{{goal}}", top_k=6),
            _node("content", "llm", 720, 0, "逐页内容",
                  prompt="任务目标：{{goal}}\n页数：{{page_count}}\n"
                         "大纲：{{plan}}\n资料：{{research}}\n"
                         "输出 JSON：{\"slides\":[{\"title\":\"...\",\"bullets\":[\"...\"],\"notes\":\"...\"}]}"),
            _node("confirm", "approval", 960, 0, "人工确认大纲",
                  reason="渲染前请确认大纲与逐页内容"),
            _node("render", "artifact", 1200, 0, "渲染预览",
                  title="{{goal}}",
                  html="<h1>{{goal}}</h1><pre>{{content}}</pre>"),
            _node("validate", "llm", 1440, 0, "校验",
                  prompt="检查下面这批幻灯片是否满足：一页一个判断、标题是结论、"
                         "每页 3–5 条要点、备注完整。指出问题，没有问题就说「通过」。\n{{content}}"),
            _node("deliver", "output", 1680, 0, "交付",
                  text="PPT 已完成。{{content}}"),
        ],
        "edges": [
            _edge("start", "plan"),
            _edge("plan", "research"),
            _edge("research", "content"),
            _edge("content", "confirm"),
            _edge("confirm", "render"),
            _edge("render", "validate"),
            _edge("validate", "deliver"),
        ],
    }


def summary_workflow_graph() -> dict:
    """长文摘要：抽取 → 归纳 → 交付。"""
    return {
        "nodes": [
            _node("start", "start", 0, 0, "开始"),
            _node("extract", "llm", 240, 0, "抽取要点",
                  prompt="从下面材料里抽出 5–8 条要点，每条不超过 40 字。\n{{materials}}"),
            _node("summary", "llm", 480, 0, "归纳成摘要",
                  prompt="把下面的要点改写成一段 300 字的摘要，不要分点。\n{{extract}}"),
            _node("deliver", "output", 720, 0, "交付", text="{{summary}}"),
        ],
        "edges": [
            _edge("start", "extract"),
            _edge("extract", "summary"),
            _edge("summary", "deliver"),
        ],
    }


def builtin_workflows() -> list[dict]:
    """内置工作流定义列表。"""
    return [
        {
            "id": "builtin_ppt",
            "name": "PPT 制作",
            "description": "拆纲 → 检索 → 逐页内容 → 人工确认 → 渲染预览 → 校验 → 交付。",
            "graph": ppt_workflow_graph(),
        },
        {
            "id": "builtin_summary",
            "name": "长文摘要",
            "description": "从材料抽取要点，再归纳成一段摘要。",
            "graph": summary_workflow_graph(),
        },
    ]


def workflow_to_graph(meta: dict) -> dict:
    """把 V3 风格的 meta（nodes+deps）转成画布图结构。

    用于把已有的 skill 工作流（如 PPT_WORKFLOW_META）并入新引擎。
    """
    nodes = []
    edges = []
    existing = meta.get("nodes") or []
    for index, item in enumerate(existing):
        key = str(item.get("key") or f"n{index}")
        deps = list(item.get("deps") or [])
        label = str(item.get("label") or key)
        node_type = "llm"
        data: dict = {"label": label}
        if key == "plan":
            node_type = "llm"
            data["prompt"] = "任务目标：{{goal}}\n输出大纲 JSON。"
        elif key == "research":
            node_type = "rag"
            data["query"] = "{{goal}}"
            data["top_k"] = 6
        elif key == "content":
            node_type = "llm"
            data["prompt"] = "任务目标：{{goal}}\n大纲：{{plan}}\n输出逐页内容 JSON。"
        elif key == "render":
            node_type = "artifact"
            data["title"] = "{{goal}}"
            data["html"] = "<h1>{{goal}}</h1><pre>{{content}}</pre>"
        elif key == "validate":
            node_type = "llm"
            data["prompt"] = "校验下面内容的完整性与一致性，指出问题。\n{{content}}"
        elif key == "deliver":
            node_type = "output"
            data["text"] = "{{content}}"
        nodes.append({
            "id": key, "type": node_type,
            "position": {"x": 240 * (index + 1), "y": 0},
            "data": data,
        })
        for dep in deps:
            edges.append({"id": f"{dep}->{key}", "source": str(dep), "target": key})
    # 补一个 start
    nodes.insert(0, {"id": "start", "type": "start", "position": {"x": 0, "y": 0},
                     "data": {"label": "开始"}})
    if nodes[1:]:
        first = nodes[1]["id"]
        edges.insert(0, {"id": f"start->{first}", "source": "start", "target": first})
    return {"nodes": nodes, "edges": edges}
