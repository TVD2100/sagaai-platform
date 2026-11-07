# Trusted signing keys

Public keys SagaAI uses to check the signatures of update manifests
(`file_versions.json`) and marketplace catalog entries (`index.json`,
`entity.json`). The folder ships with the installation: the updater and the
marketplace read it by default and never from the channel they verify - a key
delivered next to the manifest it vouches for proves nothing.

## Installed anchor

`ed25519-public.pem` in this folder is the release key installations trust.
Its private half lives on the publishing machine at `~/.sagaai/ed25519.pem`
(mode `0600`), never in the repository, and must be backed up: losing it means
new releases can no longer be signed, and the published public key has to be
replaced through a release signed by the old one. `python3
scripts/sign_release.py keyring keys/` prints the `key_id` in force.

## Generating the release key

Run once, on the machine that publishes releases:

    python3 scripts/sign_release.py keygen --private ~/.sagaai/ed25519.pem \
        --public keys/ed25519-public.pem

The command prints the `key_id`. The private half stays outside the
repository; it is also accepted from `SAGAII_SIGNING_KEY` as PEM text or a
path. Commit only the public file.

## Signing and checking a release

    python3 scripts/sign_release.py sign file_versions.json
    python3 scripts/sign_release.py verify file_versions.json
    python3 scripts/sign_release.py keyring keys/

`sign` writes `<file>.sig` next to the manifest. The updater downloads that
sidecar with the manifest and refuses the update when the stable channel
cannot verify it.

## Rotation

1. Generate the new pair and commit its public half next to the old one - any
   `*.pem` or `*.pub` file in this folder is loaded, so both keys are trusted
   during the overlap.
2. Publish releases signed with the new key; clients still on the old version
   accept them.
3. When no supported client relies on the old key, delete its public file.

The `key_id` is derived from the public key, so a signature always names the
key that made it and a rotated key never collides with the old one.

## Policy

The `stable` channel and an installation with
`marketplace.require_signature = true` refuse an unsigned manifest or one
signed by an unknown key. Development channels (`main`, `raw`, `dev`) accept
it and report the state, so local work needs no key.
`marketplace.signature_keyring` points the marketplace at another folder (a
private mirror); `SAGAII_SIGNATURE_KEYRING` does the same for the updater.
