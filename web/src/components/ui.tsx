/** 全局 UI 基建：toast 宿主、modal 宿主。挂载一次（App 根部）。 */
import { useEffect } from "react";
import { useModals, useToasts } from "../store/app";

export function ToastHost() {
  const { items, remove } = useToasts();
  return (
    <div className="toast-host" role="status">
      {items.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`} data-testid="toast" onClick={() => remove(t.id)}>
          {t.text}
        </div>
      ))}
    </div>
  );
}

export function ModalHost() {
  const { specs, close } = useModals();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && specs.length) close();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [specs.length, close]);

  if (!specs.length) return null;
  return (
    <>
      {specs.map((spec, i) => (
        <div
          key={i}
          className="modal-veil"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) close();
          }}
        >
          <div className={`modal ${spec.wide ? "wide" : ""}${spec.full ? " full" : ""}`} data-testid="modal">
            {spec.title ? <h3>{spec.title}</h3> : null}
            {spec.body}
            {spec.actions?.length ? (
              <div className="modal-actions">
                {spec.actions.map((a, j) => (
                  <button
                    key={j}
                    className={`btn ${a.kind || ""}`}
                    onClick={() => {
                      if (a.onClick) a.onClick(close);
                      else close();
                    }}
                  >
                    {a.label}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        </div>
      ))}
    </>
  );
}
