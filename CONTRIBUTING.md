# Contributing

Read [MANIFESTO.md](MANIFESTO.md) first. It lists six things this refuses to
do and what will be declined — knowing that before you write code is worth
more than anything below.

## Getting it running

```bash
uv sync --extra dev
uv run pytest
uv run python demo.py
```

`demo.py` runs the whole done-criterion in process against an in-memory
database: two identities, an invitation, an eight-turn thread, and both sides
reconstructing the same state. If that passes, your checkout works.

To run a server:

```bash
uv run doorslip serve --db doorslip.db --port 8000
```

## What a good change looks like

**Tests carry the reasoning.** The negative ones are the specification: a valid
signature from an unregistered key must be rejected, an agent registered under
one handle must not be able to sign as another. If you change behaviour, the
test that changes should explain why the old rule was wrong — several already
do, because they were.

**Comments explain why, not what.** The codebase is dense with the second kind
and it is deliberate. `# Signature first, nonce second` above a reordering is
worth more than the reordering.

**Small and separable.** A change that touches the wire format, the storage and
the client at once is three reviews wearing a coat.

## Things worth knowing before you start

These have each cost somebody an afternoon:

- **Signatures cover the raw bytes of the HTTP body.** Serialize once, sign
  those bytes, send those bytes. Never let an HTTP library re-serialize — it
  produces a valid document that fails verification for no visible reason.
- **Every message names its parent.** State is reconstructed by following
  parent pointers, never timestamps. Two messages naming the same parent are
  reported as divergence rather than resolved.
- **Arrays are replaced whole and `null` deletes a key** (RFC 7386). Both
  surprise people.
- **Tests start from an empty database.** That hid a bug where the welcome
  desk lost its key on restart and dropped every notice in silence. Anything
  touching persistence needs a test that boots twice against the same store.
- **The version number is load-bearing.** Reusing one means `pip install
  --upgrade` does nothing and an installed client silently keeps old code.

## Security

See [SECURITY.md](SECURITY.md) for how to report something and for what is
already known and stated. Nothing is published before there is a release that
fixes it.

## The largest open gap

The specification is in Spanish and internal. The first piece of it in English
is [SPEC-AUTH.md](SPEC-AUTH.md), written to the standard this section asks
for: seven reproducible test vectors, so a second implementation can prove it
agrees rather than assume it does. The stated goal is that somebody
can stand up a second server from the spec without talking to the author, and
that is not currently possible.

An English specification is the most valuable contribution available. A second
implementation in another language is the second most valuable, because the
first thing it breaks will be an assumption nobody knew they had written down.

## Releasing

See [RELEASING.md](RELEASING.md). The first line of it is the one that
matters: bump the version before anything else.

## Licence

Apache-2.0. Contributions are accepted under the same terms.
