# doorslip-auth-v1 — the authenticated request

Status: **implemented for 0.28.0**. Addresses DS-05 of the August 2026
coordinated review by Daniel Gamino. The bounded legacy verifier remains for
0.28.0 only and is removed in **0.29.0**.

This is the first piece of the English specification. Written to the standard
`CONTRIBUTING.md` asks for: somebody should be able to implement this without
talking to the author, and two implementations that follow it must agree on
every byte.

## What the change fixes

Before 0.28.0, `build_credential` signed the nonce and nothing else:

```
signature = Ed25519(nonce)
header    = pubkey "." nonce "." signature
```

That signature proves possession of a key and says nothing about what the key
was asked to do. A credential observed before its single use could be replayed
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

The request target in **origin-form** (RFC 9112 §3.2.1): path, plus the query
string introduced by `?` when the query is non-empty. No scheme, no authority,
no fragment.

Both sides **derive** it the same way, from the same two components:

```
TARGET = path-octets + ("?" + query-octets  if query-octets else "")
```

- **Server (ASGI):** `scope["raw_path"]` and `scope["query_string"]`. Never
  `scope["path"]`, which is percent-decoded: `/x/%2F` arrives there as `/x//`,
  and two different targets become one.
- **Client:** the same derivation over the components the HTTP library
  prepared. Not a URL string assembled separately — those differ.

What the derivation preserves, and every implementation must:

- percent-escape case — `%2F` and `%2f` are different targets;
- query order and repetition — `?a=1&a=2` is neither `?a=2&a=1` nor `?a=2`;
- nothing normalised: no dot-segment removal, no re-encoding, no sorting.

A target containing CR, LF, or any octet outside `0x21`–`0x7E` is **rejected**
by both sides rather than normalised. Normalising is how two implementations
sign different bytes for the same request.

A literal `#` is also rejected anywhere in `TARGET`: origin-form has no
fragment. Percent-encoded `%23` is not a fragment delimiter and remains valid.

#### A trailing `?` with an empty query is outside the profile

`GET /x?` and `GET /x` are indistinguishable at the ASGI boundary. Measured on
the Starlette/FastAPI stack this server runs on:

```
GET /x    raw_path=b'/x'  query_string=b''
GET /x?   raw_path=b'/x'  query_string=b''
```

The scopes are identical, so application code cannot know which arrived. An
earlier draft of this document required the server to reject `/x?` rather than
treat it as `/x`, which is a property the stated interface cannot carry —
found by Daniel Gamino before implementation, which is where a specification
bug is cheap.

So: **a conforming client never emits a trailing `?` with an empty query.** A
server treats one as absent, because it has no choice. The exact-request-line
property is given up for one semantically empty delimiter, and nothing is lost
by it — `/x?` and `/x` route to the same handler, so an attacker who could
change one into the other gains nothing.

The alternative was a server extension carrying the untouched request target,
with authentication refused when absent. That preserves the stronger property
and makes this scheme unimplementable by an ordinary ASGI server from this
document alone, which is the thing the document exists to avoid.

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

### Operational pre-authentication body limit

The reference server limits request bodies on endpoints protected by
`X-Doorslip-Auth` to 64 KiB (65,536 bytes). It reads the ASGI body stream
incrementally and returns HTTP 413 as soon as the accumulated bytes exceed the
limit, before signature verification, legacy fallback, handler execution, or
nonce consumption. `Content-Length` is not trusted to enforce this limit.

This is an operational resource bound, not a change to the cryptographic frame:
`BODY-SHA256` still covers every exact body byte of each accepted request. The
accepted bytes are cached unchanged so an endpoint that subsequently reads its
body receives the same bytes that were authenticated.

## The header

Unchanged in shape:

```
X-Doorslip-Auth: <pubkey> "." <nonce> "." <signature>
```

**The three components do not share an encoding**, and calling them all
"base64" is how a second implementation rejects a valid nonce or emits one
this server will not take. Exactly:

| component | bytes | encoding | length |
|---|---|---|---|
| `pubkey` | 32 | base64, standard alphabet (`+`, `/`), **padded** | 44 |
| `nonce` | 32 | base64url, URL alphabet (`-`, `_`), **unpadded** | 43 |
| `signature` | 64 | base64, standard alphabet (`+`, `/`), **padded** | 88 |

The nonce is unpadded base64url because it is minted with
`secrets.token_urlsafe(32)`; the keys and signatures are standard padded
base64 because that is what the Ed25519 material is encoded with. A verifier
should reject a component that does not match its row rather than accept both
alphabets, since accepting both means two encodings of one nonce and a
single-use value that can be spent twice.

Since `.` is not in either alphabet, splitting on it is unambiguous.

The nonce appears in both the header and the signed bytes. The server must
verify the signature over the frame it rebuilds from the request it actually
received, never over the header's copy of anything.

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

Reproducible. The private key is the 32-byte seed `00 01 02 … 1f`; the nonce
is the 32-byte sequence `20 21 … 3f` in unpadded base64url, so it conforms to
the encoding table above rather than merely looking like a nonce.

```
private (base64)   AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=
public  (base64)   A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg=
nonce   (b64url)   ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8
empty-body digest  e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
```

Each case gives the two ASGI components, the `TARGET` they derive, and the
signature over the frame.

**1 — GET, no query, no body**

The frame in full, once. Five lines separated by single LF octets, and **no
LF after the last one** — a trailing newline is a different byte string and a
different signature.

```
doorslip-auth-v1
GET
/contacts
ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
```

```
raw_path      /contacts
query_string  (empty)
TARGET        /contacts
signature     qks9hJNIFbOZvcWnlfC99EueI+/JUV5mT281ShDSh7VvjhJHQ5vA0DA20zLivU0YZfG4NiGUqImVmLpyZT1tBQ==
```

**2 — GET with a query**

```
raw_path      /inbox
query_string  unacked_only=true
TARGET        /inbox?unacked_only=true
signature     d2TkukoqWrOyPylwUoFbbwsP7c2dwkTq2lv/hiEuCd3smCtJfpWGYynUfihTJ4VPJyVmfcRmhRK4B6DCFrjzCQ==
```

**3 and 4 — percent-escape case is significant**

Different signatures for targets that differ only in the case of an escape.
Deriving from `scope["path"]` instead of `raw_path` collapses both to `/x//`
and produces one signature for two requests.

```
TARGET        /x/%2F
signature     TI5PgXU82IBApCoc9MAJKxnrYmSrBoLhbM8U7rILKHogLVgphfAGkWOkRUXCAD6iT76DuVVB9IFL6xU1tgVdAw==

TARGET        /x/%2f
signature     X9hdtQMOjPn0KFUJOgdPPj3zOE9MaSxlLs+99o3dWbsXhikcfXOlA9cov9dL0R5APOgwVwf9yvBK/ItIHRgyAQ==
```

**5 — repeated parameters keep order and multiplicity**

```
raw_path      /inbox
query_string  a=1&a=2
TARGET        /inbox?a=1&a=2
signature     QtFqFko6aQxRUe7C7loSQt5mwtTV5nkGO+ToC4hgXXxXfqlcdAVFbpGLZJFlNeZeyGRurFZuzCHP6eceZ1RCDw==
```

**6 — POST with a body**

```
raw_path      /revoke-key
query_string  (empty)
body          {"pubkey":"AAA="}
digest        f2d72ceb4fef6a4788e44e8ea0ade51cfc7e2a76959ee4bb1b8ace725b9c61e9
signature     qzEBBWLpOfciawQcDS/JRq8Hv/ZbSte+xxb7TgUTgdkh+Qq6Oq6NA6bJahKOuPhbW9nHwbohtyNDZiUdt53wDg==
```

An implementation that reproduces all six agrees with this one on every edge
that has caused a signing scheme to disagree with itself.

**Not a vector: the proxy hop.** Whether Caddy hands the application the same
octets the client sent is an integration test against a running proxy, not
something a signing vector can express. It belongs in the test suite beside
these, and a deployment behind a proxy that rewrites request targets does not
satisfy this specification.

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

3. **The server validates the request profile, then verifies v1 first and
   legacy second**, for release 0.28.0. An out-of-profile method, target, or
   body is rejected before either verifier and cannot fall through to legacy.

4. **Legacy use is logged** as an `event_log` row recording the pubkey and the
   scheme. Never the nonce and never the signature: a log that holds
   credential material is a second copy of the thing being protected.

5. **Release 0.29.0 removes nonce-only verification.** The release is named
   while the 0.28.0 window opens, not decided later. A deprecation without a
   date does not expire. Until removal, `/nonce` also advertises this boundary
   as `"nonce_only_removal": "0.29.0"`.

The instance is small enough to watch that log go quiet before cutting.
