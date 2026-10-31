/**
 * Just enough CSV to preview a whitelist import (RFC 4180: quotes, doubled quotes, CRLF). The
 * API parses the file again and is the one that decides; this only shows the rows.
 */

export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  const source = text.replace(/^﻿/, "");
  for (let i = 0; i < source.length; i++) {
    const char = source[i];
    if (quoted) {
      if (char === '"' && source[i + 1] === '"') {
        cell += '"';
        i++;
      } else if (char === '"') {
        quoted = false;
      } else {
        cell += char;
      }
    } else if (char === '"' && cell === "") {
      quoted = true;
    } else if (char === ",") {
      row.push(cell);
      cell = "";
    } else if (char === "\n" || char === "\r") {
      if (char === "\r" && source[i + 1] === "\n") i++;
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
    } else {
      cell += char;
    }
  }
  if (cell !== "" || row.length) {
    row.push(cell);
    rows.push(row);
  }
  return rows;
}

export type ImportRow = {
  /** The row's number as the API counts it: the header is 1 and empty lines are skipped. */
  line: number;
  pattern: string;
  kind: string;
  note: string;
  brand: string;
};

/** The rows under the header; null when there is no pattern column. */
export function importRows(text: string): ImportRow[] | null {
  const [header, ...body] = parseCsv(text);
  if (!header) return null;
  const columns = header.map((name) => name.trim().toLowerCase());
  const at = (name: string) => columns.indexOf(name);
  if (at("pattern") < 0) return null;
  const cell = (values: string[], name: string) =>
    at(name) >= 0 ? (values[at(name)] ?? "").trim() : "";
  // Python's csv.DictReader skips empty lines only, so a line of commas still counts.
  return body
    .filter((values) => !(values.length === 1 && values[0] === ""))
    .map((values, index) => ({
      line: index + 2,
      pattern: cell(values, "pattern"),
      kind: cell(values, "kind"),
      note: cell(values, "note"),
      brand: cell(values, "brand"),
    }));
}
