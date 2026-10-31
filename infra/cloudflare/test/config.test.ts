import { describe, expect, it } from "vitest";

import rawConfig from "../wrangler.jsonc?raw";

interface WranglerConfig {
  name: string;
  main: string;
  workers_dev: boolean;
  preview_urls: boolean;
  routes: { pattern: string; custom_domain?: boolean }[];
  vars: Record<string, string>;
  observability: { logs: { invocation_logs: boolean }; traces: { enabled: boolean } };
}

/** JSONC to JSON: drops comments outside strings and trailing commas. */
function parseJsonc(text: string): unknown {
  let out = "";
  let inString = false;
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (inString) {
      out += char;
      if (char === "\\") {
        out += text[++i] ?? "";
      } else if (char === '"') {
        inString = false;
      }
    } else if (char === '"') {
      inString = true;
      out += char;
    } else if (char === "/" && text[i + 1] === "/") {
      while (i < text.length && text[i] !== "\n") i++;
      out += "\n";
    } else if (char === "/" && text[i + 1] === "*") {
      const end = text.indexOf("*/", i + 2);
      i = end === -1 ? text.length : end + 1;
    } else {
      out += char;
    }
  }
  return JSON.parse(out.replace(/,(\s*[}\]])/g, "$1"));
}

const config = parseJsonc(rawConfig) as WranglerConfig;

describe("wrangler.jsonc", () => {
  it("names the Worker and its entry point", () => {
    expect(config.name).toBe("kalkan-edge");
    expect(config.main).toBe("src/index.ts");
  });

  it("is reachable only through its Custom Domains", () => {
    expect(config.workers_dev).toBe(false);
    expect(config.preview_urls).toBe(false);
    expect(config.routes.length).toBeGreaterThan(0);
    for (const routeEntry of config.routes) {
      expect(routeEntry.custom_domain).toBe(true);
    }
  });

  it("routes exactly the hosts the Worker serves", () => {
    const patterns = config.routes.map((routeEntry) => routeEntry.pattern).sort();
    expect(patterns).toEqual([config.vars.APP_HOST, config.vars.EVIDENCE_HOST].sort());
    expect(config.vars.APP_HOST).toMatch(/^app\./);
    expect(config.vars.EVIDENCE_HOST).toMatch(/^evidence\./);
    const domain = config.vars.APP_HOST?.slice("app.".length);
    expect(config.vars.EVIDENCE_HOST?.slice("evidence.".length)).toBe(domain);
  });

  it("commits no origin URL and no secret", () => {
    expect(config.vars.API_ORIGIN).toBe("");
    expect(config.vars.WEB_ORIGIN).toBe("");
    expect(Object.keys(config.vars).sort()).toEqual([
      "API_ORIGIN",
      "APP_HOST",
      "EVIDENCE_HOST",
      "WEB_ORIGIN",
    ]);
  });

  it("keeps request URLs (evidence tokens) out of Cloudflare's logs", () => {
    expect(config.observability.logs.invocation_logs).toBe(false);
    expect(config.observability.traces.enabled).toBe(false);
  });
});
