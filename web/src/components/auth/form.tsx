"use client";

import { OctagonAlert } from "lucide-react";
import type { ReactNode } from "react";

import { Note } from "@/components/states";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError } from "@/lib/api/client";
import { cn } from "@/lib/utils";

export function AuthHeading({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-col gap-1.5">
      <h1 className="text-[22px] leading-[1.25]">{title}</h1>
      {children ? <p className="text-[13.5px] leading-[1.5] text-text-2">{children}</p> : null}
    </div>
  );
}

export function Field({
  id,
  label,
  error,
  hint,
  className,
  ...input
}: React.ComponentProps<"input"> & { id: string; label: string; error?: string; hint?: string }) {
  const describedBy = [error ? `${id}-error` : null, hint ? `${id}-hint` : null]
    .filter(Boolean)
    .join(" ");
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id} className="text-[13px] font-semibold text-text">
        {label}
      </Label>
      <Input
        id={id}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy || undefined}
        className={cn("h-10 text-[14px]", className)}
        {...input}
      />
      {hint ? (
        <p id={`${id}-hint`} className="text-[12.5px] text-text-3">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={`${id}-error`} className="flex items-start gap-1.5 text-[12.5px] text-text">
          <OctagonAlert
            aria-hidden="true"
            strokeWidth={1.5}
            className="mt-px size-3.5 flex-none text-st-critical"
          />
          {error}
        </p>
      ) : null}
    </div>
  );
}

export function FormError({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <Note tone="critical" role="alert" className="text-text">
      {message}
    </Note>
  );
}

/** A sign-in failure in words; the lock comes back with retry_after seconds. */
export function authMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 429) {
      const seconds = Number(error.body.retry_after ?? 0);
      const minutes = Math.max(1, Math.ceil(seconds / 60));
      return `Too many attempts. Try again in ${minutes} ${minutes === 1 ? "minute" : "minutes"}.`;
    }
    if (error.status >= 500) return "Something went wrong on our side. Please try again.";
    return error.message;
  }
  return "We couldn't reach Kalkan. Check your connection and try again.";
}
