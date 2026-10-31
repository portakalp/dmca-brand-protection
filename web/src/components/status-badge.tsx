import { cn } from "@/lib/utils";
import { STATUS_LABELS, STATUS_TOKENS, type PanelStatus } from "@/lib/status";

/** The status pill of the tables (Active red, Notice sent amber, Taken down green, ...). */
export function StatusBadge({ status, className }: { status: PanelStatus; className?: string }) {
  const token = STATUS_TOKENS[status];
  const dot = status === "active" ? "var(--status-active-dot)" : `var(--status-${token})`;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border py-px pr-[9px] pl-[7px] text-xs leading-[18px] font-medium whitespace-nowrap",
        className,
      )}
      style={{
        color: `var(--status-${token}-fg)`,
        background: `var(--status-${token}-bg)`,
        borderColor: `var(--status-${token}-border)`,
      }}
    >
      <span aria-hidden="true" className="size-1.5 rounded-full" style={{ background: dot }} />
      {STATUS_LABELS[status]}
    </span>
  );
}

/** The plain status dot of the status card. */
export function StatusDot({ status }: { status: PanelStatus }) {
  return (
    <span
      aria-hidden="true"
      className="size-2 flex-none rounded-full"
      style={{ background: `var(--status-${STATUS_TOKENS[status]})` }}
    />
  );
}
