"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CircleMinus, KeyRound, Mail, UserPlus } from "lucide-react";
import { useId, useMemo, useState, type ReactNode } from "react";
import { toast } from "sonner";

import { Field, FormError } from "@/components/auth/form";
import { RecoveryCodes } from "@/components/auth/recovery-codes";
import { PageHeader } from "@/components/shell/page-header";
import { usePanel } from "@/components/shell/panel";
import { SimpleTable, Td, Th, Tr } from "@/components/simple-table";
import {
  Card,
  ErrorState,
  LoadingState,
  PanelBody,
  PanelHeader,
  errorMessage,
} from "@/components/states";
import { Indicator } from "@/components/status-badge";
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
import { accessOf, type WriteMode } from "@/lib/access";
import { api, unwrap, type Schemas } from "@/lib/api/client";
import { keys, useSettings, useUsers } from "@/lib/api/queries";
import { longDay, todayIn } from "@/lib/period";

const ROLE_LABELS: Record<Schemas["UserRole"], string> = { owner: "Owner", viewer: "Viewer" };
const ROLE_HINTS: Record<Schemas["UserRole"], string> = {
  owner: "Manages the whitelist, users and settings",
  viewer: "Reads everything",
};

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
        <Button variant="outline">
          <KeyRound aria-hidden="true" />
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
                  variant="outline"

                  onClick={() => setOpen(false)}
                >
                  Cancel
                </Button>
                <Button
                  type="submit"

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

/** A label and its value: the spec's facts list (140px label column, dividers between rows). */
function Fact({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[140px_minmax(0,1fr)] gap-3 border-t border-border py-2.5 first:border-t-0 first:pt-0 max-sm:grid-cols-[110px_minmax(0,1fr)]">
      <dt className="text-[13px] text-text-2">{term}</dt>
      <dd className="m-0 font-medium text-text [overflow-wrap:anywhere]">{children}</dd>
    </div>
  );
}

function AccountCard() {
  const { me } = usePanel();
  const user = me.user;
  return (
    <Card aria-labelledby="account-title" className="flex flex-col">
      <PanelHeader
        id="account-title"
        title="Your account"
        description="The person signed in on this browser."
      />
      <PanelBody className="flex flex-col gap-5">
        <dl className="m-0 flex max-w-[560px] flex-col">
          <Fact term="Name">{user.display_name ?? "—"}</Fact>
          <Fact term="E-mail">{user.email}</Fact>
          <Fact term="Role">
            {ROLE_LABELS[user.role]}
            <span className="ml-2 font-normal text-text-3">{ROLE_HINTS[user.role]}</span>
          </Fact>
        </dl>
        <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3 border-t border-border pt-4">
          <div className="flex min-w-0 flex-[1_1_320px] flex-col gap-1">
            <h3 className="flex flex-wrap items-center gap-x-3 gap-y-1">
              Two-step verification
              {user.totp_enabled ? (
                <Indicator tone="good" className="text-[13px]">
                  On
                </Indicator>
              ) : (
                <Indicator tone="warning" className="text-[13px]">
                  Not set up
                </Indicator>
              )}
            </h3>
            <p className="max-w-prose text-[13px] text-text-2">
              Every sign-in asks for a code from your authenticator app. Recovery codes get you in
              if you lose your phone.
            </p>
          </div>
          {user.totp_enabled ? <NewRecoveryCodes /> : null}
        </div>
      </PanelBody>
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
        <Button disabled={mode === "disabled"}>
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
            <Label htmlFor={roleId} className="text-[13px] font-semibold text-text">
              Role
            </Label>
            <Select value={role} onValueChange={(value) => setRole(value as Schemas["UserRole"])}>
              <SelectTrigger id={roleId} className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="viewer">Viewer: reads everything</SelectItem>
                <SelectItem value="owner">Owner: also manages the whitelist and users</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={!email.trim() || invite.isPending}>
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
        <Button variant="outline" size="sm" disabled={disabled} aria-label={label}>
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
      <PanelHeader
        id="users-title"
        title="Users"
        description="Owners manage the whitelist, users and settings; viewers read everything."
        actions={<InviteUser mode={mode} />}
        className="pb-4"
      />
      {users.isPending ? (
        <LoadingState rows={2} label="Loading users" className="border-t border-border" />
      ) : users.isError ? (
        <ErrorState
          error={users.error}
          onRetry={() => users.refetch()}
          className="border-t border-border"
        />
      ) : (
        <SimpleTable caption="Users and invitations" minWidth={640}>
          <thead>
            <tr>
              <Th>Person</Th>
              <Th>Role</Th>
              <Th>Status</Th>
              <Th>Last sign-in</Th>
              {writes ? (
                <Th className="w-24">
                  <span className="sr-only">Actions</span>
                </Th>
              ) : null}
            </tr>
          </thead>
          <tbody>
            {users.data.users.map((user) => (
              <Tr key={user.id}>
                <Td primary>
                  <span className="flex min-w-0 flex-col">
                    <span className="truncate font-medium text-text">
                      {user.display_name ?? user.email}
                    </span>
                    {user.display_name ? (
                      <span className="truncate text-[12.5px] text-text-3">{user.email}</span>
                    ) : null}
                  </span>
                </Td>
                <Td label="Role">{ROLE_LABELS[user.role]}</Td>
                <Td label="Status">
                  {user.disabled ? (
                    <Indicator tone="neutral" icon={CircleMinus}>
                      Disabled
                    </Indicator>
                  ) : user.totp_enabled ? (
                    <Indicator tone="done">Active</Indicator>
                  ) : (
                    <Indicator tone="warning">Setting up</Indicator>
                  )}
                </Td>
                <Td label="Last sign-in" className="tabular whitespace-nowrap">
                  {user.last_login_at ? day(user.last_login_at, timeZone) : "Never"}
                </Td>
                {writes ? (
                  <Td action className="py-1.5 text-right">
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
                <Td primary>
                  <span className="block truncate font-medium text-text">{invitation.email}</span>
                </Td>
                <Td label="Role">{ROLE_LABELS[invitation.role]}</Td>
                <Td label="Status">
                  <Indicator tone="scheduled" icon={Mail}>
                    Invited
                  </Indicator>
                </Td>
                <Td label="Invitation" className="tabular whitespace-nowrap">
                  Link valid until {day(invitation.expires_at, timeZone)}
                </Td>
                {writes ? (
                  <Td action className="py-1.5 text-right">
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
          <Label htmlFor={zoneId} className="text-[13px] font-semibold text-text">
            Time zone
          </Label>
          <Select value={zone} onValueChange={setZone} disabled={locked}>
            <SelectTrigger id={zoneId} className="w-full">
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
          <p className="text-[12.5px] text-text-3">
            Days, charts and reports are counted in this time zone.
          </p>
        </div>
      </div>
      {mode !== "hidden" ? (
        <div>
          <Button type="submit" disabled={locked || !changed || save.isPending}>
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
    <Card aria-labelledby="prefs-title" className="flex flex-col">
      <PanelHeader
        id="prefs-title"
        title="Alerts and time zone"
        description="Where we announce new threats, and the time zone every day and report uses."
      />
      {settings.isPending ? (
        <LoadingState rows={2} label="Loading settings" />
      ) : settings.isError ? (
        <ErrorState error={settings.error} onRetry={() => settings.refetch()} />
      ) : (
        <PanelBody>
          <PreferencesForm
            key={JSON.stringify(settings.data)}
            settings={settings.data}
            mode={mode}
          />
        </PanelBody>
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
        <Card aria-labelledby="team-title" className="pb-5 max-sm:pb-4">
          <PanelHeader
            id="team-title"
            title="Team and alerts"
            description="Only owners manage users, the alert e-mail and the time zone. Ask an owner of your workspace to change them."
          />
        </Card>
      )}
    </>
  );
}
