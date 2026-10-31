import { Suspense } from "react";

import { AppShell } from "@/components/shell/app-shell";

export default function AppLayout({ children }: LayoutProps<"/">) {
  return (
    <Suspense>
      <AppShell>{children}</AppShell>
    </Suspense>
  );
}
