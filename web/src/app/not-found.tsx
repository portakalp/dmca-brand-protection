import Link from "next/link";

export default function NotFound() {
  return (
    <main className="flex min-h-svh flex-col items-center justify-center gap-3 px-4 text-center">
      <h1 className="text-[22px] font-semibold">Page not found</h1>
      <p className="text-text-secondary">This page doesn&apos;t exist or was moved.</p>
      <Link href="/overview">Go to the overview</Link>
    </main>
  );
}
