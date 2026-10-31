"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import QRCode from "qrcode";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { ApiError, api, unwrap, type Schemas } from "@/lib/api/client";
import { safeNext } from "@/lib/url";

import { AuthHeading, Field, FormError, authMessage } from "./form";
import { RecoveryCodes } from "./recovery-codes";

/** A pending session that expired or never existed: back to the password step. */
function expired(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401 && error.code !== "code";
}

function Expired() {
  return (
    <>
      <AuthHeading title="Your sign-in expired">
        For your security, the second step has to follow the password within 30 minutes.
      </AuthHeading>
      <Button asChild size="xl" className="w-full">
        <Link
          href="/login"
          className="text-primary-foreground no-underline hover:text-primary-foreground"
        >
          Sign in again
        </Link>
      </Button>
    </>
  );
}

function Verify({ onDone, onSetup }: { onDone: () => void; onSetup: () => void }) {
  const [recovery, setRecovery] = useState(false);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [gone, setGone] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!code.trim()) {
      setError(recovery ? "Enter one of your recovery codes." : "Enter the 6-digit code.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      unwrap(await api.POST("/api/v1/auth/mfa", { body: { code: code.trim() } }));
      onDone();
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === "setup_required") onSetup();
      else if (expired(caught)) setGone(true);
      else setError(authMessage(caught));
      setBusy(false);
    }
  };

  if (gone) return <Expired />;
  return (
    <>
      <AuthHeading title="Two-step verification">
        {recovery
          ? "Enter one of the recovery codes you saved when you set up two-step verification."
          : "Enter the 6-digit code from your authenticator app."}
      </AuthHeading>
      <form onSubmit={submit} noValidate className="flex flex-col gap-4">
        <FormError message={error} />
        <Field
          key={recovery ? "recovery" : "totp"}
          id="code"
          label={recovery ? "Recovery code" : "Authentication code"}
          autoComplete="one-time-code"
          inputMode={recovery ? "text" : "numeric"}
          maxLength={recovery ? 32 : 6}
          autoFocus
          value={code}
          onChange={(event) => setCode(event.target.value)}
        />
        <Button type="submit" size="xl" disabled={busy}>
          {busy ? "Checking…" : "Verify"}
        </Button>
        <Button
          type="button"
          variant="link"
          className="h-auto self-center p-0 text-[13px] text-accent"
          onClick={() => {
            setRecovery(!recovery);
            setCode("");
            setError(null);
          }}
        >
          {recovery ? "Use the authenticator app" : "Use a recovery code"}
        </Button>
      </form>
    </>
  );
}

function grouped(secret: string): string {
  return secret.match(/.{1,4}/g)?.join(" ") ?? secret;
}

function Setup({ onCodes }: { onCodes: (codes: string[]) => void }) {
  const [setup, setSetup] = useState<Schemas["TotpSetup"] | null>(null);
  const [qr, setQr] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const started = useRef(false);

  useEffect(() => {
    // One secret per visit: a second call would replace the one being scanned.
    if (started.current) return;
    started.current = true;
    api
      .POST("/api/v1/auth/totp/setup")
      .then(unwrap)
      .then(async (result) => {
        setSetup(result);
        setQr(await QRCode.toString(result.otpauth_uri, { type: "svg", margin: 1, width: 176 }));
      })
      .catch(setLoadError);
  }, []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!/^\d{6}$/.test(code.trim())) {
      setError("Enter the 6-digit code from the app.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = unwrap(
        await api.POST("/api/v1/auth/totp/verify", { body: { code: code.trim() } }),
      );
      onCodes(result.recovery_codes);
    } catch (caught) {
      setError(authMessage(caught));
      setBusy(false);
    }
  };

  if (expired(loadError)) return <Expired />;
  return (
    <>
      <AuthHeading title="Set up two-step verification">
        Scan the code with an authenticator app (1Password, Google Authenticator, Authy), then enter
        the 6-digit code it shows.
      </AuthHeading>
      {loadError ? <FormError message={authMessage(loadError)} /> : null}
      {setup ? (
        <div className="mb-5 flex flex-col items-center gap-3">
          {qr ? (
            <div
              role="img"
              aria-label="QR code for your authenticator app"
              className="rounded-sm border border-border bg-white p-2 [&_svg]:size-44"
              dangerouslySetInnerHTML={{ __html: qr }}
            />
          ) : null}
          <p className="text-center text-[12.5px] text-text-2">
            Can&apos;t scan it? Enter this key:
            <code className="mt-1 block font-mono text-[13px] font-medium break-all text-text">
              {grouped(setup.secret)}
            </code>
          </p>
        </div>
      ) : null}
      <form onSubmit={submit} noValidate className="flex flex-col gap-4">
        <FormError message={error} />
        <Field
          id="setup-code"
          label="Authentication code"
          autoComplete="one-time-code"
          inputMode="numeric"
          maxLength={6}
          value={code}
          onChange={(event) => setCode(event.target.value)}
          disabled={!setup}
        />
        <Button type="submit" size="xl" disabled={!setup || busy}>
          {busy ? "Checking…" : "Turn on two-step verification"}
        </Button>
      </form>
    </>
  );
}

export function SecondFactor() {
  const router = useRouter();
  const params = useSearchParams();
  const [step, setStep] = useState(params.get("step") === "setup" ? "setup" : "verify");
  const [codes, setCodes] = useState<string[] | null>(null);
  const next = safeNext(params.get("next"));
  const done = () => router.replace(next);

  if (codes) return <RecoveryCodes codes={codes} onContinue={done} />;
  if (step === "setup") return <Setup onCodes={setCodes} />;
  return <Verify onDone={done} onSetup={() => setStep("setup")} />;
}
