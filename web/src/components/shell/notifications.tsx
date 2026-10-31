"use client";

import { Bell } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

/** The bell; the list is empty until notifications exist (a shell for now). */
export function Notifications() {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          variant="control"
          size="icon-control"
          aria-label="Notifications"
          className="text-text-strong"
        >
          <Bell aria-hidden="true" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72">
        <p className="text-[13.5px] font-semibold">Notifications</p>
        <p className="mt-1 text-[13px] text-text-secondary">You&apos;re all caught up.</p>
      </PopoverContent>
    </Popover>
  );
}
