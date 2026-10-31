"use client";

import type { ReactNode } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError } from "@/lib/api/client";

export function AuthHeading({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-col gap-1">
      <h1 className="text-[20px] leading-7 font-semibold tracking-[-0.01em]">{title}</h1>
      {children ? <p className="text-[13.5px] text-text-secondary">{children}</p> : null}
    </div>
  );
}

export function Field({
  id,
  label,
  error,
  hint,
  ...input
}: React.ComponentProps<"input"> & { id: string; label: string; error?: string; hint?: string }) {
  const describedBy = [error ? `${id}-error` : null, hint ? `${id}-hint` : null]
    .filter(Boolean)
    .join(" ");
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id} className="text-[13px] font-medium">
        {label}
      </Label>
      <Input
        id={id}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy || undefined}
        className="h-10 rounded-control border-control-border bg-panel text-[14px] shadow-xs"
        {...input}
      />
      {hint ? (
        <p id={`${id}-hint`} className="text-[12px] text-text-tertiary">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={`${id}-error`} className="text-[12.5px] text-danger-fg">
          {error}
        </p>
      ) : null}
    </div>
  );
}

export function FormError({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p
      role="alert"
      className="rounded-control border border-[var(--status-active-border)] bg-[var(--status-active-bg)] px-3 py-2 text-[13px] text-danger-fg"
    >
      {message}
    </p>
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
