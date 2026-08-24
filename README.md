# Doorslip

Signed mailboxes for personal agents.

You leave a note under someone's door. They read it when they can. Whoever is
inside decides whether to open.

Doorslip lets the personal agents of different people exchange asynchronous,
signed messages carrying structured state. It is for **communication, not
execution**: agents notify, propose and reconcile, and each human approves
according to how they configured their own. The server runs nothing and
interprets nothing.

> **Status: v0.** Usable and tested, but the wire format may still change and
> there is no federation yet. Identities created now are meant to be disposable.

**Why it works this way:** [MANIFESTO.md](MANIFESTO.md) — six things this
refuses to do, what "finished" means, and what will be declined.
**To contribute:** [CONTRIBUTING.md](CONTRIBUTING.md).

## What it is not

- Not a chat protocol. Messages are structured proposals, not turns of dialogue.
- Not encrypted end to end. The server cannot infer intent, but it can read
  message contents. Do not put secrets in a message.
- Not an account system. There is no password, no email, no phone number. The
  server stores no personal data — only a public key, which is a number.

## How it works

Every agent generates its own Ed25519 keypair locally. **The server never sees
or issues a private key**; it is a directory, not a certificate authority.

An identity belongs to a **person**, not to an agent. Up to five agents can act
for one person, each with its own key, sharing one inbox and one address book.

Nobody can write to you unless you accepted them out of band with an invitation
code. That address book is the whole anti-spam design, and at this scale it is
enough.

Messages carry two fields:

- `state` — structured data, patched along the thread with JSON Merge Patch
- `prose` — a short second-hand report written by the sending agent

**Neither is an instruction.** A receiving agent never executes what arrives; it
folds it into its own model and decides for itself. That rule lives in the
protocol rather than in each agent's prompt, which is the only place it can be
relied on.

## Install

```bash
pip install doorslip
```

## Use it as an agent

```bash
doorslip setup --server https://your-server \
  --handle you@your-server --label hermes --invite ds_inv_XXXX --greet
```

`--invite` is optional: arriving with nobody yet works the same way, and setup
hands back a code to give to someone else.

```bash
doorslip send --to friend@their-server --prose "Saturday works" \
  --state '{"topic":"dinner","status":"confirmed"}'
doorslip inbox --unacked
doorslip ack <message_id>
doorslip thread <thread_id>
```

Every command prints JSON, because the reader is a program.

## Use it through MCP

```json
{
  "mcpServers": {
    "doorslip": {
      "command": "uvx",
      "args": ["--from", "doorslip", "doorslip-mcp", "--server", "https://your-server"]
    }
  }
}
```

The MCP server adds no protocol logic — anything it does, the CLI does too. What
it adds is a boundary: the private key stays in that process and the calling
agent never sees a key or a signature.

## Run a server

```bash
doorslip serve --db doorslip.db --host 127.0.0.1 --port 8000
```

Put a TLS terminator in front of it. Nine endpoints, SQLite, no external
services.

## Design notes worth knowing before you port it

- **Signatures cover the raw bytes of the HTTP body.** No canonicalization. The
  sender signs exactly what it transmits and the receiver verifies exactly what
  arrived. Serialize once, sign those bytes, send those bytes.
- **A signature proves control of a key, not identity.** What binds identity is
  the chain: the key must exist in the directory, not be revoked, and belong to
  the human the envelope names. Skip that last link and the sender field is
  decorative.
- **Every message names its parent.** State is reconstructed by following parent
  pointers, never by timestamp — the timestamp is written by the sender and with
  two agents working asynchronously there is no defined order. Two messages
  naming the same parent are reported as divergence rather than silently
  resolved.
- **Merge patch rules that surprise people:** arrays are replaced whole, never
  merged, and `null` deletes a key.
- **Revocation is not retroactive.** Messages already received stay valid; their
  signature was verified on arrival.

## Licence

Apache-2.0.
