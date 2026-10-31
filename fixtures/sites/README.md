# Fixture sites

Static sites for the scanning tests (architecture.md §25), served by the `fixture-sites`
nginx container of the local stack. All brands and domain names are fictional: Mintera Bank is
a made-up digital bank (navy header, teal accent), and the clone copies its look.

| Host | What it is | Expected HTTP pre-check |
| --- | --- | --- |
| `mintera.test` | The official Mintera Bank site (`official/`) | (official domain) |
| `mintera-login.test` | Clone (`clone/`): same look and favicon, a "Verify your card" banner, password and card fields, a form that posts to `/collect` | browser stage |
| `mintera-parked.test` | Parked, for-sale page | watching |
| `mintera-empty.test` | Almost empty page | watching |
| `mintera-redirect.test` | Redirects to the official site | watching, checked again the next day |
| `mintera-mobile.test` | Recipe blog on desktop, clone on mobile | watching (the browser stage sees the clone) |
| `mintera-challenge.test` | Bot challenge (HTTP 403, Cloudflare signatures) | browser stage |
| `mintera-verify.test` | Lure page (`lure/`): a fake "new sign-in address" security notice; its "Sign in" button leads to the clone and "Claim refund" to a third-party site (`quick-loans.example`) | browser stage; both button targets become candidates, the share link does not |
| `mintera-review.test` | Affiliate review linking to the official site | browser stage (the AI dismisses it later) |
| `mintera-loop.test` | Endless redirect loop (`/` → `/loop` → `/`); `/js` reloads itself from JavaScript forever | watching (the fetch gives up after 10 redirects); a browser capture ends in `error` on both profiles |
| `mintera-heavy.test` | About 30,000 px tall, 60 images (most lazy-loaded), 250 links and a video | browser stage; the capture aborts the video, caps the full-page screenshot at 10,000 px and the link list at its limit |

Inside compose the names resolve to the container (172.30.255.80), which the workers may reach
through `SSRF_ALLOW_CIDRS`. Tests on the host reach it at `127.0.0.1:18080`.

The browser stage reaches them only through `dev-proxy` (tinyproxy, `infra/compose/dev-proxy/`),
the local stand-in for the residential proxy: `http://dev-proxy:8888` inside compose,
`http://127.0.0.1:18888` from the host. It resolves the `*.test` names itself and refuses every
other host with 403.
