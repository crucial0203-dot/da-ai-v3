"""V4.0 工作流包：执行引擎 + 任务节点机 + 内置定义。

四块：
- `validate.py`   —— 保存前校验（服务端强校验，错误按节点归集）
- `gates.py`      —— 8 种逻辑门真值表
- `engine.py`     —— 拓扑分层执行（Kahn），波内并行（画布自定义工作流）
- `builtin.py`    —— 内置工作流定义（PPT 六节点并入）
- `task_nodes.py` —— 任务页节点机（task_nodes 表）+ 生成链共享底座
- `docgen.py`     —— 文档生成管线（docx / slides）
- `ppt.py`        —— PPT 六节点执行器（plan→…→deliver，渲染前人工确认）

节点类型封顶 8 种，nodes≤60 / edges≤200 由服务端强限——
画布复杂度是安全面，不是 UX 选择。
"""
from __future__ import annotations

from .builtin import builtin_workflows, workflow_to_graph
from .docgen import assemble_from_nodes, generate_task, register_deliverable, rerun_node
from .engine import RunEvent, run_workflow
from .gates import GATE_OPS, eval_gate
from .ppt import approve as approve_ppt, generate_ppt, load_flow, revise as revise_ppt
from .task_nodes import ensure_nodes, ensure_workflow_nodes, nodes_list
from .validate import validate_flow

__all__ = [
    "validate_flow", "eval_gate", "GATE_OPS",
    "run_workflow", "RunEvent",
    "builtin_workflows", "workflow_to_graph",
    # 任务页文档生成链（原 pipeline.py / ppt_flow.py）
    "generate_task", "rerun_node", "assemble_from_nodes", "register_deliverable",
    "generate_ppt", "approve_ppt", "revise_ppt", "load_flow",
    "ensure_nodes", "ensure_workflow_nodes", "nodes_list",
]
