import { cloudflareTest } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.jsonc" },
      miniflare: {
        // Test values override the vars in wrangler.jsonc and stand in for the secret.
        bindings: {
          APP_HOST: "app.example.test",
          EVIDENCE_HOST: "evidence.example.test",
          API_ORIGIN: "https://api-test.a.run.app",
          WEB_ORIGIN: "https://web-test.a.run.app",
          EDGE_SHARED_SECRET: "test-edge-secret",
        },
        // Backstop for test/setup.ts: an outbound request that escapes the fetch spy fails
        // instead of reaching the network.
        outboundService: () => {
          throw new Error("tests must not reach the network");
        },
      },
    }),
  ],
  test: {
    include: ["test/**/*.test.ts"],
    setupFiles: ["test/setup.ts"],
  },
});
