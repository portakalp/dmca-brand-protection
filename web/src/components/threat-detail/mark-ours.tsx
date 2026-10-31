"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CircleCheck } from "lucide-react";
import { useId, useState } from "react";
import { toast } from "sonner";

import { FormError } from "@/components/auth/form";
import { errorMessage } from "@/components/states";
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
import { Textarea } from "@/components/ui/textarea";
import type { WriteMode } from "@/lib/access";
import { api, unwrap, type Schemas } from "@/lib/api/client";
import { keys } from "@/lib/api/queries";
import { WHITELIST_KIND_LABELS } from "@/lib/format";

type Kind = Schemas["WhitelistKind"];

/**
 * "This is our site" (product.md §11): owners only. The dialog says what happens before anything
 * does; the API adds the whitelist entry and the worker dismisses and retracts.
 */
export function MarkOurs({ detail, mode }: { detail: Schemas["ThreatDetail"]; mode: WriteMode }) {
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<Kind>("official");
  const [note, setNote] = useState("");
  const kindId = useId();
  const noteId = useId();
  const domain = detail.threat.domain;
  const sent = detail.notices.some((notice) => notice.sent_at !== null);

  const mark = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/threats/{case_id}/mark-ours", {
          params: { path: { case_id: detail.threat.id } },
          body: { kind, note: note.trim() || null },
        }),
      ),
    onSuccess: () => {
      setOpen(false);
      toast.success(`${domain} is on your whitelist`, {
        description: sent
          ? "The threat is being dismissed and the notices withdrawn."
          : "The threat is being dismissed.",
      });
      for (const key of [
        keys.threat(detail.threat.id),
        ["threats"],
        ["overview"],
        keys.whitelist,
      ]) {
        void client.invalidateQueries({ queryKey: key });
      }
    },
  });

  if (mode === "hidden") return null;
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) mark.reset();
      }}
    >
      <DialogTrigger asChild>
        <Button
          variant="control"
          size="control"
          disabled={mode === "disabled"}
          className="min-h-10 pr-3.5 pl-3"
        >
          <CircleCheck aria-hidden="true" className="text-text-secondary" />
          This is our site
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-[480px]">
        <DialogHeader>
          <DialogTitle>Is {domain} your site?</DialogTitle>
          <DialogDescription asChild>
            <div className="flex flex-col gap-2 text-[13.5px] text-text-secondary">
              <p>If you confirm:</p>
              <ul className="ml-4 list-disc space-y-1">
                <li>
                  <span className="font-mono text-[12.5px]">{domain}</span> and its subdomains are
                  added to your whitelist.
                </li>
                <li>This threat is dismissed and no new notices go out for it.</li>
                {sent ? (
                  <li>
                    The notices already sent are withdrawn: the same recipients get a retraction
                    e-mail, and the Kalkan team withdraws web form reports.
                  </li>
                ) : null}
              </ul>
            </div>
          </DialogDescription>
        </DialogHeader>
        <form
          className="flex flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            mark.mutate();
          }}
        >
          <FormError message={mark.isError ? errorMessage(mark.error) : null} />
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={kindId} className="text-[13px] font-medium">
              What is it?
            </Label>
            <Select value={kind} onValueChange={(value) => setKind(value as Kind)}>
              <SelectTrigger id={kindId} className="min-h-10 w-full rounded-control">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {Object.entries(WHITELIST_KIND_LABELS).map(([value, label]) => (
                  <SelectItem key={value} value={value}>
                    {label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={noteId} className="text-[13px] font-medium">
              Note <span className="font-normal text-text-tertiary">(optional)</span>
            </Label>
            <Textarea
              id={noteId}
              value={note}
              maxLength={500}
              rows={2}
              onChange={(event) => setNote(event.target.value)}
              placeholder="For example: our new domain for Poland"
            />
          </div>
          <DialogFooter>
            <Button type="button" variant="control" size="control" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" size="control" disabled={mark.isPending}>
              {mark.isPending ? "Saving…" : "Yes, it's our site"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
