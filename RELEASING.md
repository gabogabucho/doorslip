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
