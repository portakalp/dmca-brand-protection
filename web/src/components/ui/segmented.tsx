"use client";

import * as React from "react";
import { cn } from "cn";

export type SegmentedOption<T extends string> = { value: T; label: string };

/**
 * A row of toggle buttons for one choice (Period 7/30/90 days, Desktop/Mobile). A group named
 * by `label`; the selected button has aria-pressed and the soft accent fill. With
 * `showLabel` the label is printed before the group ("Period"); otherwise it is the group's
 * accessible name only.
 */
function Segmented<T extends string>({
  label,
  showLabel = false,
  options,
  value,
  onChange,
  className,
}: {
  label: string;
  showLabel?: boolean;
  options: SegmentedOption<T>[];
  value: T | null;
  onChange: (value: T) => void;
  className?: string;
}) {
  const id = React.useId();
  return (
    <div className={cn("inline-flex items-center gap-2", className)}>
      {showLabel ? (
        <span id={id} className="text-[13px] font-medium text-text-2">
          {label}
        </span>
      ) : null}
      <div
        role="group"
        aria-labelledby={showLabel ? id : undefined}
        aria-label={showLabel ? undefined : label}
        data-slot="segmented"
        className="inline-flex gap-0.5 rounded-sm border border-border-strong bg-surface p-0.5"
      >
        {options.map((option) => {
          const selected = option.value === value;
          return (
            <button
              key={option.value}
              type="button"
              aria-pressed={selected}
              onClick={() => onChange(option.value)}
              className={cn(
                "rounded-[3px] px-2.5 py-1 text-[13px] font-medium whitespace-nowrap text-text-2 hover:bg-sunken hover:text-text",
                selected &&
                  "bg-accent-soft font-semibold text-text shadow-[inset_0_0_0_1px_var(--border-strong)] hover:bg-accent-soft",
              )}
            >
              {option.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

export { Segmented };
