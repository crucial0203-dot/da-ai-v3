"""本地搜索罐头服务器：给 m15 提供确定性的搜索结果与正文页。

只在 SEARCH_ALLOW_LOOPBACK=1 时允许指向回环地址——线上禁止，
否则任何人都能把「搜索」变成 SSRF 探针。

启动方式（m15 自动起）：
    python tests/fake_web.py <port>
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8901

# 罐头搜索结果：固定的三个页面，标题/链接/摘要都是可断言的
RESULTS = [
    {
        "name": "行政协议诉讼的裁判规则",
        "url": "http://127.0.0.1:{port}/page/1",
        "summary": "行政协议案件中，一方请求仲裁的，仲裁机构不予受理，但双方约定仲裁的除外。",
    },
    {
        "name": "最高人民法院关于行政协议若干问题的规定",
        "url": "http://127.0.0.1:{port}/page/2",
        "summary": "2019 年最高人民法院关于行政协议若干问题的规定明确了行政协议的范围。",
    },
    {
        "name": "行政协议的判断标准",
        "url": "http://127.0.0.1:{port}/page/3",
        "summary": "行政协议是行政机关与公民法人之间订立、旨在实现行政管理目标的法律协议。",
    },
]

PAGE_BODY = {
    "1": ("行政协议诉讼的裁判规则", "根据最高人民法院关于行政协议若干问题的规定，"
          "行政协议案件中，一方请求仲裁的，仲裁机构不予受理，但双方约定仲裁的除外。"
          "人民法院审理行政协议案件，应当以法律、行政法规、地方性法规为依据。"
          "参照规章可以引用的，人民法院应当予以引用的，应当在裁判文书中予以引用。"),
    "2": ("最高人民法院关于行政协议若干问题的规定", "本规定自 2019 年 9 月 1 日起施行。"
          "行政协议是指行政机关与公民、法人以及其他组织为了实现行政管理目标，"
          "依法订定的具有行政管理内容的协议。"),
    "3": ("行政协议的判断标准", "判断一份协议是不是行政协议，要看三件事："
          "主体是不是行政机关与相对人、目的是不是实现行政管理目标、"
          "内容是不是具有行政管理属性。三个都满足才能定性为行政协议。"),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D102 - 静音
        pass

    def _reply(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        payload = {
            "data": {
                "webPages": {
                    "value": [
                        {**item, "url": item["url"].format(port=PORT)}
                        for item in RESULTS
                    ]
                }
            }
        }
        self._reply(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/search"):
            # Bing 形态的搜索结果页：<h2><a href>标题</a></h2> + <p>摘要</p>
            blocks = []
            for item in RESULTS:
                url = item["url"].format(port=PORT)
                blocks.append(
                    f"<li class='b_algo'><h2><a href=\"{url}\">{item['name']}</a></h2>"
                    f"<p>{item['summary']}</p></li>"
                )
            html = ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
                    "<title>搜索结果</title></head><body><ol>"
                    + "".join(blocks)
                    + "</ol></body></html>")
            self._reply(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if "/page/" in self.path:
            num = self.path.rsplit("/", 1)[-1]
            title, body = PAGE_BODY.get(num, ("未知页面", "内容"))
            html = (
                "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                f"<title>{title}</title>"
                "<script>window.__evil = '这段脚本不该被执行';</script>"
                "</head><body>"
                f"<h1>{title}</h1><p>{body}</p>"
                "<p>这段文字用于验证正文抽取是否干净。</p>"
                "</body></html>"
            )
            self._reply(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        self._reply(404, b"not found")


def serve() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    return server


if __name__ == "__main__":
    srv = serve()
    print(f"[fake_web] listening on http://127.0.0.1:{PORT}", flush=True)
    srv.serve_forever()
