import * as React from "react";
import { cn } from "cn";

function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(
        "flex field-sizing-content min-h-16 w-full rounded-sm border border-input bg-surface px-3 py-2 text-[14px] text-text transition-colors placeholder:text-text-3 hover:border-text-3 focus-visible:border-accent disabled:cursor-not-allowed disabled:bg-sunken disabled:opacity-60 aria-invalid:border-destructive",
        className,
      )}
      {...props}
    />
  );
}

export { Textarea };
