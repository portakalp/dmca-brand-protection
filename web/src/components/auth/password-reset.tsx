"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { api, unwrap } from "@/lib/api/client";

import { AuthHeading, Field, FormError, authMessage } from "./form";

const forgotSchema = z.object({ email: z.email("Enter your work e-mail.") });

/** Asks for a reset link; the API answers the same whether the e-mail has an account or not. */
export function ForgotPasswordForm() {
  const [sentTo, setSentTo] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const form = useForm<z.infer<typeof forgotSchema>>({
    resolver: zodResolver(forgotSchema),
    defaultValues: { email: "" },
  });
  const onSubmit = form.handleSubmit(async (values) => {
    setError(null);
    try {
      unwrap(await api.POST("/api/v1/auth/password/forgot", { body: values }));
      setSentTo(values.email);
    } catch (caught) {
      setError(authMessage(caught));
    }
  });

  if (sentTo) {
    return (
      <>
        <AuthHeading title="Check your e-mail">
          If {sentTo} has a Kalkan account, a link to choose a new password is on its way. It works
          once, for 24 hours.
        </AuthHeading>
        <Link href="/login" className="text-[13.5px] font-medium">
          Back to sign in
        </Link>
      </>
    );
  }
  const { errors, isSubmitting } = form.formState;
  return (
    <>
      <AuthHeading title="Reset your password">
        Enter the e-mail you sign in with and we&apos;ll send you a link.
      </AuthHeading>
      <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
        <FormError message={error} />
        <Field
          id="email"
          label="E-mail"
          type="email"
          autoComplete="username"
          autoFocus
          error={errors.email?.message}
          {...form.register("email")}
        />
        <Button type="submit" size="xl" disabled={isSubmitting}>
          {isSubmitting ? "Sending…" : "Send the link"}
        </Button>
        <Link href="/login" className="text-center text-[13.5px] font-medium">
          Back to sign in
        </Link>
      </form>
    </>
  );
}

const resetSchema = z
  .object({
    password: z
      .string()
      .min(12, "Use at least 12 characters.")
      .max(256, "Use at most 256 characters."),
    confirm: z.string(),
  })
  .refine((values) => values.password === values.confirm, {
    path: ["confirm"],
    message: "The passwords don't match.",
  });

/** Sets a new password from the e-mail's single-use link; every session of the user ends. */
export function ResetPasswordForm() {
  const params = useSearchParams();
  const token = params.get("token") ?? "";
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const form = useForm<z.infer<typeof resetSchema>>({
    resolver: zodResolver(resetSchema),
    defaultValues: { password: "", confirm: "" },
  });
  const onSubmit = form.handleSubmit(async (values) => {
    setError(null);
    try {
      unwrap(
        await api.POST("/api/v1/auth/password/reset", {
          body: { token, password: values.password },
        }),
      );
      setDone(true);
    } catch (caught) {
      setError(authMessage(caught));
    }
  });

  if (token.length < 10) {
    return (
      <>
        <AuthHeading title="This link doesn't work">
          Open the link from the e-mail again, or ask for a new one.
        </AuthHeading>
        <Link href="/forgot-password" className="text-[13.5px] font-medium">
          Ask for a new link
        </Link>
      </>
    );
  }
  if (done) {
    return (
      <>
        <AuthHeading title="Your password is changed">
          You were signed out everywhere. Sign in with the new password.
        </AuthHeading>
        <Button asChild size="xl" className="w-full">
          <Link
            href="/login"
            className="text-primary-foreground no-underline hover:text-primary-foreground"
          >
            Sign in
          </Link>
        </Button>
      </>
    );
  }
  const { errors, isSubmitting } = form.formState;
  return (
    <>
      <AuthHeading title="Choose a new password">Use at least 12 characters.</AuthHeading>
      <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
        <FormError message={error} />
        <Field
          id="password"
          label="New password"
          type="password"
          autoComplete="new-password"
          autoFocus
          error={errors.password?.message}
          {...form.register("password")}
        />
        <Field
          id="confirm"
          label="Repeat the password"
          type="password"
          autoComplete="new-password"
          error={errors.confirm?.message}
          {...form.register("confirm")}
        />
        <Button type="submit" size="xl" disabled={isSubmitting}>
          {isSubmitting ? "Saving…" : "Save the password"}
        </Button>
      </form>
    </>
  );
}
