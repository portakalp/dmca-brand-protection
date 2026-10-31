import { AlertCircle, Inbox } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api/client";
import { cn } from "@/lib/utils";

export function Card({
  className,
  children,
  ...props
}: React.ComponentProps<"section"> & { children: ReactNode }) {
  return (
    <section
      className={cn("min-w-0 rounded-card border border-card-border bg-card", className)}
      {...props}
    >
      {children}
    </section>
  );
}

export function CardTitle({
  id,
  title,
  subtitle,
}: {
  id?: string;
  title: string;
  subtitle?: ReactNode;
}) {
  return (
    <div className="min-w-0">
      <h2 id={id} className="text-[14px] leading-5 font-semibold">
        {title}
      </h2>
      {subtitle ? (
        <p className="mt-0.5 text-[12.5px] leading-[18px] text-text-tertiary">{subtitle}</p>
      ) : null}
    </div>
  );
}

/** A failed request in words; the API's own text when it explains a refusal or a bad value. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403 && error.code === "read_only") return error.message;
    if (error.status === 403) return "You don't have access to this.";
    if (error.status === 404) return "We couldn't find this.";
    if (error.status >= 500) return "Something went wrong on our side. Please try again.";
    return error.message;
  }
  return "We couldn't load this. Check your connection and try again.";
}

export function ErrorState({
  error,
  onRetry,
  className,
}: {
  error: unknown;
  onRetry?: () => void;
  className?: string;
}) {
  return (
    <div
      role="alert"
      className={cn(
        "flex flex-col items-center gap-3 px-6 py-10 text-center text-text-secondary",
        className,
      )}
    >
      <AlertCircle aria-hidden="true" className="size-6 text-danger" />
      <p className="max-w-sm text-[13.5px]">{errorMessage(error)}</p>
      {onRetry ? (
        <Button variant="control" size="control" onClick={onRetry}>
          Try again
        </Button>
      ) : null}
    </div>
  );
}

export function EmptyState({
  title,
  children,
  className,
}: {
  title: string;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center gap-2 px-6 py-10 text-center text-text-secondary",
        className,
      )}
    >
      <Inbox aria-hidden="true" className="size-6 text-text-tertiary" />
      <p className="text-[14px] font-semibold text-text">{title}</p>
      {children ? <div className="max-w-sm text-[13px]">{children}</div> : null}
    </div>
  );
}
