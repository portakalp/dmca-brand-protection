import { Shield } from "lucide-react";
import Link from "next/link";

/** The wordmark: a navy square with a shield, and the name in Red Hat Display. */
export function Logo({ href = "/overview" }: { href?: string }) {
  return (
    <Link
      href={href}
      className="inline-flex min-h-10 items-center gap-2.5 font-display text-[18px] font-bold text-text no-underline hover:text-text hover:no-underline"
    >
      <span
        aria-hidden="true"
        className="inline-flex size-[26px] items-center justify-center rounded-[5px] bg-accent text-on-accent"
      >
        <Shield className="size-4" strokeWidth={1.75} />
      </span>
      Kalkan
    </Link>
  );
}
