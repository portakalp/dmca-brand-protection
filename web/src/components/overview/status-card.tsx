import { Card, CardTitle } from "@/components/states";
import { StatusDot } from "@/components/status-badge";
import { count } from "@/lib/format";
import { STATUS_HINTS, STATUS_LABELS, STATUS_TOKENS, visibleStatuses } from "@/lib/status";

export function StatusCard({
  distribution,
  detected,
}: {
  distribution: Record<string, number>;
  detected: number;
}) {
  const statuses = visibleStatuses(distribution);
  return (
    <Card
      aria-labelledby="status-title"
      className="flex min-w-0 flex-[1_1_280px] flex-col gap-4 px-5 py-[18px]"
    >
      <CardTitle
        id="status-title"
        title="Threat status"
        subtitle={`All ${count(detected)} ${detected === 1 ? "detection" : "detections"} in this period`}
      />
      <div
        aria-hidden="true"
        className="flex h-2.5 gap-0.5 overflow-hidden rounded-full bg-segment"
      >
        {statuses.map((status) =>
          distribution[status] ? (
            <span
              key={status}
              style={{
                flex: `${distribution[status]} 1 0`,
                background: `var(--status-${STATUS_TOKENS[status]})`,
              }}
            />
          ) : null,
        )}
      </div>
      <ul className="flex flex-col">
        {statuses.map((status, index) => (
          <li
            key={status}
            className="flex items-center gap-3 border-row-divider py-[9px] not-last:border-b last:pb-0"
            data-first={index === 0 || undefined}
          >
            <StatusDot status={status} />
            <span className="flex min-w-0 flex-auto flex-col">
              <span className="text-[13.5px] font-medium">{STATUS_LABELS[status]}</span>
              <span className="text-[12px] text-text-tertiary">{STATUS_HINTS[status]}</span>
            </span>
            <span className="tabular text-[14px] font-semibold">
              {count(distribution[status] ?? 0)}
            </span>
          </li>
        ))}
      </ul>
    </Card>
  );
}
