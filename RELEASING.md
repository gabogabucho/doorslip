# Releasing

## Bump the version first, always

`version` in `pyproject.toml`, before anything else.

A version already published cannot be replaced — not on PyPI, and not on a
machine that already installed it. Reusing a number means `pip install
--upgrade` sees nothing to do and quietly leaves the old code running. That
has already happened here: a release added a command, the number stayed put,
and an installed agent kept failing on a command its instructions had started
telling it to use. Nothing errored.

## Build and check

```bash
uv run pytest
uv run python demo.py
rm -rf dist && uv build
uv run --with twine twine check dist/*
```

`twine check` is not optional. A description that fails to render is permanent
for that release, and PyPI will not let you replace it.

Confirm the archive carries nothing private:

```bash
tar -tzf dist/*.tar.gz | grep -iE 'key\.json|\.db$|tmp-|outbox' && echo LEAK
```

CI runs all of this on every push, so the answer is usually already known.

## Publish

**Preferred: publish from a GitHub release.** Configure trusted publishing once
at <https://pypi.org/manage/account/publishing/> (owner ,
repository , workflow , environment ), then cut a
release and ........................................................................ [ 36%]
........................................................................ [ 73%]
...................................................                      [100%]
195 passed in 6.66s

[1m1. Each agent generates its own key and registers[0m
────────────────────────────────────────────────────────────────────
  gabo@doorslip.test     key hSf7JSCu7J0WTRXK…  (never sent to the server)
  tomas@doorslip.test    key GOb94W7h31LQATC6…  (never sent to the server)

[1m2. The welcome desk answers, from a template, spending no inference[0m
────────────────────────────────────────────────────────────────────
  reply arrived on thread e5ca90f7…, acknowledged
  [90mThis is the Doorslip welcome desk. Your agent generated its own …[0m

[1m3. Nobody can write to a stranger[0m
────────────────────────────────────────────────────────────────────
  refused, as designed → 403: the recipient has not accepted you

[1m4. An invitation code opens the door, in both directions[0m
────────────────────────────────────────────────────────────────────
  gabo@doorslip.test issued ds_inv_velgli3Naic…
  gabo@doorslip.test     sees ['tomas@doorslip.test', 'welcome@doorslip.test']
  tomas@doorslip.test    sees ['gabo@doorslip.test']

[1m5. Eight turns, each patching the state of the one before it[0m
────────────────────────────────────────────────────────────────────
  1. [36mgabo@doorslip.test    [0m Saturday barbecue?
     [90mpatch: {"topic": "saturday barbecue", "status": "proposed", "who": ["gabo@doorslip.test", "tomas@doorslip.test"], "when": [{"start": "2026-08-29T20:00:00-03:00", "confidence": "high"}, {"start": "2026-08-30T13:00:00-03:00", "confidence": "low"}]}[0m
  2. [36mtomas@doorslip.test   [0m Saturday night works, drop the Sunday option.
     [90mpatch: {"status": "negotiating", "when": [{"start": "2026-08-29T20:00:00-03:00", "confidence": "high"}]}[0m
  3. [36mgabo@doorslip.test    [0m My place then.
     [90mpatch: {"where": "my place"}[0m
  4. [36mtomas@doorslip.test   [0m I'll put in for the meat.
     [90mpatch: {"budget": {"amount": 18000, "currency": "ARS", "per": "person"}}[0m
  5. [36mgabo@doorslip.test    [0m I'll handle fire and salads.
     [90mpatch: {"tasks": [{"what": "fire", "who": "gabo@doorslip.test"}, {"what": "salads", "who": "gabo@doorslip.test"}]}[0m
  6. [36mtomas@doorslip.test   [0m I'll bring wine — note the whole array is resent.
     [90mpatch: {"tasks": [{"what": "fire", "who": "gabo@doorslip.test"}, {"what": "salads", "who": "gabo@doorslip.test"}, {"what": "wine", "who": "tomas@doorslip.test"}]}[0m
  7. [36mgabo@doorslip.test    [0m One of us is vegetarian, keep it in mind.
     [90mpatch: {"constraints": ["one vegetarian guest"]}[0m
  8. [36mtomas@doorslip.test   [0m Confirmed.
     [90mpatch: {"status": "confirmed"}[0m

[1m6. Both sides reconstruct the SAME state, deterministically[0m
────────────────────────────────────────────────────────────────────
{
  "topic": "saturday barbecue",
  "status": "confirmed",
  "who": [
    "gabo@doorslip.test",
    "tomas@doorslip.test"
  ],
  "when": [
    {
      "start": "2026-08-29T20:00:00-03:00",
      "confidence": "high"
    }
  ],
  "where": "my place",
  "budget": {
    "amount": 18000,
    "currency": "ARS",
    "per": "person"
  },
  "tasks": [
    {
      "what": "fire",
      "who": "gabo@doorslip.test"
    },
    {
      "what": "salads",
      "who": "gabo@doorslip.test"
    },
    {
      "what": "wine",
      "who": "tomas@doorslip.test"
    }
  ],
  "constraints": [
    "one vegetarian guest"
  ]
}

  8 patches applied in parent order
  divergence detected: False

[1m7. Done-criterion (spec §2)[0m
────────────────────────────────────────────────────────────────────
  [32mOK[0m  two agents, different people, mutual address books
  [32mOK[0m  thread of at least 8 turns (got 8)
  [32mOK[0m  at least 2 partial state updates (got 7)
  [32mOK[0m  logs exist to count state errors

[1m8. Instrumentation (spec §9)[0m
────────────────────────────────────────────────────────────────────
  pairs_with_second_conversation         0
  average_turns_per_thread               3.67
  state_off_recommended_shape            0.0
  contacts_off_default_disclosure        0
  messages_total                         11

Checking dist/doorslip-0.16.0-py3-none-any.whl: PASSED
Checking dist/doorslip-0.16.0.tar.gz: PASSED builds, tests, attests and uploads.

No token exists to leak, and the package carries an attestation tying it to
this repository and commit. That matters more than convenience: an agent asked
to install something has no way to tell a real package from a supply chain
attack, and provenance is the only thing that answers the question rather than
asking for trust.

### Publishing by hand

Get a token at <https://pypi.org/manage/account/token/>, scoped to this
project once it exists.

```bash
uv publish --token pypi-XXXXXXXX
```

Or put it in the environment and leave it out of your shell history:

```bash
export UV_PUBLISH_TOKEN=pypi-XXXXXXXX
uv publish
```

**Nobody needs to see that token but you.** Not a collaborator, not an agent,
not a chat window. It grants the ability to publish code that other people's
machines will install and run.

Try it on TestPyPI first if the packaging changed:

```bash
uv publish --publish-url https://test.pypi.org/legacy/ --token pypi-XXXX
```

## Then update the server

The server hands arriving agents both the skill and a wheel to install from,
so a release is not finished until it does:

```bash
scp SKILL.md REFERENCE.md dist/doorslip-VERSION-py3-none-any.whl root@SERVER:/tmp/
ssh root@SERVER 'cd /tmp && ./install.sh YOUR-HOST /tmp/doorslip-VERSION-py3-none-any.whl'
```

`install.sh` restarts the service rather than only enabling it. `systemctl
enable --now` starts a stopped service and leaves a running one alone, which
means new code installed and the old process kept serving — silently. That one
also already happened.

Once the package is on PyPI, drop the wheel argument and the installer pulls
from there instead.

## Tag it

```bash
git tag -a v0.16.0 -m "0.16.0"
git push origin v0.16.0
```

## If a release turns out to be broken

You cannot replace it. Yank it so nothing new installs it, and ship a fix
under the next number:

```bash
# on pypi.org, under Manage → Releases → Yank
```

Yanking leaves it installable by exact pin, so anybody depending on it keeps
working while nobody new picks it up by accident.
