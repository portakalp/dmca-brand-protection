"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, FileUp, OctagonAlert, Plus, Trash2 } from "lucide-react";
import { useId, useRef, useState } from "react";
import { toast } from "sonner";

import { Field, FormError } from "@/components/auth/form";
import { usePanel } from "@/components/shell/panel";
import { SimpleTable, Td, Th, Tr } from "@/components/simple-table";
import { errorMessage } from "@/components/states";
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
import type { WriteMode } from "@/lib/access";
import { ApiError, api, unwrap, type Schemas } from "@/lib/api/client";
import { keys } from "@/lib/api/queries";
import { importRows, type ImportRow } from "@/lib/csv";
import { WHITELIST_KIND_LABELS, count, plural } from "@/lib/format";

type Kind = Schemas["WhitelistKind"];
const ALL_BRANDS = "all";

function useRefreshWhitelist() {
  const client = useQueryClient();
  return () => {
    for (const key of [keys.whitelist, ["overview"], ["threats"]]) {
      void client.invalidateQueries({ queryKey: key });
    }
  };
}

function KindSelect({ value, onChange }: { value: Kind; onChange: (kind: Kind) => void }) {
  const id = useId();
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id} className="text-[13px] font-semibold text-text">
        Kind
      </Label>
      <Select value={value} onValueChange={(next) => onChange(next as Kind)}>
        <SelectTrigger id={id} className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {Object.entries(WHITELIST_KIND_LABELS).map(([kind, label]) => (
            <SelectItem key={kind} value={kind}>
              {label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

/** The pattern's error: the API validates it (public suffixes, shared platforms, duplicates). */
function patternError(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  if (error.status === 409) return "This domain is already on your whitelist.";
  if (error.status === 422 && typeof error.body.detail === "string") return error.body.detail;
  if (error.status === 422) return error.message;
  return null;
}

export function AddEntry({ mode }: { mode: WriteMode }) {
  const { me } = usePanel();
  const refresh = useRefreshWhitelist();
  const [open, setOpen] = useState(false);
  const [pattern, setPattern] = useState("");
  const [kind, setKind] = useState<Kind>("official");
  const [note, setNote] = useState("");
  const [brand, setBrand] = useState(ALL_BRANDS);
  const [missing, setMissing] = useState(false);
  const brandId = useId();

  const add = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/whitelist", {
          body: {
            pattern: pattern.trim(),
            kind,
            note: note.trim() || null,
            brand_id: brand === ALL_BRANDS ? null : brand,
          },
        }),
      ),
    onSuccess: (entry) => {
      setOpen(false);
      toast.success(`${entry.pattern} is on your whitelist`, {
        description: "Open threats on it are dismissed in a moment.",
      });
      refresh();
    },
  });

  if (mode === "hidden") return null;
  const fieldError = missing ? "Enter a domain." : patternError(add.error);
  const formError = add.isError && !patternError(add.error) ? errorMessage(add.error) : null;
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) {
          setPattern("");
          setNote("");
          setKind("official");
          setBrand(ALL_BRANDS);
          setMissing(false);
          add.reset();
        }
      }}
    >
      <DialogTrigger asChild>
        <Button disabled={mode === "disabled"}>
          <Plus aria-hidden="true" />
          Add domain
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-[440px]">
        <DialogHeader>
          <DialogTitle>Add a domain</DialogTitle>
          <DialogDescription>
            Kalkan never reports a whitelisted domain or its subdomains. Open threats on it are
            dismissed.
          </DialogDescription>
        </DialogHeader>
        <form
          noValidate
          className="flex flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (!pattern.trim()) {
              setMissing(true);
              return;
            }
            setMissing(false);
            add.mutate();
          }}
        >
          <FormError message={formError} />
          <Field
            id="whitelist-pattern"
            label="Domain"
            placeholder="partner.example"
            autoComplete="off"
            spellCheck={false}
            maxLength={253}
            value={pattern}
            error={fieldError ?? undefined}
            hint="A host such as mintera.pl or app.mintera.com; subdomains are included."
            onChange={(event) => setPattern(event.target.value)}
            className="font-mono"
          />
          <KindSelect value={kind} onChange={setKind} />
          {me.brands.length > 1 ? (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={brandId} className="text-[13px] font-semibold text-text">
                Brand
              </Label>
              <Select value={brand} onValueChange={setBrand}>
                <SelectTrigger id={brandId} className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL_BRANDS}>Every brand</SelectItem>
                  {me.brands.map((item) => (
                    <SelectItem key={item.id} value={item.id}>
                      {item.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          ) : null}
          <Field
            id="whitelist-note"
            label="Note (optional)"
            maxLength={500}
            value={note}
            onChange={(event) => setNote(event.target.value)}
          />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={add.isPending}>
              {add.isPending ? "Adding…" : "Add domain"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function DeleteEntry({ entry, mode }: { entry: Schemas["WhitelistItem"]; mode: WriteMode }) {
  const refresh = useRefreshWhitelist();
  const remove = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.DELETE("/api/v1/whitelist/{entry_id}", {
          params: { path: { entry_id: entry.id } },
        }),
      ),
    onSuccess: () => {
      toast.success(`${entry.pattern} was removed from your whitelist`);
      refresh();
    },
    onError: (error) => toast.error(errorMessage(error)),
  });

  if (mode === "hidden") return null;
  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={`Remove ${entry.pattern}`}
          disabled={mode === "disabled" || remove.isPending}
          className="text-text-3 hover:text-st-critical"
        >
          <Trash2 aria-hidden="true" />
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Remove {entry.pattern}?</AlertDialogTitle>
          <AlertDialogDescription>
            Kalkan may report this domain again if it looks like a threat. Threats dismissed because
            of this entry stay dismissed until the next check.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction variant="destructive" onClick={() => remove.mutate()}>
            Remove
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

// --- CSV import ------------------------------------------------------------------------------

type Preview = { rows: ImportRow[]; result: Schemas["ImportResult"] };

async function upload(file: File, dryRun: boolean) {
  return unwrap(
    await api.POST("/api/v1/whitelist/import", {
      params: { query: { dry_run: dryRun } },
      body: { file: file as unknown as string },
      bodySerializer: (body) => {
        const form = new FormData();
        form.append("file", body.file as unknown as Blob);
        return form;
      },
    }),
  );
}

const PREVIEW_ROWS = 50;

export function ImportCsv({ mode }: { mode: WriteMode }) {
  const refresh = useRefreshWhitelist();
  const input = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const check = useMutation({
    mutationFn: async (chosen: File) => {
      const rows = importRows(await chosen.text());
      if (rows === null) throw new Error("The file needs a header row with a 'pattern' column.");
      return { rows, result: await upload(chosen, true) };
    },
    onSuccess: setPreview,
    onError: (error) =>
      setProblem(error instanceof ApiError ? errorMessage(error) : (error as Error).message),
  });
  const commit = useMutation({
    mutationFn: async () => upload(file!, false),
    onSuccess: (result) => {
      setOpen(false);
      toast.success(
        `${count(result.added)} ${plural(result.added, "domain", "domains")} added to your whitelist`,
        {
          description: result.errors.length
            ? `${count(result.errors.length)} ${plural(result.errors.length, "row was", "rows were")} skipped because of errors.`
            : undefined,
        },
      );
      refresh();
    },
    onError: (error) => setProblem(errorMessage(error)),
  });

  if (mode === "hidden") return null;
  const errors = new Map(preview?.result.errors.map((error) => [error.line, error.error]) ?? []);
  const result = preview?.result;
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) {
          setFile(null);
          setPreview(null);
          setProblem(null);
          check.reset();
          commit.reset();
        }
      }}
    >
      <DialogTrigger asChild>
        <Button variant="outline" disabled={mode === "disabled"}>
          <FileUp aria-hidden="true" />
          Import CSV
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-[640px]">
        <DialogHeader>
          <DialogTitle>Import domains from a CSV file</DialogTitle>
          <DialogDescription>
            A header row with a <code className="font-mono">pattern</code> column, and optionally{" "}
            <code className="font-mono">kind</code>, <code className="font-mono">note</code> and{" "}
            <code className="font-mono">brand</code>. At most 5,000 rows and 1 MB. Nothing is saved
            until you confirm.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-4">
          <FormError message={problem} />
          <div className="flex flex-wrap items-center gap-3">
            <input
              ref={input}
              id="whitelist-csv"
              type="file"
              accept=".csv,text/csv"
              hidden
              onChange={(event) => {
                const chosen = event.target.files?.[0] ?? null;
                event.target.value = "";
                if (!chosen) return;
                setFile(chosen);
                setPreview(null);
                setProblem(null);
                check.mutate(chosen);
              }}
            />
            <Button
              type="button"
              variant="outline"

              onClick={() => input.current?.click()}
            >
              {file ? "Choose another file" : "Choose a file"}
            </Button>
            <span className="min-w-0 truncate text-[13px] text-text-2" aria-live="polite">
              {check.isPending ? "Checking the file…" : (file?.name ?? "No file chosen")}
            </span>
          </div>

          {preview && result ? (
            <>
              <p role="status" className="text-[13.5px] text-text">
                {count(result.added)} to add · {count(result.skipped)} already on the list ·{" "}
                <span className={result.errors.length ? "font-semibold" : undefined}>
                  {count(result.errors.length)} with errors
                </span>
              </p>
              <div className="max-h-72 overflow-y-auto rounded-sm border border-border [&_th]:border-t-0">
                <SimpleTable caption="Rows in the file" minWidth={480}>
                  <thead>
                    <tr>
                      <Th>Line</Th>
                      <Th>Domain</Th>
                      <Th>Kind</Th>
                      <Th>Result</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.rows.slice(0, PREVIEW_ROWS).map((row) => {
                      const error = errors.get(row.line);
                      return (
                        <Tr key={row.line}>
                          <Td label="Line" className="tabular">
                            {row.line}
                          </Td>
                          <Td primary className="font-mono text-[13px] font-medium text-text">
                            {row.pattern || "—"}
                          </Td>
                          <Td label="Kind">{row.kind || "other"}</Td>
                          <Td label="Result" className="text-text">
                            <span className="inline-flex items-start gap-[7px]">
                              {error ? (
                                <OctagonAlert
                                  aria-hidden="true"
                                  strokeWidth={1.5}
                                  className="mt-0.5 size-4 flex-none text-st-critical"
                                />
                              ) : (
                                <Check
                                  aria-hidden="true"
                                  strokeWidth={1.5}
                                  className="mt-0.5 size-4 flex-none text-st-neutral"
                                />
                              )}
                              {error ?? "OK"}
                            </span>
                          </Td>
                        </Tr>
                      );
                    })}
                  </tbody>
                </SimpleTable>
              </div>
              {preview.rows.length > PREVIEW_ROWS ? (
                <p className="text-[12.5px] text-text-3">
                  Showing the first {PREVIEW_ROWS} of {count(preview.rows.length)} rows.
                  {result.errors.some((error) => error.line > PREVIEW_ROWS + 1)
                    ? " Some rows further down have errors too."
                    : null}
                </p>
              ) : null}
            </>
          ) : null}
        </div>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button
            type="button"

            disabled={!result || result.added === 0 || commit.isPending}
            onClick={() => commit.mutate()}
          >
            {commit.isPending
              ? "Importing…"
              : `Import ${count(result?.added ?? 0)} ${plural(result?.added ?? 0, "domain", "domains")}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
