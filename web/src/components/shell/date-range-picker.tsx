"use client";

import { CalendarDays } from "lucide-react";
import { useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Segmented, type SegmentedOption } from "@/components/ui/segmented";
import {
  MAX_DAYS,
  PRESETS,
  PRESET_DAYS,
  daysBetween,
  isIsoDate,
  rangeLabel,
  rangeParams,
  rangeDays,
  type Range,
  type RangeKey,
} from "@/lib/period";
import { useUrlState } from "@/lib/url";

import { usePanel } from "./panel";

type Choice = Exclude<RangeKey, "custom">;

/** The custom range: a "Custom" button that opens two date fields. */
function CustomRange({
  range,
  onApply,
}: {
  range: Range;
  onApply: (from: string, to: string) => void;
}) {
  const { timeZone } = usePanel();
  const [open, setOpen] = useState(false);
  const days = rangeDays(range, timeZone);
  const [from, setFrom] = useState(days?.from ?? "");
  const [to, setTo] = useState(days?.to ?? "");
  const id = useId();
  const selected = range.key === "custom";
  const customError =
    !isIsoDate(from) || !isIsoDate(to)
      ? "Pick both days."
      : from > to
        ? "The first day comes after the last."
        : daysBetween(from, to) > MAX_DAYS
          ? `Pick at most ${MAX_DAYS} days.`
          : null;

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        if (next) {
          setFrom(days?.from ?? "");
          setTo(days?.to ?? "");
        }
        setOpen(next);
      }}
    >
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          aria-pressed={selected}
          aria-label={selected ? `Custom range: ${rangeLabel(range)}` : "Custom range"}
          className={selected ? "bg-accent-soft hover:bg-accent-soft" : undefined}
        >
          <CalendarDays aria-hidden="true" className="text-text-2" />
          {selected ? rangeLabel(range) : "Custom"}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72 p-3">
        <form
          className="flex flex-col gap-2.5"
          onSubmit={(event) => {
            event.preventDefault();
            if (!customError) {
              onApply(from, to);
              setOpen(false);
            }
          }}
        >
          <p className="text-[13px] font-semibold">Custom range</p>
          <div className="grid grid-cols-2 gap-2">
            <div className="flex flex-col gap-1">
              <Label htmlFor={`${id}-from`} className="text-[12.5px] text-text-2">
                From
              </Label>
              <Input
                id={`${id}-from`}
                type="date"
                value={from}
                max={to || undefined}
                onChange={(event) => setFrom(event.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor={`${id}-to`} className="text-[12.5px] text-text-2">
                To
              </Label>
              <Input
                id={`${id}-to`}
                type="date"
                value={to}
                min={from || undefined}
                onChange={(event) => setTo(event.target.value)}
              />
            </div>
          </div>
          {customError && (from || to) ? (
            <p className="text-[12.5px] text-text-2" role="status">
              {customError}
            </p>
          ) : null}
          <Button type="submit" disabled={!!customError}>
            Apply
          </Button>
        </form>
      </PopoverContent>
    </Popover>
  );
}

/**
 * The page's one period control: a visible "Period" label, a group of 7 / 30 / 90 days (and
 * "All time" where the page defaults to it), and a custom range. It writes the range to the
 * URL, so every figure, chart and list on the page follows it.
 */
export function PeriodControl({ range, fallback }: { range: Range; fallback: RangeKey }) {
  const { update } = useUrlState();
  const choose = (next: Range) => update({ ...rangeParams(next, fallback), page: null });
  const options: SegmentedOption<Choice>[] = PRESETS.map((key) => ({
    value: key,
    label: `${PRESET_DAYS[key]} days`,
  }));
  if (fallback === "all") options.push({ value: "all", label: "All time" });

  // `contents`: the period and Custom wrap with the header's other buttons, not as one block.
  return (
    <div className="contents">
      <Segmented
        label="Period"
        showLabel
        options={options}
        value={range.key === "custom" ? null : range.key}
        onChange={(key) => choose({ key, from: null, to: null })}
      />
      <CustomRange range={range} onApply={(from, to) => choose({ key: "custom", from, to })} />
    </div>
  );
}

/** The old name. */
export const DateRangePicker = PeriodControl;
