# Commercial local development

The loopback control plane exists for contract and UI testing. It is not a
production service.

## Start the development service

From the repository root:

```powershell
python -m commercial_control_plane `
  --host 127.0.0.1 `
  --port 8765 `
  --state-dir .commercial/control-plane `
  --expose-development-codes
```

The service creates an Ed25519 private key, public key, and SQLite database
under the ignored `.commercial/` directory. Never copy that private key into
source control or an EXE.

## Enable the desktop development key

Source builds expose development-key settings only when explicitly launched
with:

```powershell
$env:UNIFIED_PAPER_COMMERCIAL_DEV_MODE = "1"
python -m unified_paper_orchestrator
```

In Account & Licence settings:

1. enable commercial account mode;
2. use `http://127.0.0.1:8765`;
3. select preview or stable;
4. enable development mode;
5. select `.commercial/control-plane/development-signing-key.public.pem`;
6. request the code, enter the development code returned by the local API,
   then activate.

A frozen EXE ignores the environment switch and rejects arbitrary
development keys.

## Build-profile verification

The default build remains the internal compatibility edition:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_exe.ps1 `
  -StageOnly `
  -ReleaseProfile Internal
```

A customer closed-beta build must use the public half of the same signing key
that the control plane uses for licence envelopes:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_exe.ps1 `
  -StageOnly `
  -ReleaseProfile CustomerClosedBeta `
  -CommercialPublicKeyPath .commercial/control-plane/development-signing-key.public.pem
```

The private key stays under ignored control-plane state. The packaged
`distribution-status` command verifies the frozen edition and public-key count
without creating a device, contacting the network, or opening a workspace.

## Safety checks

- bind only to loopback;
- use synthetic e-mail addresses;
- do not import production credentials or workspace state;
- delete `.commercial/` when the local test identity is no longer required;
- run `python tools/audit_publish_boundary.py` before sharing source or an
  artifact staging directory.
