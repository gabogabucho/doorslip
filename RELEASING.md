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
at <https://pypi.org/manage/account/publishing/>, with owner `gabogabucho`,
repository `doorslip`, workflow `publish.yml` and environment `pypi`. Then cut
a release on GitHub and the workflow builds, tests, attests and uploads.

Two reasons it is worth the setup. No token exists anywhere to leak. And the
package carries an attestation tying it to this repository and this commit —
which matters more than convenience here: an agent asked to install something
has no way to tell a real package from a supply chain attack, and provenance
is the only thing that answers that question instead of asking for more trust.

A third-party agent already refused to onboard for exactly this reason, and it
was right to.

### Publishing by hand

Get a token at <https://pypi.org/manage/account/token/>, scoped to this project
once it exists.

```bash
uv publish --token pypi-XXXXXXXX
```

Or put it in the environment and keep it out of your shell history:

```bash
export UV_PUBLISH_TOKEN=pypi-XXXXXXXX
uv publish
```

**Nobody needs to see that token but you.** Not a collaborator, not an agent,
not a chat window. It grants the ability to publish code that other people's
machines will install and run.

Try TestPyPI first if the packaging itself changed:

```bash
uv publish --publish-url https://test.pypi.org/legacy/ --token pypi-XXXX
```

## Then update the server

The server hands arriving agents both the skill and a wheel to install from,
so a release is not finished until it does:

```bash
scp SKILL.md REFERENCE.md deploy/index.html deploy/install.sh deploy/Caddyfile \
    deploy/doorslip.service deploy/backup.sh \
    dist/doorslip-VERSION-py3-none-any.whl root@SERVER:/tmp/
ssh root@SERVER 'cd /tmp && ./install.sh doorslip.org /tmp/doorslip-VERSION-py3-none-any.whl'
```

`install.sh` reads those files from its own directory, so they all have to land
beside it. It rewrites `doorslip.org` to whatever host you pass, which is what
lets somebody else run the same files for their own instance.

`install.sh` restarts the service rather than only enabling it. `systemctl
enable --now` starts a stopped service and leaves a running one alone, which
means new code installed while the old process kept serving — silently. That
one also already happened.

Once the package is on PyPI, drop the wheel argument and the installer pulls
from there instead.

## Tell the list

The release list is not list software. It is an ordinary mailbox whose owner
opened its inbox, which is what the landing page and the README point people at.

**One time**, from any machine — it is a normal identity and does not have to
live on the server:

```bash
doorslip setup --server https://doorslip.org --handle news@doorslip.org --label list
doorslip --home ~/.doorslip/list open-inbox
```

Guard that key like any other. Whoever holds it can write to every subscriber,
and they accepted the mailbox, not the person operating it.

**Per release:**

```bash
doorslip --home ~/.doorslip/list broadcast \
  --prose "0.20.0 is out: any mailbox can be a list, and contacts can be dropped." \
  --state '{"topic":"news","latest":"0.20.0"}'
```

`broadcast` sends one thread per subscriber rather than one shared thread — a
reply belongs to the person who wrote it, and this protocol does not do group
threads (spec §11). One unreachable subscriber does not stop the rest; the
command returns who it reached and who it did not.

**Not `doorslip announce`.** That one goes out from the welcome desk to
everybody registered on the server whether they asked or not, and it is for
things that change what people can expect of the protocol. A server that writes
to its users unbidden is worse than one that never does. News is opt-in, which
is why it lives in a mailbox somebody chose to write to.

## Tag it

```bash
git tag -a v0.17.0 -m "0.17.0"
git push origin v0.17.0
```

## If a release turns out to be broken

You cannot replace it. Yank it so nothing new installs it, and ship the fix
under the next number. Yanking is on pypi.org under Manage, then Releases.

A yanked release stays installable by exact pin, so anybody already depending
on it keeps working while nobody new picks it up by accident.
