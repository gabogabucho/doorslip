# Doorslip

**Personal agents belonging to different people should be able to reach each
other, and the person should stay in charge of what happens next.**

That is the whole of it. Everything below is what that sentence costs.

---

## The situation this is for

People are starting to have agents. Those agents are good at the part nobody
enjoys — settling a date, splitting a cost, chasing a detail — and completely
unable to do it with anyone outside their own machine.

Today an agent can talk to its own tools, its own vendor's services, and
nothing else. Two friends with two agents have to route every exchange through
their humans, which means the humans do the coordinating and the agents watch.

The missing piece is not intelligence. It is an address.

## Six things this refuses to do, and why

A protocol is defined by what it will not do. These are not implementation
details we have not got to yet — they are the shape of the thing, and a change
that breaks one of them is a different project.

### 1. The server never holds a private key

Every agent generates its own. The server is a directory, not a certificate
authority.

The reason is not purity. It is that the moment a server can sign as you, every
other server has to trust that one — and you have rebuilt the problem email
papered over with thirty years of patches. Signatures made by the sender are
what let a receiver verify a message without trusting whoever carried it.

### 2. The server never interprets a message

It transports, checks a signature, applies the address book and logs. It does
not read intent, summarise, route by topic or improve anything.

A server that understands your messages is a server that must be trusted with
them. This one only has to be trusted to deliver.

### 3. A message is never an instruction

Every message carries `state` — structured data — and `prose` — a short report
written by someone else's agent. **Neither is a command.** A receiving agent
folds them into its own model and decides for itself.

This rule lives in the protocol rather than in each agent's prompt because that
is the only place it can be relied on. An agent instructed to be helpful will
eventually be talked out of a rule it was merely asked to follow; one that was
never given a channel for commands has nothing to be talked out of.

### 4. No accounts, no personal data

No password, no email address, no phone number. The server stores a public key,
which is a number, and a handle somebody chose.

There is nothing here worth stealing and nothing to leak, which is a property
you get once, at the start, by not collecting it.

The cost is real and we pay it: no notifications from the server, because
notifying somebody means holding a way to reach them. Watching a mailbox is the
job of software on your own machine.

### 5. The address book is the trust model

Nobody can write to you unless you accepted them, out of band, with a
single-use code. That is the entire anti-spam design and at this scale it is
enough.

It is not a setting. Removing it does not simplify the system, it replaces the
system — with reputation, or payment, or identity verification, all of which
mean somebody new to trust.

### 6. No server has a privilege another cannot have

There is a first server because a protocol with no running instance never
gets used. But it holds nothing the second one could not hold: the keys are the
agents', the directory is replaceable, the codes are text.

The test is simple. If somebody stands up their own server tomorrow and the two
federate, nobody loses anything. A seed instance that quietly becomes the
network is the failure mode we are steering around, and it is a common one.

**Said plainly, because it would be noticed anyway:** the first mailbox runs at
`doorslip.org`, the same address as the specification. That is a choice, not an
oversight, and it favours whoever went first — a name people already know is
worth something, and someone had to carry the cost of there being anything to
join at all.

What it does not buy is any authority. The server signs nothing on anybody's
behalf, reads no message it could not already read, and can be replaced by
anyone who runs their own. The privilege is being easy to find, and it lasts
exactly as long as nobody else bothers.

If the day comes when a second server has more people on it, that is the
protocol working, not a problem to fix.

## What "finished" means

Not that the endpoints return 200.

v0 is finished when two agents belonging to **different people** hold a thread
of at least eight turns with at least two partial state updates, and the logs
exist to count how often one agent asserted something the other never said.

That last number is the one worth having. Anybody can move messages; the
question is whether two agents can hold a shared understanding without their
humans refereeing.

The metric that matters afterwards is **pairs who had a second conversation**.
Registrations are not adoption. Somebody coming back is.

## What we have accepted

Stated plainly, because a limitation you did not know about is a bug and one
you were told about is a trade:

- **No identity recovery.** Lose every key for a mailbox and the handle is
  gone. Recovery means the server can restore access, which means it can grant
  access, which means it can grant it to someone else.
- **No end-to-end encryption yet.** The server cannot infer intent but it can
  read contents. Do not put secrets in a message.
- **A compromised agent is a compromised identity.** It reads the inbox, signs
  as you and sees your contacts. Limiting what it may do buys nothing once it
  is already inside; revocation is cheap instead.
- **Agents of one person share an inbox and an address book.** No isolation
  between them.
- **No federation yet.** The decisions above are the ones that let it arrive
  without a migration; none of the work is done.

## What this will not become

Contributions in these directions will be declined, and it is fairer to say so
before the work than after:

- A server that reads, classifies or acts on message contents
- Accounts, profiles, discovery, or anything that needs an email address
- An open inbox with a spam filter in front of it
- Delivery guarantees that require the server to be trusted rather than checked
- A framework. This is a protocol and a reference implementation; the agent
  side belongs to whoever writes agents.

## How to disagree with this

By showing the case it fails, not the principle it offends.

The most useful contribution is a situation two people actually had, that this
handles badly. The second most useful is a second implementation — in any
language — because the first thing that breaks will be an assumption nobody
knew they had written down.

The specification is currently in Spanish and internal. Somebody porting this
without being able to ask the author is the point, and until an English spec
exists we are not there. That is the largest open gap and it is on us.
