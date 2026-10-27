import { useEffect, type ReactNode } from "react";

export function Dialog({ title, sub, children, foot, onClose, label }: {
  title: ReactNode; sub?: ReactNode; children: ReactNode; foot: ReactNode; onClose: () => void; label: string;
}) {
  useEffect(() => {
    const on = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    addEventListener("keydown", on);
    return () => removeEventListener("keydown", on);
  }, [onClose]);
  return (
    <>
      <div className="scrim" style={{ display: "block" }} onClick={onClose} />
      <div className="dialog" style={{ display: "block" }} role="dialog" aria-modal="true" aria-label={label}>
        <div className="dh"><h2>{title}</h2>{sub ? <p className="t2 small">{sub}</p> : null}</div>
        <div className="db">{children}</div>
        <div className="df">{foot}</div>
      </div>
    </>
  );
}
