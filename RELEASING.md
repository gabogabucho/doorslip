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
scp SKILL.md REFERENCE.md dist/doorslip-VERSION-py3-none-any.whl root@SERVER:/tmp/
ssh root@SERVER 'cd /tmp && ./install.sh YOUR-HOST /tmp/doorslip-VERSION-py3-none-any.whl'
```

`install.sh` restarts the service rather than only enabling it. `systemctl
enable --now` starts a stopped service and leaves a running one alone, which
means new code installed while the old process kept serving — silently. That
one also already happened.

Once the package is on PyPI, drop the wheel argument and the installer pulls
from there instead.

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
