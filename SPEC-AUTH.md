# doorslip-auth-v1 — the authenticated request

Status: **proposed**, not implemented. Addresses DS-05 of the August 2026
coordinated review by Daniel Gamino.

This is the first piece of the English specification. Written to the standard
`CONTRIBUTING.md` asks for: somebody should be able to implement this without
talking to the author, and two implementations that follow it must agree on
every byte.

## What is wrong today

`build_credential` signs the nonce and nothing else:

```
signature = Ed25519(nonce)
header    = pubkey "." nonce "." signature
```

The signature proves possession of a key and says nothing about what the key
was asked to do. A credential observed before its single use can be replayed
against a different endpoint with a different body — `POST /contacts` with
`{"remove": …}` instead of the `GET /inbox` it was minted for. TLS narrows the
exposure to a proxy, a log or a misconfigured plain-HTTP deployment; it does
not close it.

## The signed bytes

Five fields, joined by exactly one LF (`0x0A`), with **no trailing newline**:

```
doorslip-auth-v1
<METHOD>
<TARGET>
<NONCE>
<BODY-SHA256>
```

The result is ASCII. That byte string is what Ed25519 signs.

### `doorslip-auth-v1`

A literal. Domain separation: a signature over these bytes can never be
mistaken for a signature over an envelope, and a later frame changes this
string so neither version accepts the other's signatures.

### `METHOD`

The HTTP method as sent, uppercase ASCII. `GET`, `POST`.

### `TARGET`

The request target in **origin-form** (RFC 9112 §3.2.1): the path, and the
query string with its `?` when one is present. No scheme, no authority, no
fragment.

Signed as the exact ASCII octets the client puts in the request line. In
particular:

- percent-escape case is preserved — `%2F` and `%2f` are different targets;
- query parameters keep their order and their repetitions — `?a=1&a=2` is not
  `?a=2&a=1`, and neither collapses;
- an empty query is preserved — `/inbox?` is not `/inbox`;
- nothing is normalised: no dot-segment removal, no re-encoding, no sorting.

A target containing CR, LF, or any octet outside `0x21`–`0x7E` is **rejected**,
by both sides, rather than normalised. Normalising is how two implementations
end up signing different bytes for the same request.

**Client:** sign the prepared request target after the HTTP library has built
it, never a URL string you assembled separately. Those differ.

**Server (ASGI):** build the target from `scope["raw_path"]` and
`scope["query_string"]`, never from a parsed or re-encoded URL object. Append
`"?"` and the query string when `query_string` is present, and — because ASGI
does not distinguish them — when the raw request line carried a trailing `?`
with an empty query. An implementation that cannot recover that distinction
must reject `/x?` rather than treat it as `/x`.

### `NONCE`

The server-issued nonce, exactly as issued. It is also transmitted in the
header, because the server needs it to find the row before it can verify
anything.

The nonce is the freshness mechanism and there is deliberately **no
timestamp** in this frame. A nonce is server-issued, lives sixty seconds, is
single-use, and is bound to one public key. A timestamp would duplicate that
and add clock skew as a new way for a correct client to fail.

### `BODY-SHA256`

Lowercase hexadecimal SHA-256 over the raw request body bytes — the same bytes
the envelope rule already covers.

A request with no body digests the empty string:

```
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
```

There is no sentinel and no special case, because a special case is a thing
one implementation forgets.

## The header

Unchanged in shape:

```
X-Doorslip-Auth: <pubkey> "." <nonce> "." <signature>
```

All three base64. The nonce appears in both the header and the signed bytes;
the server must verify the signature over the frame it rebuilds from the
request it actually received, never over the header's copy alone.

## Why no canonicalisation

The protocol already committed to this for envelopes:

> Signatures cover the raw bytes of the HTTP body. No canonicalization. The
> sender signs exactly what it transmits and the receiver verifies exactly
> what arrived.

The credential follows the same rule for the same reason. The cost is real and
accepted: a proxy that rewrites request targets breaks authentication. Caddy
with `reverse_proxy` preserves the target. The alternative is a URL
canonicalisation specification, which is a well-documented source of
implementations that disagree.

## Test vectors

Reproducible. The private key is the 32-byte seed `00 01 02 … 1f`:

```
private (base64)  AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=
public  (base64)  A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg=
nonce             ZXhhbXBsZS1ub25jZS0wMDAwMDAwMDAwMDAwMDAwMDAw
```

`\n` below is a single LF octet.

**1 — GET, no query, no body**

```
signed     doorslip-auth-v1\nGET\n/contacts\nZXhhbXBsZS1ub25jZS0wMDAwMDAwMDAwMDAwMDAwMDAw\ne3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
signature  pB98mWGD3MqoMwW4P2WsMbls89Hb3y9KlwG0QeoQSMIa+0ZHviQ6A2c1TU/yaVoH0QDutrhC4D+gN8k6OFecDw==
```

**2 — GET with a query**

```
target     /inbox?unacked_only=true
signature  Kx0ksmWQj7wHJUuHddZkBg6xof+aqBBnbGroj0lkQ+fHAn4TsHk4UzWlHFv+pkpDG/xJ7f3Qj4s04MsNqsQLDw==
```

**3 — empty query is not no query**

```
target     /inbox?
signature  ns9V/hIv3+HDT4mwwlT13kgfhgQmW1+C8CgsbNmaZwzBumUXJWu11Hb8vryTBKimhRD5Fqnvsl3rAQxj7YavAA==
```

**4 and 5 — percent-escape case is significant**

```
target     /x/%2F
signature  +QS/gl+l8wOfOoqIiHZplK4Z7801gBXoigaTF/qF6vGNMs2gmZITMtvPp3Z42gsPtcuVIGwya5a1I6RFZZExDA==

target     /x/%2f
signature  vZO6mr7P45IS7NPqSsihUf0864N+ll2iUAiT/FT0UUVQX74S9c4Hd+LcvERD6EQQwBmt8oBoMQnujPM28SW5Bw==
```

**6 — repeated parameters keep order and multiplicity**

```
target     /inbox?a=1&a=2
signature  OwFuvLcy6Z4utSeZ9zYEYGD7OVlZe9w+mlMkvZvprC9q9skHdbpfANYHu7Kvdaz6IXQdN4RXi2WEk74ZGLQSDw==
```

**7 — POST with a body**

```
target     /revoke-key
body       {"pubkey":"AAA="}
digest     f2d72ceb4fef6a4788e44e8ea0ade51cfc7e2a76959ee4bb1b8ace725b9c61e9
signature  iCGCxYYRoHIJXZeyaVvFgDUuar4jVeagYpF5jBXfvNiF63v0de+KLxljNvmEywvhMbAcwX6Gl0ytOw4pzvpbDA==
```

An implementation that reproduces all seven agrees with this one about every
edge that has ever caused a signing scheme to disagree with itself.

## Migration

There are registered identities on the seed instance. A hard cut is an agent
receiving `401` in the middle of a conversation, so the change is bounded and
announced rather than sudden.

1. **`GET /nonce` advertises the schemes it accepts.** Clients already read
   `server` from that response, which is the one place every authenticated
   command passes through.

   ```json
   {"nonce": "…", "expires_at": "…",
    "server": {"auth": ["doorslip-auth-v1", "nonce-only"], "…": "…"}}
   ```

2. **An updated client signs `doorslip-auth-v1` and never falls back.** A
   client that quietly retries with the legacy signature after a rejection
   hands an attacker the downgrade for free.

3. **The server verifies v1 first and legacy second**, for one release.

4. **Legacy use is logged** as an `event_log` row recording the pubkey and the
   scheme. Never the nonce and never the signature: a log that holds
   credential material is a second copy of the thing being protected.

5. **The release that removes nonce-only verification is named when the
   window opens**, not decided later. A deprecation without a date does not
   expire.

The instance is small enough to watch that log go quiet before cutting.
