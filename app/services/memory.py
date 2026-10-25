"""V4.0 N7 用户记忆。

模型在回合结束时可以主动写入/更新/删除记忆。规则：

- **只记用户明确说出来的偏好与事实**，不记模型自己的推断；
- 记忆条目有上限（20 条）与单条长度上限（300 字），超了不写；
- 记忆是**跨任务**的，但**不跨用户**——每个用户只能看到自己的；
- 评审失败（模型调 complete 抛错）**不影响主流程**：对话照常 done，静默跳过。

记忆内容进 prompt 时单独成块 `<<<MEMORY>>>`，与材料区分开——
材料是这次给的，记忆是以前记的，模型不该把两者混为一谈。
"""
from __future__ import annotations

import json
import re

from .. import config, db

MEMORY_BLOCK = "<<<MEMORY>>>"
MEMORY_END = "<<<END_MEMORY>>>"

# 模型输出的记忆指令：
# {"op":"add","content":"..."} / {"op":"update","id":"m_xxx","content":"..."} / {"op":"delete","id":"m_xxx"}
_OP_BLOCK = re.compile(r"```memory\s*\n?([\s\S]*?)\n?```", re.IGNORECASE)

# 去重比较用的归一化：去空白与中英常见标点，忽略大小写。
# 模型评审常对同一件事反复输出 add（正文内联一份、评审再输出一份），
# 不去重记忆列表会被同一句话刷满——20 条的上限经不起这么耗。
_DEDUPE_STRIP = re.compile(r"[\s，。！？、；：「」『』（）()\[\]{},.!?;:\"'`~—-]+")


def _normalize(text: str) -> str:
    return _DEDUPE_STRIP.sub("", text or "").lower()


def _user_memories(user_id: str) -> list[dict]:
    rows = db.rows(
        "SELECT id, kind, content, source, created_at, updated_at, change FROM memories "
        "WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
        (user_id, config.MEMORY_MAX_ITEMS),
    )
    return [dict(r) for r in rows]


def list_memories(user_id: str) -> list[dict]:
    return _user_memories(user_id)


def count_memories(user_id: str) -> int:
    """记忆条数（上下文压力条用：记忆段按条数估 token）。"""
    row = db.one("SELECT COUNT(*) AS c FROM memories WHERE user_id = ?", (user_id,))
    return int((row["c"] if row else 0) or 0)


def add_memory(user_id: str, content: str, kind: str = "note",
                source: str = "", task_id: str | None = None) -> tuple[str, str]:
    """写一条记忆。返回 (id, change)：

      ("m_xxx", "added")    新写入
      ("m_xxx", "existing") 归一化后已有同一句——不重复写，只把变更态标成 existing

    超限直接抛 ValueError（调用方决定要不要回注给模型）。
    """
    content = (content or "").strip()
    if not content:
        raise ValueError("记忆内容为空")
    if len(content) > config.MEMORY_MAX_ITEM_CHARS:
        raise ValueError(f"单条记忆超过 {config.MEMORY_MAX_ITEM_CHARS} 字上限")
    # 去重命中也要写 change：记忆页要显示「已存在」，用户才知道模型
    # 想记的这件事已经有了。不动 updated_at——没有内容变化，
    # 不该把这条顶到列表最前面（排序还是看真实修改时间）。
    norm = _normalize(content)
    if norm:
        for row in db.rows("SELECT id, content FROM memories WHERE user_id = ?", (user_id,)):
            if _normalize(row["content"]) == norm:
                db.execute(
                    "UPDATE memories SET change = 'existing' WHERE id = ? AND user_id = ?",
                    (row["id"], user_id),
                )
                return row["id"], "existing"
    existing = db.one(
        "SELECT COUNT(*) AS c FROM memories WHERE user_id = ?", (user_id,)
    )
    if existing and int(existing["c"]) >= config.MEMORY_MAX_ITEMS:
        raise ValueError(f"记忆条数已达上限（{config.MEMORY_MAX_ITEMS} 条），先删旧的再加")
    mem_id = db.new_id("m_")
    now = db.now_iso()
    db.execute(
        "INSERT INTO memories(id, user_id, kind, content, source, task_id, created_at, updated_at, change) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (mem_id, user_id, kind[:32], content, source[:64], task_id, now, now, "added"),
    )
    return mem_id, "added"


def update_memory(user_id: str, mem_id: str, content: str) -> None:
    content = (content or "").strip()
    if not content:
        raise ValueError("记忆内容为空")
    if len(content) > config.MEMORY_MAX_ITEM_CHARS:
        raise ValueError(f"单条记忆超过 {config.MEMORY_MAX_ITEM_CHARS} 字上限")
    row = db.one("SELECT id FROM memories WHERE id = ? AND user_id = ?", (mem_id, user_id))
    if not row:
        raise ValueError("记忆不存在或不属于你")
    db.execute(
        "UPDATE memories SET content = ?, updated_at = ?, change = 'updated' "
        "WHERE id = ? AND user_id = ?",
        (content, db.now_iso(), mem_id, user_id),
    )


def delete_memory(user_id: str, mem_id: str) -> None:
    row = db.one("SELECT id FROM memories WHERE id = ? AND user_id = ?", (mem_id, user_id))
    if not row:
        raise ValueError("记忆不存在或不属于你")
    db.execute("DELETE FROM memories WHERE id = ? AND user_id = ?", (mem_id, user_id))


def format_context(user_id: str) -> str:
    """把记忆格式化成注入块。空则返回空串。"""
    items = _user_memories(user_id)
    if not items:
        return ""
    lines = [MEMORY_BLOCK, "以下是你此前记住的关于这位用户的信息（可能过期，仅供参考）："]
    for index, item in enumerate(items, 1):
        lines.append(f"{index}. {item['content']}")
    lines.append(MEMORY_END)
    return "\n".join(lines)


def parse_ops(text: str) -> list[dict]:
    """从模型正文里抽记忆操作指令。"""
    ops: list[dict] = []
    for match in _OP_BLOCK.finditer(text or ""):
        raw = match.group(1).strip()
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            ops.append(parsed)
        elif isinstance(parsed, list):
            ops.extend(p for p in parsed if isinstance(p, dict))
    return ops


def apply_ops(user_id: str, ops: list[dict], task_id: str | None = None) -> list[dict]:
    """执行记忆操作，返回每条的结果。

    成功行：{"op": "add"|"update"|"delete", "id": "...", "change": "added"|"updated"|"existing"|"deleted"}
    失败行：{"op": "...", "error": "人话"}

    change 的三态（added/updated/existing）同时写进 memories.change 列，
    记忆页据此显示徽标；这里返回的结构给对话里的 memory chips 用。
    """
    results: list[dict] = []
    for op in ops:
        kind = str(op.get("op") or op.get("action") or "").lower()
        content = str(op.get("content") or op.get("text") or "").strip()
        mem_id = str(op.get("id") or op.get("memory_id") or "").strip()
        try:
            if kind in {"add", "create", "remember"}:
                new_id, change = add_memory(user_id, content, kind=op.get("kind") or "note",
                                            source="model", task_id=task_id)
                results.append({"op": "add", "id": new_id, "change": change})
            elif kind in {"update", "edit"}:
                update_memory(user_id, mem_id, content)
                results.append({"op": "update", "id": mem_id, "change": "updated"})
            elif kind in {"delete", "remove", "forget"}:
                delete_memory(user_id, mem_id)
                results.append({"op": "delete", "id": mem_id, "change": "deleted"})
            else:
                results.append({"op": kind, "error": f"未知的记忆操作：{kind}"})
        except ValueError as exc:
            results.append({"op": kind, "error": str(exc)})
    return results


def review_prompt(reply_text: str) -> str:
    """给模型的记忆评审提示。

    只在回合结束后发一次，不扣额度（走 record_usage）。
    """
    return (
        "现在做一次记忆评审。如果这段对话里有值得长期记住的用户偏好、背景或约束，"
        "输出一个 ```memory 代码块，格式：\n"
        '{"op":"add","content":"一句可长期复用的记忆"}\n'
        "最多 3 条，每条不超过 100 字。\n"
        "只记用户明确表达过的偏好与事实，不要记你的推断，也不要记与本任务无关的闲聊。\n"
        "没有值得记的就不要输出代码块，直接回复「无」。"
    )
