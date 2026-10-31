"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { api, unwrap } from "@/lib/api/client";
import { withParams } from "@/lib/url";

import { AuthHeading, Field, FormError, authMessage } from "./form";

const schema = z.object({
  email: z.email("Enter your work e-mail."),
  password: z.string().min(1, "Enter your password."),
});
type Values = z.infer<typeof schema>;

export function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const [error, setError] = useState<string | null>(null);
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { email: "", password: "" },
  });

  const onSubmit = form.handleSubmit(async (values) => {
    setError(null);
    try {
      const next = unwrap(await api.POST("/api/v1/auth/login", { body: values }));
      const step = next.mfa === "setup" ? "setup" : "verify";
      router.push(`/login/mfa${withParams("", { step, next: params.get("next") })}`);
    } catch (caught) {
      setError(authMessage(caught));
    }
  });

  const { errors, isSubmitting } = form.formState;
  return (
    <>
      <AuthHeading title="Sign in to Kalkan">Use the e-mail your team invited.</AuthHeading>
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
        <Field
          id="password"
          label="Password"
          type="password"
          autoComplete="current-password"
          error={errors.password?.message}
          {...form.register("password")}
        />
        <Link href="/forgot-password" className="-mt-2 self-end text-[13px] font-medium">
          Forgot password?
        </Link>
        <Button type="submit" size="xl" disabled={isSubmitting}>
          {isSubmitting ? "Signing in…" : "Continue"}
        </Button>
      </form>
    </>
  );
}
