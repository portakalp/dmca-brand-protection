"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Contrast, LogOut } from "lucide-react";
import { useRouter } from "next/navigation";
import { useTheme } from "next-themes";
import { useSyncExternalStore } from "react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { api } from "@/lib/api/client";
import { initials } from "@/lib/format";

import { usePanel } from "./panel";

const noop = () => () => {};

/** True after hydration; the theme is only known in the browser. */
function useMounted() {
  return useSyncExternalStore(
    noop,
    () => true,
    () => false,
  );
}

/** "Theme: Light" / "Theme: Dark": a labelled button that switches between the two. */
export function ThemeButton() {
  const { resolvedTheme, setTheme } = useTheme();
  const mounted = useMounted();
  const dark = mounted && resolvedTheme === "dark";
  return (
    <button
      type="button"
      onClick={() => setTheme(dark ? "light" : "dark")}
      className="inline-flex items-center gap-2 self-start rounded-sm border border-border-strong px-2.5 py-[5px] text-[12.5px] font-semibold text-text-2 hover:bg-surface hover:text-text"
    >
      <Contrast aria-hidden="true" strokeWidth={1.5} className="size-4" />
      {mounted ? `Theme: ${dark ? "Dark" : "Light"}` : "Theme"}
    </button>
  );
}

/** The signed-in user at the foot of the sidebar; opens account, theme and sign-out. */
export function UserMenu() {
  const { me } = usePanel();
  const { theme, setTheme } = useTheme();
  const router = useRouter();
  const queryClient = useQueryClient();
  const name = me.user.display_name || me.user.email;

  const signOut = async () => {
    await api.POST("/api/v1/auth/logout").catch(() => undefined);
    queryClient.clear();
    router.replace("/login");
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        aria-label={`Account: ${name}`}
        className="-mx-1.5 flex min-w-0 items-center gap-2.5 rounded-sm px-1.5 py-1 text-left text-[13px] text-text hover:bg-sunken"
      >
        <span
          aria-hidden="true"
          className="inline-flex size-7 flex-none items-center justify-center rounded-full border border-border-strong text-[11px] font-semibold text-text-2"
        >
          {initials(me.user.display_name, me.user.email)}
        </span>
        <span className="truncate">{name}</span>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align="start" className="min-w-56">
        <DropdownMenuLabel className="flex flex-col gap-0.5">
          <span className="text-[13.5px] font-semibold text-text">{name}</span>
          <span className="text-[12px] font-normal text-text-3">{me.user.email}</span>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuLabel className="text-[12px] text-text-3">Theme</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={theme ?? "light"} onValueChange={setTheme}>
          <DropdownMenuRadioItem value="light">Light</DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="dark">Dark</DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="system">System</DropdownMenuRadioItem>
        </DropdownMenuRadioGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={signOut}>
          <LogOut aria-hidden="true" />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
