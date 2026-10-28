# Commercial release checklist

## Product and legal

- [ ] Replace placeholder owner/contact text in `LICENSE`, `PRIVACY.md`, and
      `SECURITY.md`.
- [ ] Finalise EULA, subscription terms, refund policy, privacy policy, and
      automated-submission disclosures with qualified counsel.
- [ ] Decide and document Qt/PySide licensing compliance.
- [ ] Include complete third-party licence texts in the installer.

## Control plane

- [ ] Production HTTPS service, database migration, backups, and recovery.
- [ ] Real e-mail delivery, abuse/rate limits, session revocation, billing
      webhook verification, and operator audit.
- [ ] KMS/HSM-backed Ed25519 signing and documented key rotation.
- [ ] Inject only the reviewed public key into the desktop keyring.
- [ ] Define availability, retention, deletion, support, and incident response.

## Desktop

- [ ] Version and release notes are consistent.
- [ ] Full test suite, Ruff, prompt-integrity seal, QML smoke, and publish audit
      pass from a clean checkout.
- [ ] Internal candidate is built with
      `tools/build_exe.ps1 -StageOnly -ReleaseProfile Internal`.
- [ ] Customer candidate is built with
      `tools/build_exe.ps1 -StageOnly -ReleaseProfile CustomerClosedBeta
      -CommercialPublicKeyPath <reviewed-public-key.pem>`.
- [ ] Packaged `distribution-status` proves the expected edition,
      enforcement flag, and customer public-key count.
- [ ] Existing production EXE and user workspace remain unchanged.
- [ ] Candidate EXE passes CLI and GUI smoke tests in an empty test profile.
- [ ] Windows Authenticode signature and timestamp verify.
- [ ] Signed release manifest exactly matches artifact size and SHA-256.
- [ ] Malware/dependency scans are archived.

## Go/no-go

- [ ] Trial, paid, expiry, offline-grace, device-limit, revocation, update, and
      diagnostics scenarios have evidence.
- [ ] No production private key, credential, personal fixture, state database,
      browser profile, or manuscript is present in the release bundle.
- [ ] Rollback commit, source backup, previous EXE hash, and restore procedure
      are verified.

Any unchecked security, key-custody, code-signing, privacy, or legal item is a
release blocker, not a post-launch task.
