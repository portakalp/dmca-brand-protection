import { cn } from "cn";

/**
 * A thin single-measure bar (the 83% takedown share, visual similarity). Decorative: the
 * percentage is always printed next to it, so the bar is aria-hidden. "takedown" is the blue
 * that means takedowns everywhere; "neutral" is for measures that are not takedowns.
 */
function Meter({
  value,
  tone = "takedown",
  className,
}: {
  /** 0 to 100 */
  value: number;
  tone?: "takedown" | "neutral";
  className?: string;
}) {
  const width = Math.max(0, Math.min(100, value));
  return (
    <div
      aria-hidden="true"
      data-slot="meter"
      className={cn("h-1.5 overflow-hidden rounded-[3px] bg-grid", className)}
    >
      <div
        className={cn(
          "h-full rounded-[3px]",
          tone === "takedown" ? "bg-takedown" : "bg-meter-neutral",
        )}
        style={{ width: `${width}%` }}
      />
    </div>
  );
}

export { Meter };
