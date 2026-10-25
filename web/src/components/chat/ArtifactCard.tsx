/**
 * 制品卡（P1-7）：一个制品从「正在写」到「可预览」的一张卡。
 *
 * 两态：
 *   generating  模型正在写 HTML（artifact_create 工具执行中）——
 *               微光「正在写」+ 实时字数（直播正文长度，就是它在写的字符数）；
 *   done        制品已落库——标题 + 版本徽标 + 预览（沙箱 iframe）+ 下载。
 *
 * 安全口径与制品渲染路由一致：iframe sandbox 不给 allow-same-origin，
 * 渲染页处在独立 opaque origin，碰不到 parent.document。
 * 预览/下载都走 /api/artifacts/{id}/render（自带极严 CSP）。
 */
import { useState } from "react";
import { ShimmerLabel } from "../Skeleton";

export interface ArtifactCardProps {
  state: "generating" | "done";
  /** generating：围栏里已写出的字符数；done：制品正文字符数 */
  words?: number;
  title?: string;
  artifactId?: string;
  version?: number;
}

export default function ArtifactCard({ state, words = 0, title, artifactId, version }: ArtifactCardProps) {
  const [preview, setPreview] = useState(false);

  if (state === "generating") {
    return (
      <div className="artifact-card" data-testid="artifact-card" data-state="generating">
        <div className="row" style={{ gap: 8 }}>
          <ShimmerLabel text="正在写" />
          <span className="tiny faint mono">{words} 字</span>
        </div>
        <div className="tiny faint">制品生成中，写完后可预览、可下载</div>
      </div>
    );
  }

  return (
    <div className="artifact-card" data-testid="artifact-row" data-state="done">
      <div className="row-between">
        <span className="small truncate grow" title={title || "未命名制品"} style={{ marginRight: 8 }}>
          {title || "未命名制品"}
        </span>
        <span className="row" style={{ gap: 6 }}>
          {words ? <span className="tiny faint mono">{words} 字</span> : null}
          {(version || 0) > 1 && <span className="badge">v{version}</span>}
        </span>
      </div>
      <div className="row" style={{ gap: 4, marginTop: 6 }}>
        {artifactId && (
          <>
            <button
              className="btn btn-ghost btn-xs"
              data-testid="artifact-toggle"
              onClick={() => setPreview((v) => !v)}
            >
              {preview ? "收起" : "预览"}
            </button>
            <a className="btn btn-ghost btn-xs" href={`/api/artifacts/${artifactId}/render`} download>
              下载
            </a>
          </>
        )}
      </div>
      {preview && artifactId && (
        <iframe className="artifact-frame" title={title || "制品预览"} sandbox="" src={`/api/artifacts/${artifactId}/render`} />
      )}
    </div>
  );
}
