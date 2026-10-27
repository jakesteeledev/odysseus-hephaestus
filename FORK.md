# Odysseus-Hephaestus fork notes

This repository is a personal fork of [odysseus-dev/odysseus](https://github.com/odysseus-dev/odysseus).
It stays close to upstream so that upstream updates are easy to take.

## Branches

| Branch | Purpose |
|---|---|
| `dev` (and `main`) | Clean mirrors of upstream. Never commit fork changes here. |
| `hephaestus` | The fork's working branch and GitHub default: `dev` plus the theme, fixes and features. |
| `feature/*` | New work, branched off `hephaestus` and merged back into it. |

Changes to upstream files are marked with `HEPHAESTUS PATCH` comments, so they are easy to find when
resolving merge conflicts: `git grep -n "HEPHAESTUS PATCH"`.

## Remotes

```bash
git remote -v
# origin    https://github.com/jakesteeledev/odysseus-hephaestus.git   (this fork)
# upstream  https://github.com/odysseus-dev/odysseus.git                (original)
```

## Pulling upstream updates

```bash
git fetch upstream

# 1. Update the mirror branch (fast-forward only, so it never diverges)
git switch dev
git merge --ff-only upstream/dev
git push origin dev

# 2. Bring the update into the fork branch
git switch hephaestus
git merge dev            # or: git rebase dev  (then: git push --force-with-lease)
# resolve any conflicts around "HEPHAESTUS PATCH" markers, then run the tests:
.venv/bin/python -m pytest -q -n auto
git push origin hephaestus
```

Prefer `merge` once the branch is shared or has feature branches on top of it; `rebase` keeps the
history linear but rewrites `hephaestus`. Do the same for `main` if you track it.

## Building and running with Docker Compose

Upstream's compose file pulls the prebuilt `ghcr.io/odysseus-dev/odysseus` image, which does **not**
contain this fork's changes. Build the image from this checkout instead, under a local name so that
`docker compose pull` can never replace it with the upstream image. In `.env`:

```bash
ODYSSEUS_IMAGE=odysseus-hephaestus:local
```

Then:

```bash
docker compose up -d --build      # build from this checkout and (re)start
docker compose logs -f odysseus   # watch start-up
```

Rebuild with the same command after every merge or upstream update. User data lives in `data/`
(bind-mounted, git-ignored) and is kept across rebuilds.

## Tests

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q -n auto
```

Browser and JavaScript tests need Node.js and Playwright; without them they fail or are skipped, on
upstream as well as here.

## Licence

Odysseus is licensed under the [GNU AGPL v3](LICENSE). This fork keeps that licence unchanged. If you
run it for other people over a network, the AGPL requires offering them the source of the version
you run; this public repository does that.
