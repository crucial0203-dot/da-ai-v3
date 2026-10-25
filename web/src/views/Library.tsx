/** 库：材料与交付物列表、筛选、预览、下载、删除。 */
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router";
import { api } from "../lib/api";
import { formatSize, formatTime } from "../lib/format";
import { previewFile } from "../components/Message";
import { SkeletonList } from "../components/Skeleton";
import { confirmDialog, toast } from "../store/app";

interface FileRow {
  id: string;
  kind: string;
  name: string;
  size: number;
  task_id: string;
  task_title: string;
  created_at: string;
}

export default function Library() {
  const [kind, setKind] = useState("");
  const [keyword, setKeyword] = useState("");
  const [files, setFiles] = useState<FileRow[]>([]);
  const [loading, setLoading] = useState(true);

  const paint = useCallback(async () => {
    setLoading(true);
    try {
      const d = await api<{ files: FileRow[] }>(`/api/files${kind ? `?kind=${kind}` : ""}`);
      setFiles(d.files);
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setLoading(false);
    }
  }, [kind]);

  useEffect(() => {
    void paint();
  }, [paint]);

  const shown = files.filter((f) => !keyword || f.name.toLowerCase().includes(keyword));

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>库</h1>
          <div className="page-sub">材料与交付物</div>
        </div>
        <div className="row">
          <input type="search" placeholder="搜文件名…" style={{ width: 200 }} value={keyword} onChange={(e) => setKeyword(e.target.value)} />
          <button className="btn btn-ghost btn-sm" onClick={() => void paint()}>
            刷新
          </button>
        </div>
      </div>
      <div className="row" style={{ marginBottom: 16 }}>
        {[
          ["", "全部"],
          ["deliverable", "交付物"],
          ["source", "材料"],
        ].map(([k, label]) => (
          <button key={k} className={`filter-chip ${kind === k ? "active" : ""}`} onClick={() => setKind(k)}>
            {label}
          </button>
        ))}
      </div>

      {loading ? (
        <SkeletonList rows={3} />
      ) : shown.length === 0 ? (
        <div className="empty">
          <h4>暂无文件</h4>
          <p>上传材料或生成文件后会出现在这里。</p>
        </div>
      ) : (
        <table className="data-table" data-testid="library-table">
          <thead>
            <tr>
              <th>名称</th>
              <th>类型</th>
              <th>来自任务</th>
              <th>大小</th>
              <th>时间</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {shown.map((f) => (
              <tr key={f.id}>
                <td>
                  <span className="truncate" style={{ maxWidth: 280, display: "inline-block" }} title={f.name}>
                    {f.name}
                  </span>
                </td>
                <td>
                  <span className={`badge ${f.kind === "deliverable" ? "badge-ok" : ""}`}>{f.kind === "deliverable" ? "交付物" : "材料"}</span>
                </td>
                <td>{f.task_id ? <Link to={`/t/${f.task_id}`}>{f.task_title || "任务"}</Link> : <span className="faint">—</span>}</td>
                <td className="mono tiny">{formatSize(f.size)}</td>
                <td className="tiny faint">{formatTime(f.created_at)}</td>
                <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                  {f.kind === "source" && (
                    <button className="btn btn-ghost btn-sm" onClick={() => void previewFile(f.id)}>
                      看
                    </button>
                  )}
                  <a className="btn btn-sm" href={`/api/files/${f.id}`} download>
                    下载
                  </a>
                  <button
                    className="btn btn-ghost btn-sm"
                    onClick={async () => {
                      if (!(await confirmDialog("删除这个文件？"))) return;
                      await api(`/api/files/${f.id}`, { method: "DELETE" });
                      toast("已删除", "ok");
                      void paint();
                    }}
                  >
                    ×
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
