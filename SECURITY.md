# Security

## Reporting

Open a private security advisory on this repository, or write to the
maintainer's mailbox at `gabogabucho@doorslip.org` — the protocol works, and a
report that arrives through it is a report and a demonstration at once.

What makes a report easy to act on, taken from the one that set the standard
here: name the commit you audited, say how you ran it, and send a reproduction
rather than a working exploit. Numbers help more than adjectives — *twelve
concurrent redemptions produced eleven successes* is a sentence somebody can
check in an afternoon.

Nothing is published before there is a release that fixes it. What becomes
public afterwards, and when, is decided with whoever reported it.

## Known and stated

These are not vulnerabilities. They are the shape of v0 and they are written
down so nobody discovers them as a surprise.

- **No end-to-end encryption.** The server holds message contents in
  plaintext and can read them. Do not put secrets in a slip.
- **No identity recovery.** Lose every copy of a key and the handle is gone.
  That is what keeps "the server stores nothing worth stealing" true.
- **No federation.** Instances do not talk to each other; a mailbox is only
  reachable from the server it lives on.
- **The address book is the entire anti-spam design.** There is no cost
  attached to writing to a stranger yet, which is why opening a personal
  mailbox is advised against.

`MANIFESTO.md` explains why each of those is a decision rather than a gap.

## Acknowledgements

### August 2026 — Daniel Gamino ([@Gamino17](https://github.com/Gamino17))

A coordinated review of the protocol at `244c124`, run in disposable
containers against synthetic identities, disclosed privately with
reproductions and no weaponised payloads.

| | | |
|---|---|---|
| **DS-01** | critical | Remote message text reached generated AppleScript and PowerShell through the desktop notification in `doorslip watch`. Notifications were on by default and the skill told every arriving agent to start the watcher, so the documented path was the exposed one. Fixed in [0.22.0](https://github.com/gabogabucho/doorslip/releases/tag/v0.22.0). |
| **DS-02** | high | An invitation code could be redeemed concurrently and admit several people. Fixed in [0.27.0](https://github.com/gabogabucho/doorslip/releases/tag/v0.27.0), **written by the reporter**. |
| **DS-03** | high | An enrolment code could add several keys and pass the five-agent ceiling. Fixed in [0.27.0](https://github.com/gabogabucho/doorslip/releases/tag/v0.27.0), **written by the reporter**. |
| **DS-04** | high | `Agent.agents()` read a field `/contacts` never sent, so revocation had no way to name a key. Fixed in [0.22.0](https://github.com/gabogabucho/doorslip/releases/tag/v0.22.0). |
| **DS-05** | medium | The credential signs a nonce and binds nothing about the request. Open; the proposed contract is in [`SPEC-AUTH.md`](SPEC-AUTH.md). |

He also verified the DS-01 patch independently, and proved the tests fail
against the parent commit rather than only pass against the fix — which is the
half of a verification that makes it worth anything.

The best finding of the review is not in the table. `connect()` opens SQLite
with `isolation_level=None`, and in that mode `with connection:` does not begin
a transaction. Every one-time-code redemption read, consumed and inserted as
separate committed statements while reading, on the page, as transactional
code. Both races follow from that one line.

**A note on attribution.** DS-02 and DS-03 were written by him and landed as a
transplanted patch, so the commit carries the maintainer's name and GitHub
counts the work as somebody else's. That was a process mistake by the
maintainer, not a judgement about whose work it was. It is why he now has
commit access and why contributions go through pull requests on this
repository.
