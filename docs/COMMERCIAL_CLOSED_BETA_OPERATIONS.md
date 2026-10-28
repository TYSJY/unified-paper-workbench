# Commercial closed-beta operations

This runbook covers the operated `0.12.0b3` customer candidate. It does not
turn an unsigned candidate into a public paid release.

## Production resources

- Sites project ID:
  `appgprj_6a68840667a48191af30000117c32a4c`
- Product origin:
  `https://unified-paper-workbench.gptpro20x836074.chatgpt.site`
- Control-plane base:
  `https://unified-paper-workbench.gptpro20x836074.chatgpt.site/api/control-plane`
- D1 binding: `DB`
- R2 binding: `DOWNLOADS`
- Customer channel: `preview`

`.openai/hosting.json` in `product_site/` is the only source of truth for the
opaque Sites project ID. Never derive or substitute that ID from the site
title or URL.

The Sites deployment is a rollback target, not a mandatory hosting dependency.
If that workspace cannot publish publicly, product-site commit
`0e3862727364af535c4067083baa0ea491d3a662` also contains an independent
Cloudflare Workers deployment path. It uses the same Worker, D1 and R2
contracts without changing the product pages or account API.

## Required environment

Store all runtime values in Sites, not in source, `.openai/hosting.json`, an
EXE, an installer, or a command transcript.

| Variable | Secret | Purpose |
|---|---:|---|
| `PUBLIC_SITE_URL` | no | Canonical website and API origin |
| `ALLOWED_ORIGINS` | no | Exact browser CORS allow-list |
| `BETA_ACCESS_MODE` | no | `allowlist` for closed beta |
| `ADMIN_API_KEY` | yes | Operator API authentication |
| `AUTH_PEPPER` | yes | Login-code and invite hashing |
| `SIGNING_PRIVATE_KEY_PEM` | yes | Candidate licence/release signing |
| `SIGNING_KEY_ID` | no | Must match the embedded desktop public key |
| `RESEND_API_KEY` | yes | Real transactional e-mail delivery |
| `EMAIL_FROM` | no | Sender on a verified Resend domain |
| `EMAIL_REPLY_TO` | no | Optional support reply address |
| `MANUAL_LOGIN_CODES` | no | Bounded operator fallback (`1` or `0`) |
| `DEV_EXPOSE_LOGIN_CODES` | no | Must be `0` in production |
| duration variables | no | Trial, session, lease, refresh, and grace windows |

After any environment change, deploy a saved Sites version so the new
environment revision becomes active.

## Enable external closed-beta access

The application allow-list cannot run while Sites itself is owner-only.
Before inviting customers:

1. enable internet publishing for the Sites workspace;
2. set the site access mode to `public`;
3. keep `BETA_ACCESS_MODE=allowlist`;
4. verify that an unsigned browser session can load `/`, `/beta`, `/invite`,
   `/account`, `/download`, and `/api/control-plane/health`;
5. verify that an unapproved e-mail cannot request a login code.

Do not use a Sites owner bypass token in the desktop or distribute it to
customers.

### Independent Cloudflare path

Authenticate once with `wrangler login`, create an ignored
`product_site/secrets.cloudflare.production.json`, and run:

```powershell
cd .\product_site
npm.cmd run cloudflare:dry-run
npm.cmd run cloudflare:deploy -- `
  --secrets-file secrets.cloudflare.production.json
```

The secrets JSON requires `ADMIN_API_KEY`, `AUTH_PEPPER`,
`SIGNING_PRIVATE_KEY_PEM`, `SIGNING_KEY_ID`, `RESEND_API_KEY`, and
`EMAIL_FROM`. The deployment script:

1. runs all site tests and lint;
2. converts vinext output into a production Wrangler configuration;
3. provisions or reuses exactly one `DB` D1 binding and one `DOWNLOADS` R2
   binding;
4. uploads secrets without printing them;
5. freezes the discovered HTTPS origin and redeploys once;
6. requires the public health endpoint to report database, storage, signing,
   and real e-mail delivery as ready.

Cloudflare account and resource identifiers are retained only under ignored
`product_site/work/cloudflare/`. For a custom domain, pass
`--public-origin https://paper.example.com --custom-domain`.

After selecting the final public origin, rebuild the customer EXE with that
exact control-plane URL. A customer EXE frozen to the Sites URL must not be
distributed with a different Cloudflare origin.

## Configure real e-mail

1. Verify a sender domain in Resend.
2. Create a production API key limited to transactional mail where the
   provider supports that scope.
3. set `RESEND_API_KEY` as a secret;
4. set `EMAIL_FROM` to the verified sender, for example
   `Unified Paper <account@example.com>`;
5. optionally set `EMAIL_REPLY_TO`;
6. deploy the saved site version;
7. apply or invite a synthetic beta address, request a code, and confirm that
   the received code completes login;
8. confirm that codes expire, are single-use, rate-limited, and redacted from
   logs.

`MANUAL_LOGIN_CODES=1` is an operator fallback, not proof that real mail is
working. For a public paid release, decide whether that fallback remains
enabled and record who may reveal a code.

## Publish the customer installer

Build and verify the customer EXE first. Then build the installer and publish
it without placing the administrator key on the command line:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\tools\publish_customer_release.ps1 `
  -ControlPlaneUrl `
    "https://unified-paper-workbench.gptpro20x836074.chatgpt.site/api/control-plane" `
  -AdminApiKeyPath "D:\private\unified-paper-admin-key.txt" `
  -InstallerPath "D:\release\UnifiedPaperWorkbench-Setup-0.12.0b3.exe" `
  -Version "0.12.0b3" `
  -Channel "preview" `
  -MinimumSupportedVersion "0.11.6" `
  -NotesUrl `
    "https://unified-paper-workbench.gptpro20x836074.chatgpt.site/download"
```

The tool refuses non-HTTPS origins except explicit loopback testing, aborts an
incomplete multipart upload when possible, and returns only non-secret
release evidence.

After publication:

1. compare the public manifest size and SHA-256 with the local installer;
2. download the complete artifact and verify SHA-256;
3. verify a `Range: bytes=0-1023` request returns HTTP `206`;
4. verify the signed envelope with the customer public key;
5. install into an empty Windows user profile;
6. complete real e-mail login, trial start, device activation, restart, and
   offline-lease checks.

## Operator console

`/admin` supports application review, invitation creation, manual-code
fallback, user/subscription inspection, subscription grants, release history,
and audit review. The administrator key is kept in `sessionStorage` for the
browser session and must not be pasted into issue reports or screenshots.

## Rollback

- Desktop source start point: tag `commercial-work-start-0.12.0b3`
- Previous customer candidate: tag `commercial-candidate-0.12.0b2`
- Stable compatibility baseline: tag `commercial-baseline-0.11.6`
- Website rollback source: Sites saved version `1`, source commit
  `7dd4772cb325ebe3b0f99108faa6e764d83ddd93`
- Sites version `2` source baseline: tag `commercial-site-sites-v2`, commit
  `16d32c710d57322ef16cd840d22fca98745b136f`
- Independent Cloudflare deployment tooling: product-site commit
  `0e3862727364af535c4067083baa0ea491d3a662`

Rolling back the website does not revoke an already issued desktop licence or
delete a local paper workspace. Unpublish a bad release manifest before
publishing a corrected artifact. Never delete customer paper directories as
part of application rollback.
