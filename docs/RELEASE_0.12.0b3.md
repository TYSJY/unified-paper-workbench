# 0.12.0b3 external closed-beta candidate

This candidate adds the operated product shell around the local-first desktop
while preserving the `0.11.6`, `0.12.0b1`, and `0.12.0b2` rollback baselines,
existing paper workspaces, and every tuned internal prompt in writing,
figures, journal/author, and smart submission.

## Delivered

- production-shaped Sites Worker with D1 state and R2 release artifacts;
- closed-beta application, invite, allow-list, account, session, trial,
  subscription, device, signed licence, policy, diagnostics, and audit routes;
- bilingual public website, invite activation, member centre, administrator
  console, and dynamic signed download page;
- Resend delivery adapter for login codes and beta invitations, with a bounded
  operator fallback for closed-beta incidents;
- immutable customer desktop profile fixed to the production HTTPS origin and
  one reviewed Ed25519 public key;
- per-user Windows installer with replacement rollback, optional shortcuts,
  uninstall registration, silent test switches, and local-workspace
  preservation;
- deterministic multipart release publisher in
  `tools/publish_customer_release.ps1`.

## Website provenance

- Product-site source commit:
  `16d32c710d57322ef16cd840d22fca98745b136f`
- Independent Cloudflare deployment tooling commit:
  `0e3862727364af535c4067083baa0ea491d3a662`
- Independent deployment source archive SHA-256:
  `6b3f5647bb043c5593b773676c0a0e7a50a68a794ae936737226abfb3d14bb74`
- Saved Sites version: `2`
- Saved version ID:
  `appgprj_6a68840667a48191af30000117c32a4c~appgver_2ce765aa786c8191b789fceac32a0ada`
- Successful deployment ID:
  `appgdep_6a6a1d4dfdbc81918b808e691eda4134`
- Production URL:
  `https://unified-paper-workbench.gptpro20x836074.chatgpt.site`
- Site unit/render tests: `11/11` passed
- Site lint: passed
- Production dependency audit: zero vulnerabilities
- Desktop/mobile Chinese/English visual and interaction QA: passed
- Direct Cloudflare production-config tests: `5/5` passed
- Combined product-site tests after adding the deployment path: `16/16`
  passed
- Wrangler production upload dry run with D1, R2, assets, and environment
  bindings: passed

The deployment is currently owner-only because the Sites workspace rejects
public access with `sites_publish_disabled`. The source and deployment are
valid, but this is not yet an externally reachable customer website.

## Desktop and installer evidence

- Verification date: `2026-07-30` (Asia/Taipei)
- Source branch: `codex/commercial-v0.12`
- Pre-change rollback commit:
  `bb59635bbae09e3d38fc3ee0f1e941054af50ce7`
- Full repository regression:
  `1335 passed, 54 subtests passed`
- Former loopback/proxy failures retest: `8 passed`
- Packaged version: `0.12.0b3`
- Packaged distribution: `customer`
- Commercial enforcement: `true`
- Runtime control-plane configuration: disabled
- Development keys: disabled
- Embedded trusted public keys: `1`
- Frozen control-plane origin:
  `https://unified-paper-workbench.gptpro20x836074.chatgpt.site/api/control-plane`

Customer EXE:

- size: `211938953` bytes
- SHA-256:
  `c8325dc6343f4eac25931a780004517c47879c9ba33d931eddfbe1f6d00506b8`
- Authenticode: `NotSigned`

Customer installer:

- file: `UnifiedPaperWorkbench-Setup-0.12.0b3.exe`
- size: `210223871` bytes
- SHA-256:
  `3713dfbc9599fce932fe85a6979c5e9dffc9e2b79fc38b04c53183cbad5b8e01`
- embedded payload verification: passed
- silent isolated install: passed
- silent in-place upgrade: passed
- installed EXE hash matched the frozen source EXE
- installed `--version` and `distribution-status`: passed
- installation-root marker and product-file boundary checks: passed
- copied uninstaller refused to delete a foreign directory and preserved its
  sentinel file
- installer refused to replace a foreign non-empty directory and preserved its
  sentinel file
- reparse-point and drive-root deletion boundaries are enforced
- isolated uninstall: exit `0`, install directory removed

Local production-adapter release exercise:

- real installer multipart upload and completion: passed
- signed latest-release manifest publication: passed
- signing key ID: `ed25519-96a68d69df554cbe571d`
- desktop-code Ed25519 manifest verification: passed
- full `210223871`-byte download SHA-256: matched
- `Range: bytes=0-1023`: HTTP `206`, correct content range
- bilingual browser download-page check with real metadata: passed

## Preserved rollback evidence

- `0.11.6` stable EXE SHA-256:
  `0adc5877cae12495f194245df499ff5abf6883ac570dffc919c9042a376a8885`
- `0.12.0b2` customer EXE SHA-256:
  `ea5e756b890bec3e86633af5cb509930ce8ea04f1fc2e6c7c742b54749fe441e`
- candidate work start tag: `commercial-work-start-0.12.0b3`
- previous candidate tag: `commercial-candidate-0.12.0b2`
- stable source tag: `commercial-baseline-0.11.6`
- tuned prompt files changed by this candidate: `0`
- existing `dist/UnifiedPaperOrchestrator.exe`: unchanged

## External go-live blockers

This candidate must not be represented as a ready public paid release until:

1. either internet publishing is enabled for the Sites workspace or the
   independently prepared Cloudflare Worker is deployed from an authenticated
   account, and anonymous access is verified;
2. a verified Resend sender plus `RESEND_API_KEY` and `EMAIL_FROM` are
   configured, deployed, and tested with a real recipient;
3. the real installer is uploaded to the production R2 control plane and
   downloaded again from the public origin;
4. the customer EXE and installer receive a trusted Windows Authenticode
   signature and timestamp;
5. production signing-key custody is moved to managed KMS/HSM or an equivalent
   controlled signer with rotation and recovery;
6. final legal identity, EULA, privacy, refund, automated-submission, Qt/PySide
   licensing, monitoring, backup, abuse, incident, malware, and billing gates
   are approved.

Manual-code fallback and an owner-only deployment do not satisfy real e-mail
or external-access verification.

## Operating and rollback instructions

See
[Commercial closed-beta operations](COMMERCIAL_CLOSED_BETA_OPERATIONS.md) and
[Commercial release checklist](COMMERCIAL_RELEASE_CHECKLIST.md).
