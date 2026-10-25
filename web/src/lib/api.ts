/**
 * API 层：fetch 封装 + 单文件上传（XHR 进度）+ POST SSE 解析。
 * 从旧前端 api.js 移植，契约不变。
 */

export class ApiError extends Error {
  status: number;
  code?: string;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    // 服务端门禁会返回结构化 detail（{message, code, ...}），抽出人话当 message
    const message =
      typeof detail === "object" && detail !== null
        ? (detail as { message?: string }).message || JSON.stringify(detail)
        : (detail as string) || `请求失败（${status}）`;
    super(message);
    this.status = status;
    this.detail = detail;
    this.code = typeof detail === "object" && detail !== null ? (detail as { code?: string }).code : undefined;
  }
}

export async function api<T = Record<string, unknown>>(path: string, options: { method?: string; body?: unknown } = {}): Promise<T> {
  const opts: RequestInit = { method: options.method || "GET", headers: {}, credentials: "same-origin" };
  if (options.body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(options.body);
  }
  let res: Response;
  try {
    res = await fetch(path, opts);
  } catch (err) {
    throw new ApiError(0, err instanceof DOMException && err.name === "AbortError" ? "请求已取消" : "请检查网络连接后重试");
  }
  const text = await res.text().catch(() => {
    throw new ApiError(0, "读取响应失败，请重试");
  });
  let data: { detail?: unknown; message?: string; code?: string } | null = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!res.ok) {
    let detail: unknown = (data && (data.detail || data.message)) || text.slice(0, 200);
    let code = data?.code;
    if (typeof detail !== "string") {
      const d = detail as { code?: string; message?: string } | null;
      code = code || d?.code;
      detail = d?.message || JSON.stringify(detail);
    }
    const err = new ApiError(res.status, detail);
    if (code) err.code = code;
    throw err;
  }
  return data as T;
}

/** 单文件上传，带真实进度（fetch 没有上传进度事件，用 XHR）。 */
export function uploadOne(taskId: string, file: File, onProgress?: (pct: number) => void) {
  const xhr = new XMLHttpRequest();
  const promise = new Promise<{ files?: Array<{ id: string; meta?: Record<string, unknown>; vision_usable?: boolean }> }>((resolve, reject) => {
    xhr.open("POST", `/api/tasks/${taskId}/files`);
    xhr.withCredentials = true;
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) return;
      onProgress?.(Math.min(100, Math.round((event.loaded / event.total) * 100)));
    };
    xhr.onload = () => {
      let data: { detail?: unknown } | null = null;
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        data = null;
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve((data || {}) as { files?: Array<{ id: string; meta?: Record<string, unknown>; vision_usable?: boolean }> });
      else reject(new ApiError(xhr.status, (data && data.detail) || "上传失败"));
    };
    xhr.onerror = () => reject(new ApiError(0, "上传中断，请重试"));
    xhr.ontimeout = () => reject(new ApiError(0, "上传超时，请重试"));
    xhr.onabort = () => reject(new ApiError(0, "已取消"));
    const form = new FormData();
    form.append("files", file);
    xhr.send(form);
  });
  return { promise, abort: () => xhr.abort() };
}

export interface SSEHandlers {
  onEvent?: (event: string, data: Record<string, unknown>) => void;
  onError?: (message: string) => void;
  onEnd?: (info: { aborted: boolean }) => void;
}

/** POST + SSE。返回 AbortController，调用方可中途 abort()。 */
export function streamSSE(path: string, body: unknown, handlers: SSEHandlers = {}) {
  const controller = new AbortController();
  void (async () => {
    let res: Response;
    try {
      res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify(body || {}),
        credentials: "same-origin",
        signal: controller.signal,
      });
    } catch (err) {
      if (!controller.signal.aborted) {
        handlers.onError?.(err instanceof Error && err.message.includes("fetch") ? "请检查网络连接后重试" : (err as Error).message || "网络中断，请重试");
      }
      handlers.onEnd?.({ aborted: controller.signal.aborted });
      return;
    }
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      let detail: unknown = text;
      try {
        detail = JSON.parse(text).detail || text;
      } catch {
        /* keep text */
      }
      // 429 等结构化错误detail是对象（{message, code, quota...}）：直接往下传，
      // React 会把对象当子节点炸掉（错误边界兜底但用户只看到一句兜底话）。
      // 这里统一取 message 字段；没有就序列化，绝不把对象传进 onError。
      let message: string;
      if (typeof detail === "string") message = detail;
      else if (detail && typeof detail === "object" && "message" in detail) {
        message = String((detail as { message: unknown }).message || "");
      } else {
        message = JSON.stringify(detail);
      }
      handlers.onError?.(message || `请求失败（${res.status}）`);
      handlers.onEnd?.({ aborted: false });
      return;
    }
    const reader = res.body!.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx: number;
        while ((idx = buffer.indexOf("\n\n")) >= 0) {
          const block = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          if (!block || block.startsWith(":")) continue;
          let event = "message";
          const dataLines: string[] = [];
          for (const line of block.split("\n")) {
            if (line.startsWith("event:")) event = line.slice(6).trim();
            else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
          }
          if (!dataLines.length) continue;
          let payload: Record<string, unknown>;
          try {
            payload = JSON.parse(dataLines.join("\n"));
          } catch {
            payload = { raw: dataLines.join("\n") };
          }
          handlers.onEvent?.(event, payload);
        }
      }
    } catch (err) {
      if (!controller.signal.aborted) handlers.onError?.((err as Error).message || "连接中断，请重试");
    } finally {
      handlers.onEnd?.({ aborted: controller.signal.aborted });
    }
  })();
  return controller;
}
