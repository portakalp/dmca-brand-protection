import { Suspense } from "react";

import { Logo } from "@/components/shell/logo";

/** Sign-in pages: the logo and one card on the canvas. */
export default function AuthLayout({ children }: LayoutProps<"/">) {
  return (
    <main className="flex min-h-svh flex-col items-center justify-center gap-6 px-4 py-10">
      <Logo href="/login" />
      <div className="w-full max-w-[400px] rounded-panel border border-panel-border bg-panel p-6 shadow-panel sm:p-8">
        <Suspense>{children}</Suspense>
      </div>
    </main>
  );
}
