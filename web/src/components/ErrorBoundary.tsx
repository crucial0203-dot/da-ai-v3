/** 错误边界：React 渲染期抛异常时兜底，避免整站白屏。
 *
 * 两条纪律（照 ZCode ErrorBoundary 的教训）：
 * 1. 必须把捕获到的异常转发出去（onCaughtError）——React 错误边界捕获的异常
 *    不会冒泡到 window.onerror，监控默认收不到，不转发等于线上崩溃 100% 无感知。
 * 2. 正常态直接 return children，绝不额外包一层 DOM——本站样式依赖
 *    `.shell > .sidebar` 这类结构选择器，多包一个 div 会塌布局。
 */
import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  /** 出错区域的名字，用于上报定位与兜底文案 */
  label?: string;
  /** 自定义兜底 UI；不传用默认卡片 */
  fallback?: ReactNode;
  /** 必须接——不传就退化成 console.error */
  onCaughtError?: (error: Error, info: ErrorInfo) => void;
}

interface State {
  error: Error | null;
  stack: string;
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, stack: "" };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    const stack = String(info.componentStack || "").slice(0, 2000);
    this.setState({ stack });
    if (this.props.onCaughtError) {
      try {
        this.props.onCaughtError(error, info);
      } catch {
        /* 上报不能二次抛错 */
      }
    } else {
      console.error(`[ErrorBoundary${this.props.label ? `:${this.props.label}` : ""}]`, error);
    }
  }

  private reset = () => this.setState({ error: null, stack: "" });

  render() {
    const { error, stack } = this.state;
    if (!error) return this.props.children;
    if (this.props.fallback) return this.props.fallback;

    return (
      <div className="card" data-testid="error-boundary" style={{ margin: 12 }}>
        <h4 style={{ margin: "0 0 6px" }}>
          {this.props.label ? `「${this.props.label}」` : "这一段"}加载失败了
        </h4>
        <p className="small muted" style={{ margin: "0 0 10px" }}>
          页面其他部分不受影响，可以重试这一段。
        </p>
        <div style={{ display: "flex", gap: 8, marginBottom: 10 }}>
          <button className="btn btn-primary btn-sm" onClick={this.reset} data-testid="error-boundary-retry">
            重试
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => window.location.reload()}>
            刷新页面
          </button>
        </div>
        <details>
          <summary className="small muted" style={{ cursor: "pointer" }}>
            展开错误详情
          </summary>
          <pre
            className="small"
            style={{ whiteSpace: "pre-wrap", wordBreak: "break-word", marginTop: 8, opacity: 0.8 }}
          >
            {error.message}
            {stack ? `\n${stack}` : ""}
          </pre>
        </details>
      </div>
    );
  }
}
