import { describe, expect, it } from "vitest";

import { importRows, parseCsv } from "./csv";

describe("csv", () => {
  it("reads quotes, doubled quotes and CRLF", () => {
    expect(parseCsv('a,"b,c","say ""hi"""\r\nd,,\n')).toEqual([
      ["a", "b,c", 'say "hi"'],
      ["d", "", ""],
    ]);
  });

  it("numbers rows like the API: header is 1, empty lines skipped", () => {
    const rows = importRows("﻿Pattern,Kind,Note\none.example,partner,x\n\n,,\nthree.example,,\n");
    expect(rows?.map((row) => [row.line, row.pattern, row.kind])).toEqual([
      [2, "one.example", "partner"],
      [3, "", ""],
      [4, "three.example", ""],
    ]);
  });

  it("needs a pattern column", () => {
    expect(importRows("domain\nx.example\n")).toBeNull();
    expect(importRows("")).toBeNull();
  });
});
