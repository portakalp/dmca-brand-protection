# kalkan-edge (optional)

A Cloudflare Worker that sits in front of `app.<domain>` and `evidence.<domain>` in the **optional Google Cloud reference deployment** (Cloud Run + one VM + Cloudflare edge, see [`infra/terraform`](../terraform/README.md); architecture.md §22; K80, K84). It forwards requests to the Cloud Run `api` and `web` services and adds the shared `X-Edge-Auth` header, so the run.app URLs refuse anything that did not come through the edge.

You do not need it to run Kalkan. A self-hosted server ([`deploy/selfhost`](../../deploy/selfhost/)) uses Caddy for the same job: it routes the same paths and injects the same header.

## Routing

| Request | Goes to |
| --- | --- |
| `app.<domain>/api` and `app.<domain>/api/*` | `api`, path unchanged |
| `evidence.<domain>/*` | `api`, path unchanged |
| `app.<domain>/*` (everything else) | `web`, path unchanged |
| `/healthz` and `/api/healthz` on either host | `api`, path becomes `/api/health` (Cloud Run answers those paths itself, K80) |
| Any other host | 404, nothing is forwarded |
| `http://` | 308 to the same URL with `https://` |

- `X-Edge-Auth`, `X-Forwarded-For`, `X-Forwarded-Host`, `X-Forwarded-Proto` and `Forwarded` sent by the client are dropped. The Worker sets its own values: the secret, `CF-Connecting-IP`, the original host and `https`.
- Method, query string and body are forwarded byte for byte. Redirects are not followed (`redirect: "manual"`) and Cloudflare's cache is not used (`cache: "no-store"`). The response is returned as is, with one exception: the `Location` header.
- A `Location` that points at the `API_ORIGIN` or `WEB_ORIGIN` host (`http`, `https` or `//host`) is moved to the host the request came in on, with `https://`; path and query stay. FastAPI's `redirect_slashes` builds an absolute URL with the run.app address, and behind Cloud Run uvicorn sees the scheme as `http`; a browser sent to run.app would get 403 because the header is missing. Relative `Location` values and other hosts are left alone.
- Without the secret, or with an empty or non-`https` `API_ORIGIN`, the Worker answers 503 (`edge is not configured`) and forwards nothing. With an empty `WEB_ORIGIN` the web paths answer 503 (`web is not deployed yet`). An unreachable origin gives 502.

## Configuration

`wrangler.jsonc`:

- `routes`: `app.example.com` and `evidence.example.com` as Custom Domains. Replace `example.com` with your domain. `workers_dev` and `preview_urls` are off, so no other host reaches the Worker.
- `vars.APP_HOST`, `vars.EVIDENCE_HOST`: must equal the Custom Domains; `test/config.test.ts` checks this. When the domain changes, all four lines change together.
- `vars.API_ORIGIN`, `vars.WEB_ORIGIN`: empty in the repository. Pass them on every deploy, e.g. `--var API_ORIGIN:<terraform output api_url>`. Without them the Worker answers 503.
- Secret `EDGE_SHARED_SECRET`: set with `wrangler secret put`, never in a file. It must equal the `edge-shared-secret` value in Secret Manager.
- Invocation logs and traces are off, because URLs carry evidence tokens (`/e/<token>`).

Deploying (you run it, with your own Cloudflare account):

```sh
cd infra/cloudflare
npm ci
npx wrangler secret put EDGE_SHARED_SECRET
npx wrangler deploy \
  --var "API_ORIGIN:$(terraform -chdir=../terraform output -raw api_url)" \
  --var "WEB_ORIGIN:$(terraform -chdir=../terraform output -raw web_url)"
```

## Local commands

Needs Node 22 or newer (CI uses Node 24).

```sh
cd infra/cloudflare
npm ci
npm run typecheck   # wrangler types (generates worker-configuration.d.ts, git-ignored) and tsc
npm test            # vitest; the tests run inside workerd
```

`make edge-test` at the repository root runs the same steps, with `npm ci` first when `package-lock.json` changed.

- The tests run in workerd through `@cloudflare/vitest-plugin`. They never reach the network: `fetch` is mocked in every test, and a request that escapes fails because of the `outboundService` in `vitest.config.ts`.
- Host, origin and secret values for the tests come from `vitest.config.ts`.
- `test/route.test.ts` covers the pure routing table, `test/proxy.test.ts` the Worker's entry point (`exports.default.fetch`) and `test/config.test.ts` the `wrangler.jsonc` file.

## Versions

`wrangler`, `@cloudflare/vitest-plugin`, `vitest` and `typescript` are pinned to exact versions. Each plugin release depends on exactly one wrangler release (1.3.7 → 4.148.0), so the two are upgraded together. `compatibility_date` (`2026-10-01`) cannot be later than the workerd date inside the pinned wrangler.

The `overrides` entry in `package.json` pins `sharp` to 0.35.5: miniflare asks for 0.35.4, which has a librsvg vulnerability (development and test tooling only; it never ends up in the Worker bundle). Remove the override once wrangler and the plugin move to a release that brings the fixed sharp.
