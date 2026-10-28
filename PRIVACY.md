# Privacy and data boundaries — open-source preview

This notice describes the data boundaries implemented by Unified Paper
Workbench open-source preview. It is an engineering disclosure, not a
substitute for the final privacy policy required before public sale.

## Local-first data

By default, manuscripts, experimental data, figures, author files, submission
packages, browser evidence, account metadata, and workflow state remain in the
paper workspace and application state directories chosen by the user.

The product does not upload those files to the commercial licensing service.
Model providers, journal systems, mailbox providers, and browsers are separate
services selected or connected by the user. Data sent to them is governed by
their terms and privacy policies.

## Commercial account service

The internal compatibility build contacts no commercial service by default.
When commercial account mode is enabled there, or when the customer edition
is used, the control plane may receive:

- the login e-mail address and one-time-code verification events;
- random user, session, installation, and device identifiers;
- application version and operating-system family;
- subscription plan, entitlement, activation, refresh, and revocation events;
- release channel and signed-update requests.

The desktop uses a random device identifier. It does not derive a hardware
fingerprint from serial numbers, MAC addresses, disks, TPMs, or similar
hardware attributes.

## Diagnostics

Diagnostic upload is off by default and requires an explicit opt-in. The
implemented diagnostic contract allows only version, error code, component,
operation category, duration, OS family, release channel, licence state,
anonymous installation ID, and a small allow-listed metadata set.

It does not allow manuscript text, titles, author names, e-mail addresses,
local paths, prompts, passwords, API keys, bearer tokens, cookies, browser
HTML, screenshots, or raw exception messages. Events failing the allow-list
validation are not sent.

See [docs/PRIVACY_DIAGNOSTICS.md](docs/PRIVACY_DIAGNOSTICS.md) for the exact
contract.

## Credentials

Model keys, journal credentials, and mailbox passwords are stored locally
using the product credential vault. Commercial bearer sessions are protected
for the current Windows user through DPAPI. Secrets are never included in
signed licences, release manifests, or diagnostic events.

## Retention and deletion

The local application allows users to remove workspaces and account records.
The production operator must define control-plane retention, deletion,
support-contact, lawful-basis, international-transfer, and data-subject-request
procedures before public availability. Those operational values are not yet
claimed by this preview.

## Contact

Before release, replace this section with the legal operator name, privacy
contact, jurisdiction, and effective date.
