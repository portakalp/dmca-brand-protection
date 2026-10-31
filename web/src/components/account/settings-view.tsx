"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { KeyRound, UserPlus } from "lucide-react";
import { useId, useMemo, useState } from "react";
import { toast } from "sonner";

import { Field, FormError } from "@/components/auth/form";
import { RecoveryCodes } from "@/components/auth/recovery-codes";
import { Pill } from "@/components/pill";
import { PageHeader } from "@/components/shell/page-header";
import { usePanel } from "@/components/shell/panel";
import { SimpleTable, Td, Th, Tr } from "@/components/simple-table";
import { Card, CardTitle, ErrorState, errorMessage } from "@/components/states";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { accessOf, type WriteMode } from "@/lib/access";
import { api, unwrap, type Schemas } from "@/lib/api/client";
import { keys, useSettings, useUsers } from "@/lib/api/queries";
import { longDay, todayIn } from "@/lib/period";

const ROLE_LABELS: Record<Schemas["UserRole"], string> = { owner: "Owner", viewer: "Viewer" };

function day(at: string, timeZone: string): string {
  return longDay(todayIn(timeZone, new Date(at)));
}

// --- two-step verification -------------------------------------------------------------------

function NewRecoveryCodes() {
  const [open, setOpen] = useState(false);
  const [code, setCode] = useState("");
  const [codes, setCodes] = useState<string[] | null>(null);
  const renew = useMutation({
    mutationFn: async () =>
      unwrap(await api.POST("/api/v1/auth/recovery-codes", { body: { code: code.trim() } })),
    onSuccess: (result) => setCodes(result.recovery_codes),
  });
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) {
          setCode("");
          setCodes(null);
          renew.reset();
        }
      }}
    >
      <DialogTrigger asChild>
        <Button variant="control" size="control">
          <KeyRound aria-hidden="true" className="text-text-secondary" />
          New recovery codes
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-[440px]">
        {codes ? (
          <>
            <DialogTitle className="sr-only">Your new recovery codes</DialogTitle>
            <DialogDescription className="sr-only">The old codes no longer work.</DialogDescription>
            <RecoveryCodes codes={codes} continueLabel="Done" onContinue={() => setOpen(false)} />
          </>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>Get new recovery codes</DialogTitle>
              <DialogDescription>
                Enter the 6-digit code from your authenticator app. Your old recovery codes stop
                working.
              </DialogDescription>
            </DialogHeader>
            <form
              className="flex flex-col gap-4"
              onSubmit={(event) => {
                event.preventDefault();
                renew.mutate();
              }}
            >
              <Field
                id="renew-code"
                label="Code"
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={8}
                value={code}
                error={renew.isError ? errorMessage(renew.error) : undefined}
                onChange={(event) => setCode(event.target.value)}
                className="tabular font-mono"
              />
              <DialogFooter>
                <Button
                  type="button"
                  variant="control"
                  size="control"
                  onClick={() => setOpen(false)}
                >
                  Cancel
                </Button>
                <Button
                  type="submit"
                  size="control"
                  disabled={code.trim().length < 6 || renew.isPending}
                >
                  {renew.isPending ? "Checking…" : "Get new codes"}
                </Button>
              </DialogFooter>
            </form>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

function AccountCard() {
  const { me } = usePanel();
  const user = me.user;
  return (
    <Card aria-labelledby="account-title" className="flex flex-col gap-4 px-5 py-[18px]">
      <CardTitle id="account-title" title="Your account" />
      <dl className="m-0 grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-4">
        <div>
          <dt className="text-[12.5px] text-text-tertiary">Name</dt>
          <dd className="m-0 text-[14px] font-medium">{user.display_name ?? "—"}</dd>
        </div>
        <div>
          <dt className="text-[12.5px] text-text-tertiary">E-mail</dt>
          <dd className="m-0 text-[14px] font-medium [overflow-wrap:anywhere]">{user.email}</dd>
        </div>
        <div>
          <dt className="text-[12.5px] text-text-tertiary">Role</dt>
          <dd className="m-0 text-[14px] font-medium">{ROLE_LABELS[user.role]}</dd>
        </div>
      </dl>
      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-row-divider pt-4">
        <div className="flex min-w-0 flex-col gap-0.5">
          <p className="flex items-center gap-2 text-[14px] font-semibold">
            Two-step verification
            <Pill tone={user.totp_enabled ? "success" : "waiting"}>
              {user.totp_enabled ? "On" : "Not set up"}
            </Pill>
          </p>
          <p className="text-[13px] text-text-secondary">
            Every sign-in asks for a code from your authenticator app. Recovery codes get you in if
            you lose your phone.
          </p>
        </div>
        {user.totp_enabled ? <NewRecoveryCodes /> : null}
      </div>
    </Card>
  );
}

// --- users and invitations -------------------------------------------------------------------

function InviteUser({ mode }: { mode: WriteMode }) {
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Schemas["UserRole"]>("viewer");
  const roleId = useId();
  const invite = useMutation({
    mutationFn: async () =>
      unwrap(await api.POST("/api/v1/users", { body: { email: email.trim(), role } })),
    onSuccess: (invitation) => {
      setOpen(false);
      toast.success(`Invitation sent to ${invitation.email}`, {
        description: "The link works once, for 24 hours.",
      });
      void client.invalidateQueries({ queryKey: keys.users });
    },
  });
  if (mode === "hidden") return null;
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) {
          setEmail("");
          setRole("viewer");
          invite.reset();
        }
      }}
    >
      <DialogTrigger asChild>
        <Button size="control" disabled={mode === "disabled"}>
          <UserPlus aria-hidden="true" />
          Invite
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-[420px]">
        <DialogHeader>
          <DialogTitle>Invite someone</DialogTitle>
          <DialogDescription>
            They get an e-mail with a link to choose a password and set up two-step verification.
          </DialogDescription>
        </DialogHeader>
        <form
          noValidate
          className="flex flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            invite.mutate();
          }}
        >
          <FormError message={invite.isError ? errorMessage(invite.error) : null} />
          <Field
            id="invite-email"
            label="Work e-mail"
            type="email"
            autoComplete="off"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={roleId} className="text-[13px] font-medium">
              Role
            </Label>
            <Select value={role} onValueChange={(value) => setRole(value as Schemas["UserRole"])}>
              <SelectTrigger id={roleId} className="min-h-10 w-full rounded-control">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="viewer">Viewer: reads everything</SelectItem>
                <SelectItem value="owner">Owner: also manages the whitelist and users</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <DialogFooter>
            <Button type="button" variant="control" size="control" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" size="control" disabled={!email.trim() || invite.isPending}>
              {invite.isPending ? "Sending…" : "Send invitation"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function Confirm({
  label,
  title,
  text,
  action,
  onConfirm,
  disabled,
}: {
  label: string;
  title: string;
  text: string;
  action: string;
  onConfirm: () => void;
  disabled: boolean;
}) {
  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>
        <Button variant="control" size="control" disabled={disabled} aria-label={label}>
          {action}
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>{text}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction variant="destructive" onClick={onConfirm}>
            {action}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

function UsersCard({ mode }: { mode: WriteMode }) {
  const { me, timeZone } = usePanel();
  const client = useQueryClient();
  const users = useUsers();
  const refresh = () => void client.invalidateQueries({ queryKey: keys.users });
  const disable = useMutation({
    mutationFn: async (user: Schemas["UserItem"]) =>
      unwrap(
        await api.DELETE("/api/v1/users/{user_id}", { params: { path: { user_id: user.id } } }),
      ),
    onSuccess: (_, user) => {
      toast.success(`${user.email} can no longer sign in`);
      refresh();
    },
    onError: (error) => toast.error(errorMessage(error)),
  });
  const withdraw = useMutation({
    mutationFn: async (invitation: Schemas["InvitationItem"]) =>
      unwrap(
        await api.DELETE("/api/v1/users/invitations/{invitation_id}", {
          params: { path: { invitation_id: invitation.id } },
        }),
      ),
    onSuccess: (_, invitation) => {
      toast.success(`The invitation to ${invitation.email} was withdrawn`);
      refresh();
    },
    onError: (error) => toast.error(errorMessage(error)),
  });
  const writes = mode !== "hidden";

  return (
    <Card aria-labelledby="users-title" className="flex flex-col overflow-hidden">
      <div className="flex flex-wrap items-start justify-between gap-3 px-5 pt-[18px] pb-4">
        <CardTitle
          id="users-title"
          title="Users"
          subtitle="Owners manage the whitelist, users and settings; viewers read everything."
        />
        <InviteUser mode={mode} />
      </div>
      {users.isPending ? (
        <div aria-busy="true" aria-label="Loading users" className="flex flex-col gap-2 px-5 pb-5">
          <Skeleton className="h-11" />
          <Skeleton className="h-11" />
        </div>
      ) : users.isError ? (
        <ErrorState error={users.error} onRetry={() => users.refetch()} />
      ) : (
        <SimpleTable caption="Users and invitations" minWidth={640}>
          <thead>
            <tr>
              <Th>Person</Th>
              <Th>Role</Th>
              <Th>Status</Th>
              <Th>Last sign-in</Th>
              {writes ? (
                <Th>
                  <span className="sr-only">Actions</span>
                </Th>
              ) : null}
            </tr>
          </thead>
          <tbody>
            {users.data.users.map((user) => (
              <Tr key={user.id}>
                <Td>
                  <span className="flex flex-col">
                    <span className="font-medium text-text">{user.display_name ?? user.email}</span>
                    {user.display_name ? (
                      <span className="text-[12.5px] text-text-tertiary">{user.email}</span>
                    ) : null}
                  </span>
                </Td>
                <Td>{ROLE_LABELS[user.role]}</Td>
                <Td>
                  {user.disabled ? (
                    <Pill>Disabled</Pill>
                  ) : user.totp_enabled ? (
                    <Pill tone="success">Active</Pill>
                  ) : (
                    <Pill tone="waiting">Setting up</Pill>
                  )}
                </Td>
                <Td className="tabular whitespace-nowrap">
                  {user.last_login_at ? day(user.last_login_at, timeZone) : "Never"}
                </Td>
                {writes ? (
                  <Td className="py-1.5 text-right">
                    {user.id !== me.user.id && !user.disabled ? (
                      <Confirm
                        label={`Disable ${user.email}`}
                        title={`Disable ${user.email}?`}
                        text="They are signed out everywhere and can no longer sign in. Their past actions stay in the log."
                        action="Disable"
                        disabled={mode === "disabled" || disable.isPending}
                        onConfirm={() => disable.mutate(user)}
                      />
                    ) : null}
                  </Td>
                ) : null}
              </Tr>
            ))}
            {users.data.invitations.map((invitation) => (
              <Tr key={invitation.id}>
                <Td>
                  <span className="font-medium text-text">{invitation.email}</span>
                </Td>
                <Td>{ROLE_LABELS[invitation.role]}</Td>
                <Td>
                  <Pill tone="scheduled">Invited</Pill>
                </Td>
                <Td className="tabular whitespace-nowrap">
                  Link valid until {day(invitation.expires_at, timeZone)}
                </Td>
                {writes ? (
                  <Td className="py-1.5 text-right">
                    <Confirm
                      label={`Withdraw the invitation to ${invitation.email}`}
                      title={`Withdraw the invitation to ${invitation.email}?`}
                      text="The link in the e-mail stops working."
                      action="Withdraw"
                      disabled={mode === "disabled" || withdraw.isPending}
                      onConfirm={() => withdraw.mutate(invitation)}
                    />
                  </Td>
                ) : null}
              </Tr>
            ))}
          </tbody>
        </SimpleTable>
      )}
    </Card>
  );
}

// --- alerts and time zone --------------------------------------------------------------------

function timeZones(current: string): string[] {
  const zones = Intl.supportedValuesOf("timeZone");
  return zones.includes(current) ? zones : [current, ...zones];
}

function PreferencesForm({
  settings,
  mode,
}: {
  settings: Schemas["SettingsOut"];
  mode: WriteMode;
}) {
  const client = useQueryClient();
  const [email, setEmail] = useState(settings.alert_email ?? "");
  const [zone, setZone] = useState(settings.timezone);
  const zones = useMemo(() => timeZones(settings.timezone), [settings.timezone]);
  const zoneId = useId();
  const locked = mode !== "enabled";
  const save = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.PATCH("/api/v1/settings", {
          body: email.trim()
            ? { alert_email: email.trim(), clear_alert_email: false, timezone: zone }
            : { clear_alert_email: true, timezone: zone },
        }),
      ),
    onSuccess: (saved) => {
      setEmail(saved.alert_email ?? "");
      toast.success("Settings saved");
      for (const key of [keys.settings, keys.me, ["overview"], ["threats"]]) {
        void client.invalidateQueries({ queryKey: key });
      }
    },
  });
  const changed = email.trim() !== (settings.alert_email ?? "") || zone !== settings.timezone;

  return (
    <form
      noValidate
      className="flex flex-col gap-4"
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <FormError message={save.isError ? errorMessage(save.error) : null} />
      <div className="grid grid-cols-[repeat(auto-fit,minmax(240px,1fr))] gap-4">
        <Field
          id="alert-email"
          label="Alert e-mail"
          type="email"
          value={email}
          disabled={locked}
          hint="Where new threats are announced. Leave it empty to turn alerts off."
          onChange={(event) => setEmail(event.target.value)}
        />
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={zoneId} className="text-[13px] font-medium">
            Time zone
          </Label>
          <Select value={zone} onValueChange={setZone} disabled={locked}>
            <SelectTrigger id={zoneId} className="min-h-10 w-full rounded-control">
              <SelectValue />
            </SelectTrigger>
            <SelectContent className="max-h-72">
              {zones.map((name) => (
                <SelectItem key={name} value={name}>
                  {name.replaceAll("_", " ")}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="text-[12px] text-text-tertiary">
            Days, charts and reports are counted in this time zone.
          </p>
        </div>
      </div>
      {mode !== "hidden" ? (
        <div>
          <Button type="submit" size="control" disabled={locked || !changed || save.isPending}>
            {save.isPending ? "Saving…" : "Save changes"}
          </Button>
        </div>
      ) : null}
    </form>
  );
}

function PreferencesCard({ mode }: { mode: WriteMode }) {
  const settings = useSettings();
  return (
    <Card aria-labelledby="prefs-title" className="flex flex-col gap-4 px-5 py-[18px]">
      <CardTitle id="prefs-title" title="Alerts and time zone" />
      {settings.isPending ? (
        <Skeleton aria-label="Loading settings" className="h-20" />
      ) : settings.isError ? (
        <ErrorState error={settings.error} onRetry={() => settings.refetch()} />
      ) : (
        <PreferencesForm key={JSON.stringify(settings.data)} settings={settings.data} mode={mode} />
      )}
    </Card>
  );
}

export function SettingsView() {
  const { me } = usePanel();
  const access = accessOf(me);
  return (
    <>
      <PageHeader title="Settings" subtitle="Your account, your team and how Kalkan reaches you." />
      <AccountCard />
      {access.owner ? (
        <>
          <UsersCard mode={access.users} />
          <PreferencesCard mode={access.settings} />
        </>
      ) : (
        <Card className="px-5 py-[18px]">
          <CardTitle
            title="Team and alerts"
            subtitle="Only owners manage users, the alert e-mail and the time zone."
          />
        </Card>
      )}
    </>
  );
}
