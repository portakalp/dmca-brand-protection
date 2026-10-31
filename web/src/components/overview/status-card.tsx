import { Card, PanelBody, PanelHeader } from "@/components/states";
import { Status } from "@/components/status-badge";
import { percent } from "@/lib/chart";
import { count, plural } from "@/lib/format";
import {
  CLEARED_STATUSES,
  STATUS_BAR_COLORS,
  STATUS_HINTS,
  STATUS_LABELS,
  VERIFIED_STATUSES,
  visibleStatuses,
  type PanelStatus,
} from "@/lib/status";
import { cn } from "@/lib/utils";

const GROUPS: { title: string; statuses: PanelStatus[] }[] = [
  { title: "Being checked", statuses: ["reviewing"] },
  { title: "Verified threats", statuses: VERIFIED_STATUSES },
  { title: "Not a threat now", statuses: CLEARED_STATUSES },
];

const CELL = "py-2.5 align-top";

/** "Threat status": where each detection of the period stands now, grouped by verdict. */
export function StatusCard({
  distribution,
  detected,
  compact = false,
  className,
}: {
  distribution: Record<string, number>;
  detected: number;
  /** The printed report leaves out each status's meaning line so the row fits page one. */
  compact?: boolean;
  className?: string;
}) {
  const cell = compact ? "py-1.5 align-top" : CELL;
  const shown = visibleStatuses(distribution);
  const groups = GROUPS.map((group) => ({
    ...group,
    statuses: group.statuses.filter((status) => shown.includes(status)),
  })).filter((group) => group.statuses.length);
  const n = (status: PanelStatus) => distribution[status] ?? 0;
  const total = shown.reduce((sum, status) => sum + n(status), 0);
  const label = shown.map((status) => `${STATUS_LABELS[status]} ${n(status)}`).join(", ");

  return (
    <Card aria-labelledby="status-title" className={cn("flex flex-col", className)}>
      <PanelHeader
        id="status-title"
        title="Threat status"
        description={`All ${count(detected)} ${plural(detected, "detection", "detections")} in this period, and where each stands today.`}
      />
      <PanelBody className="flex-1">
        <div role="img" aria-label={label} className="flex h-3 gap-0.5 overflow-hidden rounded-xs">
          {total === 0 ? (
            <span className="flex-1 bg-grid" />
          ) : (
            shown.map((status) =>
              n(status) ? (
                <span
                  key={status}
                  style={{ flex: `${n(status)} 1 0`, background: STATUS_BAR_COLORS[status] }}
                />
              ) : null,
            )
          )}
        </div>
        <table className="mt-3 w-full border-collapse text-[13.5px]">
          <thead>
            <tr className="border-b border-border text-[12.5px] text-text-2">
              <th scope="col" className="py-2 text-left font-medium">
                Status
              </th>
              <th scope="col" className="py-2 pl-3 text-right font-medium">
                Sites
              </th>
              <th scope="col" className="py-2 pl-3 text-right font-medium whitespace-nowrap">
                % of all {count(total)}
              </th>
            </tr>
          </thead>
          {groups.map((group) => {
            const sum = group.statuses.reduce((acc, status) => acc + n(status), 0);
            return (
              <tbody key={group.title}>
                <tr>
                  <th
                    scope="rowgroup"
                    colSpan={2}
                    className="pt-3.5 pb-1 text-left text-[13px] font-semibold"
                  >
                    {group.title}
                  </th>
                  <td className="tabular pt-3.5 pb-1 pl-3 text-right text-[13px] font-semibold whitespace-nowrap">
                    {count(sum)} {plural(sum, "site", "sites")}
                  </td>
                </tr>
                {group.statuses.map((status, index) => (
                  <tr key={status} className={cn(index > 0 && "border-t border-border")}>
                    <th scope="row" className={cn(cell, "pr-2 text-left font-normal")}>
                      <Status status={status} />
                      {compact ? null : (
                        <span className="mt-0.5 block pl-[23px] text-[12.5px] leading-[1.45] text-text-3">
                          {STATUS_HINTS[status]}
                        </span>
                      )}
                    </th>
                    <td className={cn(cell, "tabular pl-3 text-right font-semibold")}>
                      {count(n(status))}
                    </td>
                    <td className={cn(cell, "tabular pl-3 text-right text-text-2")}>
                      {percent(n(status), total)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            );
          })}
        </table>
      </PanelBody>
    </Card>
  );
}
