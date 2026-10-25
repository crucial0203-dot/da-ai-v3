"""SKILL.md 解析、分类与安全解包。

分类不是装饰：它决定这个技能会被注入提示词，还是只被保存展示。
含脚本的一律归为 script，永不执行。
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path, PurePosixPath

from .. import config, db

FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
CODE_LANGS = ("python", "py", "javascript", "js", "bash", "sh", "bat", "powershell", "ps1")
SCRIPT_EXT = {".py", ".js", ".sh", ".bat", ".ps1", ".ts"}


def parse_front_matter(text: str) -> tuple[dict, str]:
    match = FRONT_MATTER.match(text)
    if not match:
        return {}, text.strip()
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip().lower()] = value.strip().strip("'\"")
    return meta, text[match.end():].strip()


def classify(body: str, filenames: list[str] | None = None) -> str:
    names = filenames or []
    if any(PurePosixPath(n).suffix.lower() in SCRIPT_EXT for n in names):
        return "script"
    for lang in CODE_LANGS:
        if re.search(rf"```{lang}\b", body, re.I):
            return "script"
    if re.search(r"(workflow|节点|node_key|task_nodes|工作流定义)", body, re.I):
        return "workflow"
    return "prompt"


def derive_name(body: str, meta: dict, fallback: str) -> str:
    if meta.get("name"):
        return meta["name"][:40]
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:40]
    return fallback[:40]


def safe_unzip(zip_path: Path, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        if len(infos) > config.MAX_ZIP_ENTRIES:
            raise ValueError(f"压缩包条目过多（{len(infos)} > {config.MAX_ZIP_ENTRIES}）")
        total = sum(i.file_size for i in infos)
        if total > config.MAX_ZIP_UNCOMPRESSED:
            raise ValueError("压缩包解压后体积超限，已拒绝")
        for info in infos:
            name = info.filename
            if name.startswith("/") or ".." in PurePosixPath(name).parts:
                raise ValueError(f"压缩包内包含非法路径：{name}")
            if info.is_dir():
                continue
            target = dest / PurePosixPath(name).name
            counter = 1
            while target.exists():
                target = dest / f"{PurePosixPath(name).stem}_{counter}{PurePosixPath(name).suffix}"
                counter += 1
            with archive.open(info) as src, open(target, "wb") as out:
                out.write(src.read())
            extracted.append(target)
    return extracted


def active_prompt_skills(user_id: str | None) -> list[dict]:
    """可用技能：内置共享 + 用户自己的，且只取 prompt 类、已启用。"""
    rows = db.rows(
        "SELECT s.id, s.name, sv.body FROM skills s "
        "JOIN skill_versions sv ON sv.skill_id = s.id "
        "WHERE s.enabled = 1 AND s.kind = 'prompt' "
        "AND (s.is_builtin = 1 OR s.owner_id = ? OR (s.visibility = 'shared' AND s.reviewed_by IS NOT NULL)) "
        "AND sv.id = (SELECT MAX(id) FROM skill_versions WHERE skill_id = s.id) "
        "ORDER BY s.is_builtin DESC, s.created_at ASC",
        (user_id,),
    )
    return [{"id": r["id"], "name": r["name"], "body": r["body"]} for r in rows]


def skills_by_ids(user_id: str | None, skill_ids: list[str]) -> list[dict]:
    """任务绑定的 prompt 技能：可见性条件与 active_prompt_skills 完全一致，按 id 精确取。

    停用的技能取不到（enabled=1 硬条件），所以「绑定后停用 → 不再注入」。
    """
    if not skill_ids:
        return []
    marks = ",".join("?" * len(skill_ids))
    rows = db.rows(
        f"SELECT s.id, s.name, sv.body FROM skills s "
        f"JOIN skill_versions sv ON sv.skill_id = s.id "
        f"WHERE s.id IN ({marks}) AND s.enabled = 1 AND s.kind = 'prompt' "
        f"AND (s.is_builtin = 1 OR s.owner_id = ? OR (s.visibility = 'shared' AND s.reviewed_by IS NOT NULL)) "
        f"AND sv.id = (SELECT MAX(id) FROM skill_versions WHERE skill_id = s.id)",
        (*skill_ids, user_id),
    )
    return [{"id": r["id"], "name": r["name"], "body": r["body"]} for r in rows]
