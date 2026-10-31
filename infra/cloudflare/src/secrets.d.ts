// Secrets live outside wrangler.jsonc, so `wrangler types` cannot list them; declared here instead.
// Optional on purpose: an unset secret is undefined at runtime and the Worker must fail closed.
declare namespace Cloudflare {
  interface Env {
    EDGE_SHARED_SECRET?: string;
  }
}
interface Env {
  EDGE_SHARED_SECRET?: string;
}
