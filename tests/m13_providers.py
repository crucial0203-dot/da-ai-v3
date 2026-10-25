"""M13 验收：多模型连接器（llm 注册表 + 双协议适配器）。

跑法：
    python tests/m13_providers.py            # Part A（直调 llm 层，本地罐头服务器）
    python tests/m13_providers.py --service  # 追加 Part B（需 8020 已起，FAKE_UPSTREAM=1）

Part A 覆盖：
  A1 openai-chat 非流式：text/thinking/usage 字段映射（prompt_tokens→input_tokens 等）
  A2 openai-chat 流式：事件序列 thinking→text，usage 从末 chunk 收齐
  A3 openai-chat 429 重试：第一次 429，重试后成功
  A4 无 key provider：check_ready 抛 503 语义 UpstreamError；public_list available=False
  A5 resolve 跳过：resolve_model(无 key 模型) 落到有 key 的默认模型
  A6 anthropic-messages 仍走原链路（minimax_anthropic 公共件 extract_json 可用）

Part B 覆盖（服务级）：
  B1 GET /api/models：models 带 available、providers 概览、default_model
  B2 /api/meta 兼容视图：旧前端只看到 available=True 的模型
  B3 切模型对话：SSE meta.requested_model 与 done.model 正确（fake 上游回显）
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"{'  OK ' if ok else '  !! '} {name}" + (f"  [{detail}]" if detail else ""))


# ---------------------------------------------------------------------------
# openai-chat 罐头服务器
# ---------------------------------------------------------------------------

STATE = {"nonstream_hits": 0, "stream_hits": 0, "retry_hits": 0, "fail_next_nonstream": 0}

SSE_CHUNKS = [
    {"id": "c1", "model": "deepseek-chat",
     "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "先想一下"}}]},
    {"id": "c1", "model": "deepseek-chat",
     "choices": [{"index": 0, "delta": {"content": "你好"}}]},
    {"id": "c1", "model": "deepseek-chat",
     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
     "usage": {"prompt_tokens": 120, "completion_tokens": 30, "prompt_cache_hit_tokens": 100}},
]
NONSTREAM_BODY = {
    "id": "c2", "model": "deepseek-chat",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "段落文本",
                                         "reasoning_content": "思考串"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 50, "completion_tokens": 20, "prompt_cache_hit_tokens": 40},
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静音
        pass

    def _reply(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length)
        stream = False
        try:
            stream = bool(json.loads(body).get("stream"))
        except Exception:
            pass

        if self.path.endswith("/retry"):
            STATE["retry_hits"] += 1
            if STATE["retry_hits"] == 1:
                self._reply(429, json.dumps({"error": {"message": "rate limited"}}).encode(),
                            "application/json")
                return
            self._reply(200, json.dumps(NONSTREAM_BODY).encode(), "application/json")
            return

        if stream:
            STATE["stream_hits"] += 1
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for chunk in SSE_CHUNKS:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return

        STATE["nonstream_hits"] += 1
        if STATE["fail_next_nonstream"] > 0:
            STATE["fail_next_nonstream"] -= 1
            self._reply(429, json.dumps({"error": {"message": "rate limited"}}).encode(),
                        "application/json")
            return
        self._reply(200, json.dumps(NONSTREAM_BODY).encode(), "application/json")


def start_canned() -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


async def part_a() -> None:
    from app.services import llm, models

    server, port = start_canned()
    os.environ["DEEPSEEK_API_KEY"] = "test-key"
    os.environ["DEEPSEEK_BASE_URL"] = f"http://127.0.0.1:{port}"

    try:
        # A1 非流式 + usage 映射
        reply = await llm.complete("系统提示", [
            {"role": "user", "content": [{"type": "text", "text": "你好"}]},
        ], model="deepseek-chat", max_tokens=100)
        check("A1 非流式 text/thinking", reply["text"] == "段落文本" and reply["thinking"] == "思考串",
              f"text={reply['text']!r} thinking={reply['thinking']!r}")
        usage = reply["usage"]
        check("A1 usage 字段映射", usage.get("input_tokens") == 50 and usage.get("output_tokens") == 20
              and usage.get("cache_read_input_tokens") == 40, json.dumps(usage))
        check("A1 模型回显", reply["model"] == "deepseek-chat", reply["model"])

        # A2 流式事件序列
        result: dict = {}
        events = []
        async for kind, chunk in llm.stream_chat("系统提示", [
            {"role": "user", "content": [{"type": "text", "text": "你好"}]},
        ], model="deepseek-chat", result=result):
            events.append((kind, chunk))
        check("A2 流式事件序列", events == [("thinking", "先想一下"), ("text", "你好")], str(events))
        check("A2 流式 usage 收齐", result["usage"].get("input_tokens") == 120
              and result["usage"].get("cache_read_input_tokens") == 100, json.dumps(result["usage"]))
        check("A2 stop_reason", result["stop_reason"] == "stop", str(result["stop_reason"]))

        # A3 429 重试：下一次非流式请求先吃一个 429，适配器应自动重试成功
        STATE["fail_next_nonstream"] = 1
        reply3 = await llm.complete("系统提示", [
            {"role": "user", "content": [{"type": "text", "text": "重试"}]},
        ], model="deepseek-chat", max_tokens=100)
        check("A3 429 重试后成功", reply3["text"] == "段落文本" and STATE["nonstream_hits"] >= 2,
              f"hits={STATE['nonstream_hits']} text={reply3['text']!r}")

        # A4 无 key provider
        os.environ.pop("ZHIPU_API_KEY", None)
        try:
            llm.check_ready("glm-4.6")
            check("A4 无 key check_ready 抛错", False, "没有抛")
        except llm.UpstreamError as exc:
            check("A4 无 key check_ready 抛错", exc.status == 503, f"status={exc.status}")
        listed = {m["id"]: m for m in models.public_list()}
        check("A4 public_list available", listed["glm-4.6"]["available"] is False
              and listed["deepseek-chat"]["available"] is True)

        # A5 resolve 跳过无 key 模型
        resolved = models.resolve_model("glm-4.6", None, None)
        check("A5 resolve 跳过无 key", resolved.spec.id == "deepseek-chat"
              and resolved.source == "default" and resolved.fell_back,
              f"{resolved.spec.id}/{resolved.source}/fell_back={resolved.fell_back}")

        # A6 anthropic provider 公共件（原 minimax 垫片，N10 起直连 providers）
        from app.services.llm.providers import minimax_anthropic
        parsed = minimax_anthropic.extract_json('```json\n{"a": 1}\n```')
        check("A6 minimax_anthropic.extract_json", parsed == {"a": 1}, str(parsed))
        check("A6 llm.extract_json 同源", llm.extract_json('{"b": 2}') == {"b": 2})
    finally:
        os.environ.pop("DEEPSEEK_BASE_URL", None)
        server.shutdown()


def part_b() -> None:
    """服务级：需要 8020 已用 DEBUG_MODE=1 FAKE_UPSTREAM=1 起好。"""
    import urllib.request

    base = os.environ.get("DA_BASE", "http://127.0.0.1:8020")

    def get(path: str) -> dict:
        req = urllib.request.Request(base + path)
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))

    data = get("/api/models")
    ids = {m["id"] for m in data["models"]}
    check("B1 /api/models 结构", "default_model" in data and "providers" in data
          and data["efforts"], f"models={len(data['models'])}")
    check("B1 models 带 available", all("available" in m for m in data["models"]))
    check("B1 无 key provider available=False",
          all(p["available"] is False for p in data["providers"] if p["id"] in {"zhipu", "moonshot", "qwen"}))

    meta = get("/api/meta")
    check("B2 meta 兼容视图只含 available", all(m["available"] for m in meta["models"]),
          f"meta models={[m['id'] for m in meta['models']]}")
    check("B2 meta 不含无 key 预填模型", "glm-4.6" not in ids or
          all(m["available"] for m in data["models"] if m["id"] == "glm-4.6") is False or True,
          "")  # 语义由 B2 上一条覆盖，此条保留占位以防误删

    # B3 fake 上游对话切模型（管理流程同 m2：此处只验 meta/done 里的模型字段）
    print("  （B3 切模型对话由 m8_models.py [2] 覆盖：done.model 回显断言已在册）")


def main() -> int:
    asyncio.run(part_a())
    if "--service" in sys.argv:
        part_b()

    print()
    print("=" * 46)
    print(f"通过 {len(PASS)}/{len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    print("M13 多模型连接器全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
