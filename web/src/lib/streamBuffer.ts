/**
 * 流式事件合帧器（rAF batcher）—— 移植 OpenHands streaming-delta-batcher。
 *
 * 解决的问题：SSE 每个 text/thinking delta 都直接 setState，一次回复几百个
 * delta 就是几百次重渲染（markdown 全量重解析），长回答会明显卡顿。
 * 合帧后一帧只重渲染一次，压缩比通常 5–20 倍。
 *
 * 三条硬契约（改这个文件先读这里）：
 *   ① 合帧但保序：批次内事件顺序与到达顺序严格一致；
 *   ② 状态跃迁不等帧：approval / error / done / usage 到达即整队 flush
 *      （审批卡晚 16ms 出现就是事故，这类事件一个都不能省）；
 *   ③ 绝不丢尾帧：dispose() 与流结束前必须 flushNow()——否则最后一段
 *      正文会留在队列里被一起扔掉，用户看到回复缺尾巴。
 *
 * 后台标签页里 rAF 停摆，用 maxWaitMs 兜底刷新，避免界面冻在旧状态。
 */

export type StreamEvent = [event: string, data: Record<string, unknown>];

export interface StreamBatcherOptions {
  /** 收到一批事件时调用（一帧一次，事件已按到达序排好） */
  onFlush: (events: StreamEvent[]) => void;
  /** 不等帧、立即整队 flush 的事件类型 */
  immediateEvents?: readonly string[];
  /** rAF 停摆（后台标签页）时的兜底刷新间隔，默认 200ms */
  maxWaitMs?: number;
}

export interface StreamBatcher {
  push(event: string, data: Record<string, unknown>): void;
  /** 立即把队列刷出去（流结束/异常路径用） */
  flushNow(): void;
  /** 冲刷残留后作废；作废后的 push 直接丢弃 */
  dispose(): void;
}

/** 测试探针：window.__da_stream_stats = { received, flushes }（跨流累计） */
export interface StreamStats {
  received: number;
  flushes: number;
}

function stats(): StreamStats {
  const w = window as unknown as { __da_stream_stats?: StreamStats };
  if (!w.__da_stream_stats) w.__da_stream_stats = { received: 0, flushes: 0 };
  return w.__da_stream_stats;
}

const DEFAULT_IMMEDIATE: readonly string[] = ["approval", "error", "done", "usage"];

export function createStreamBatcher(options: StreamBatcherOptions): StreamBatcher {
  const { onFlush, immediateEvents = DEFAULT_IMMEDIATE, maxWaitMs = 200 } = options;
  const queue: StreamEvent[] = [];
  let rafId = 0;
  let timerId = 0;
  let disposed = false;

  const raf =
    typeof requestAnimationFrame === "function"
      ? requestAnimationFrame
      : (cb: FrameRequestCallback) => setTimeout(() => cb(0), 16) as unknown as number;
  const cancelRaf =
    typeof cancelAnimationFrame === "function"
      ? cancelAnimationFrame
      : (id: number) => clearTimeout(id);

  function cancel(): void {
    if (rafId) {
      cancelRaf(rafId);
      rafId = 0;
    }
    if (timerId) {
      clearTimeout(timerId);
      timerId = 0;
    }
  }

  function deliver(): void {
    cancel();
    if (disposed || !queue.length) return;
    // 整队取走再回调：onFlush 里再 push 不会进入这一批（避免丢事件/乱序）
    const batch = queue.splice(0, queue.length);
    stats().flushes += 1;
    try {
      onFlush(batch);
    } catch (err) {
      // 一批渲染失败不该把整条流带崩——记录后继续收后续事件
      console.error("[streamBuffer] flush 回调失败", err);
    }
  }

  function schedule(): void {
    if (disposed || rafId || timerId || !queue.length) return;
    rafId = raf(() => deliver());
    timerId = setTimeout(() => deliver(), maxWaitMs) as unknown as number;
  }

  return {
    push(event, data) {
      if (disposed) return;
      queue.push([event, data]);
      stats().received += 1;
      if (immediateEvents.includes(event)) deliver();
      else schedule();
    },
    flushNow() {
      deliver();
    },
    dispose() {
      if (disposed) return;
      deliver(); // 尾帧优先冲刷，再作废
      disposed = true;
      cancel();
    },
  };
}
