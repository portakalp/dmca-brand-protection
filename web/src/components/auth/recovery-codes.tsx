"use client";

import { Check, Copy, Download } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";

import { AuthHeading } from "./form";

/** The ten recovery codes, shown once after TOTP is set up or after new ones are made. */
export function RecoveryCodes({
  codes,
  onContinue,
  continueLabel = "Continue to Kalkan",
}: {
  codes: string[];
  onContinue: () => void;
  continueLabel?: string;
}) {
  const [copied, setCopied] = useState(false);
  const [saved, setSaved] = useState(false);
  const text = `Kalkan recovery codes\n\n${codes.join("\n")}\n`;

  const copy = async () => {
    await navigator.clipboard.writeText(text);
    setCopied(true);
  };
  const download = () => {
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "kalkan-recovery-codes.txt";
    link.click();
    URL.revokeObjectURL(url);
  };

  return (
    <>
      <AuthHeading title="Save your recovery codes">
        Each code signs you in once if you lose your phone. Keep them somewhere safe; we won&apos;t
        show them again.
      </AuthHeading>
      <ul
        aria-label="Recovery codes"
        className="tabular mb-4 grid grid-cols-2 gap-x-4 gap-y-1.5 rounded-sm border border-border bg-sunken px-4 py-3.5 font-mono text-[13.5px] text-text"
      >
        {codes.map((code) => (
          <li key={code}>{code}</li>
        ))}
      </ul>
      <div className="mb-5 flex gap-2">
        <Button type="button" variant="outline" className="flex-1" onClick={copy}>
          {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
          {copied ? "Copied" : "Copy"}
        </Button>
        <Button
          type="button"
          variant="outline"

          className="flex-1"
          onClick={download}
        >
          <Download aria-hidden="true" />
          Download
        </Button>
      </div>
      <label className="mb-4 flex cursor-pointer items-start gap-2.5 text-[13.5px] text-text">
        <input
          type="checkbox"
          checked={saved}
          onChange={(event) => setSaved(event.target.checked)}
          className="mt-0.5 size-4 accent-[var(--accent)]"
        />
        I saved these codes somewhere safe.
      </label>
      <Button type="button" size="xl" className="w-full" disabled={!saved} onClick={onContinue}>
        {continueLabel}
      </Button>
    </>
  );
}
