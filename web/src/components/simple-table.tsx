import type { CSSProperties, ReactNode } from "react";

import { cn } from "@/lib/utils";

/*
 * The tables of the whitelist and account pages, drawn like the overview's Recent threats: a
 * sunken header band in sentence case, 1px row dividers, 20px outer padding. Above 560px the
 * table keeps `minWidth` and scrolls in its box if the panel is narrower. At 560px and below it
 * becomes stacked rows instead: the `primary` cell first, an `action` cell to its right, and every
 * other cell on its own line with its column `label` in front. Explicit roles keep the table
 * semantics when the rows turn into grids.
 */
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
    <div className="relative overflow-x-auto max-[560px]:overflow-visible">
      <table
        role="table"
        className="w-full border-collapse text-[13.5px] max-[560px]:block min-[561px]:min-w-(--table-min) max-[560px]:[&_tbody]:block max-[560px]:[&_tbody]:border-t max-[560px]:[&_tbody]:border-border max-[560px]:[&_thead]:sr-only"
        style={{ "--table-min": `${minWidth}px` } as CSSProperties}
      >
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
      role="columnheader"
      className={cn(
        "border-y border-border bg-sunken px-3 py-[9px] text-left text-[12.5px] font-semibold whitespace-nowrap text-text-2 first:pl-5 last:pr-5",
        className,
      )}
    >
      {children}
    </th>
  );
}

export function Td({
  children,
  label,
  primary = false,
  action = false,
  className,
}: {
  children?: ReactNode;
  /** The column name, shown in front of the value when the rows stack on a phone. */
  label?: string;
  /** The row's main cell: first, on its own line, when the rows stack. */
  primary?: boolean;
  /** Buttons: to the right of the primary cell when the rows stack. */
  action?: boolean;
  className?: string;
}) {
  return (
    <td
      role="cell"
      className={cn(
        "px-3 py-[11px] align-middle text-text-2 first:pl-5 last:pr-5",
        "max-[560px]:block max-[560px]:min-w-0 max-[560px]:p-0 max-[560px]:first:p-0 max-[560px]:last:p-0",
        primary && "max-[560px]:col-start-1 max-[560px]:row-start-1",
        action &&
          "max-[560px]:col-start-2 max-[560px]:row-start-1 max-[560px]:self-start max-[560px]:justify-self-end",
        !primary && !action && "max-[560px]:col-span-2",
        className,
      )}
    >
      {label && !primary && !action ? (
        <span aria-hidden="true" className="hidden text-text-3 max-[560px]:inline">
          {label}:{" "}
        </span>
      ) : null}
      {children}
    </td>
  );
}

export function Tr({ children }: { children: ReactNode }) {
  return (
    <tr
      role="row"
      className="border-b border-border last:border-b-0 max-[560px]:grid max-[560px]:grid-cols-[minmax(0,1fr)_auto] max-[560px]:items-center max-[560px]:gap-x-3 max-[560px]:gap-y-1 max-[560px]:px-4 max-[560px]:py-3"
    >
      {children}
    </tr>
  );
}
