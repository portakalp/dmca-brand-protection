import { ShieldCheck } from "lucide-react";
import Link from "next/link";

export function Logo({ href = "/overview" }: { href?: string }) {
  return (
    <Link
      href={href}
      className="flex min-h-11 items-center gap-2.5 px-1.5 text-text no-underline hover:text-text hover:no-underline"
    >
      <span
        aria-hidden="true"
        className="inline-flex size-[30px] items-center justify-center rounded-[9px] bg-brand text-white"
      >
        <ShieldCheck className="size-[18px]" />
      </span>
      <span className="text-[16px] font-semibold tracking-[-0.01em]">Kalkan</span>
    </Link>
  );
}
