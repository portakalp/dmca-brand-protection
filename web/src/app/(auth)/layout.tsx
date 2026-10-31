import { Suspense } from "react";

import { Logo } from "@/components/shell/logo";

/** Sign-in pages: the wordmark, one panel on the page background, and who runs the service. */
export default function AuthLayout({ children }: LayoutProps<"/">) {
  return (
    <main className="flex min-h-svh flex-col items-center justify-center gap-5 bg-bg px-4 py-10">
      <div className="flex w-full max-w-[400px] flex-col gap-5">
        <Logo href="/login" />
        <div className="w-full rounded-lg border border-border bg-surface p-7 max-sm:p-5">
          <Suspense>{children}</Suspense>
        </div>
        <p className="text-[12.5px] text-balance text-text-3">
          Kalkan finds lookalike domains of your brand and has them taken down.
        </p>
      </div>
    </main>
  );
}
