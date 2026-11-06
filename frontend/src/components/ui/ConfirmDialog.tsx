import { useEffect, useRef, type ReactNode } from 'react';
import { Button } from './Button';

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description?: ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  tone?: 'primary' | 'danger';
  loading?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  children?: ReactNode;
}

/** Native <dialog>: focus is trapped, Esc closes, and the backdrop is inert. */
export function ConfirmDialog({ open, title, description, confirmLabel, cancelLabel = 'Cancel', tone = 'primary', loading, onConfirm, onCancel, children }: ConfirmDialogProps) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      aria-labelledby="confirm-dialog-title"
      onCancel={(event) => {
        event.preventDefault();
        onCancel();
      }}
      onClick={(event) => {
        if (event.target === ref.current) onCancel();
      }}
      className="w-[min(92vw,28rem)] rounded-xl border border-neutral-200 bg-white p-0 text-neutral-900 shadow-pop backdrop:bg-neutral-900/40"
    >
      <div className="p-5">
        <h2 id="confirm-dialog-title" className="text-base font-semibold">
          {title}
        </h2>
        {description ? <p className="mt-1.5 text-sm text-neutral-600">{description}</p> : null}
        {children ? <div className="mt-4">{children}</div> : null}
      </div>
      <div className="flex justify-end gap-2 rounded-b-xl border-t border-neutral-100 bg-neutral-50 px-5 py-3">
        <Button variant="secondary" onClick={onCancel} disabled={loading}>
          {cancelLabel}
        </Button>
        <Button variant={tone === 'danger' ? 'danger' : 'primary'} onClick={onConfirm} loading={loading}>
          {confirmLabel}
        </Button>
      </div>
    </dialog>
  );
}
