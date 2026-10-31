"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { ApiError, api, unwrap } from "@/lib/api/client";

import { AuthHeading, Field, FormError, authMessage } from "./form";

const schema = z
  .object({
    display_name: z.string().trim().max(120, "Use at most 120 characters."),
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
type Values = z.infer<typeof schema>;

export function AcceptInvitation({ token }: { token: string }) {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { display_name: "", password: "", confirm: "" },
  });

  const onSubmit = form.handleSubmit(async (values) => {
    setError(null);
    try {
      unwrap(
        await api.POST("/api/v1/invitations/accept", {
          body: {
            token,
            password: values.password,
            display_name: values.display_name || null,
          },
        }),
      );
      // Invited users always set up TOTP before the first page (§17).
      router.push("/login/mfa?step=setup");
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.code === "token"
          ? "This invitation link is no longer valid. Ask the person who invited you for a new one."
          : authMessage(caught),
      );
    }
  });

  const { errors, isSubmitting } = form.formState;
  return (
    <>
      <AuthHeading title="Join your team on Kalkan">
        Choose a password. Next, you&apos;ll set up two-step verification.
      </AuthHeading>
      <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
        <FormError message={error} />
        <Field
          id="display_name"
          label="Your name (optional)"
          autoComplete="name"
          error={errors.display_name?.message}
          {...form.register("display_name")}
        />
        <Field
          id="password"
          label="Password"
          type="password"
          autoComplete="new-password"
          hint="At least 12 characters."
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
          {isSubmitting ? "Joining…" : "Join"}
        </Button>
      </form>
    </>
  );
}
