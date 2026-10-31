"use client";

import { CalendarDays, Check, ChevronDown } from "lucide-react";
import { useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
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
import { cn } from "@/lib/utils";

import { usePanel } from "./panel";

function Option({
  selected,
  onSelect,
  children,
}: {
  selected: boolean;
  onSelect: () => void;
  children: string;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onSelect}
      className="flex min-h-9 w-full items-center justify-between gap-3 rounded-md px-2.5 text-left text-[13.5px] hover:bg-segment"
    >
      {children}
      {selected ? <Check aria-hidden="true" className="size-4 text-brand" /> : null}
    </button>
  );
}

/** The period of the page: 7, 30 or 90 days, a custom range, and on Threats all time. */
export function DateRangePicker({ range, fallback }: { range: Range; fallback: RangeKey }) {
  const { timeZone } = usePanel();
  const { update } = useUrlState();
  const [open, setOpen] = useState(false);
  const days = rangeDays(range, timeZone);
  const [from, setFrom] = useState(days?.from ?? "");
  const [to, setTo] = useState(days?.to ?? "");
  const id = useId();

  const choose = (next: Range) => {
    update({ ...rangeParams(next, fallback), page: null });
    setOpen(false);
  };
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
        <Button variant="control" size="control" aria-label={`Date range: ${rangeLabel(range)}`}>
          <CalendarDays aria-hidden="true" className="text-text-secondary" />
          {rangeLabel(range)}
          <ChevronDown aria-hidden="true" className="text-text-tertiary" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72 p-1.5">
        <div role="group" aria-label="Date range" className="flex flex-col">
          {PRESETS.map((key) => (
            <Option
              key={key}
              selected={range.key === key}
              onSelect={() => choose({ key, from: null, to: null })}
            >
              {`Last ${PRESET_DAYS[key]} days`}
            </Option>
          ))}
          {fallback === "all" ? (
            <Option
              selected={range.key === "all"}
              onSelect={() => choose({ key: "all", from: null, to: null })}
            >
              All time
            </Option>
          ) : null}
        </div>
        <form
          className={cn("mt-1.5 flex flex-col gap-2 border-t border-row-divider p-2.5")}
          onSubmit={(event) => {
            event.preventDefault();
            if (!customError) choose({ key: "custom", from, to });
          }}
        >
          <p className="text-[12px] font-medium text-text-secondary">Custom range</p>
          <div className="grid grid-cols-2 gap-2">
            <div className="flex flex-col gap-1">
              <Label htmlFor={`${id}-from`} className="text-[12px]">
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
              <Label htmlFor={`${id}-to`} className="text-[12px]">
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
            <p className="text-[12px] text-danger-fg" role="status">
              {customError}
            </p>
          ) : null}
          <Button type="submit" size="lg" disabled={!!customError}>
            Apply
          </Button>
        </form>
      </PopoverContent>
    </Popover>
  );
}
