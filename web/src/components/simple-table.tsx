import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/** The tables of the account pages, styled like the threat table; they scroll in their box. */
export function SimpleTable({
  caption,
  minWidth = 560,
  children,
}: {
  caption: string;
  minWidth?: number;
  children: ReactNode;
}) {
  return (
    // relative: the sr-only labels in the head stay inside the scrolling box.
    <div className="relative overflow-x-auto">
      <table className="w-full border-collapse text-[13.5px]" style={{ minWidth }}>
        <caption className="sr-only">{caption}</caption>
        {children}
      </table>
    </div>
  );
}

export function Th({ children, className }: { children?: ReactNode; className?: string }) {
  return (
    <th
      scope="col"
      className={cn(
        "border-y border-card-border bg-table-head px-3 py-2.5 text-left text-[12px] font-medium whitespace-nowrap text-text-secondary first:pl-5 last:pr-5",
        className,
      )}
    >
      {children}
    </th>
  );
}

export function Td({ children, className }: { children?: ReactNode; className?: string }) {
  return (
    <td
      className={cn(
        "px-3 py-[11px] align-middle text-text-secondary first:pl-5 last:pr-5",
        className,
      )}
    >
      {children}
    </td>
  );
}

export function Tr({ children }: { children: ReactNode }) {
  return <tr className="border-row-divider not-first:border-t">{children}</tr>;
}
