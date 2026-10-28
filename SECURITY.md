# Security policy

Unified Paper Workbench is an early open-source preview. Only explicitly maintained versions receive fixes; a preview version number is not a security certification.

## Reporting a vulnerability

Do not publish credentials, manuscript data, browser evidence, or exploitation details in a public issue. Use GitHub private vulnerability reporting on this repository when enabled. If private reporting is unavailable, open a minimal issue asking for a private contact channel without posting sensitive details.

## Data and execution boundaries

- Manuscripts and runtime records are stored in user-selected local workspaces.
- AI task inputs are sent to the model services the user connects; this is not offline inference.
- Passwords and SDK login challenges must not appear in papers, public screenshots or support logs.
- Private update/signing keys and production account infrastructure must remain outside the repository.
- The optional local commercial-control-plane code is development infrastructure, not a production service ready to expose publicly.

For privacy details, see [PRIVACY.md](PRIVACY.md). For first-release limitations, see [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md).
