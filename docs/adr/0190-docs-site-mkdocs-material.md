# 0190. Docs site: MkDocs with the Material theme

Status: Accepted
Date: 2026-10-12

## Context

The docs are Markdown under `docs/` (guides, runbooks, ADRs, lab results) and read well on GitLab, but there was no
built site, no navigation across them, no search, and nothing caught a broken link. The site has to publish to GitLab
Pages (the primary remote) and GitHub Pages (a mirror), and it has to build locally because GitLab CI is paused.

## Decision

- **MkDocs 1.6 with Material for MkDocs**, pinned in `docs/requirements.txt`, configured by `mkdocs.yml` at the repo
  root with `docs_dir: docs` and output in `public/` (the directory GitLab Pages serves).
- The build is always `mkdocs build --strict` with MkDocs' own link validation set to warn on missing files, unknown
  links and missing anchors, so any broken link inside `docs/` fails the build.
- Links from a doc to code or deploy files outside `docs/` are rewritten to the repository URL on `main` by a small
  hook (`docs/hooks/repo_links.py`), so they work on the site and stay relative in the repo.
- One command, `make docs` (or the `uvx` line it runs, on Windows), is used locally, in the GitLab `docs`/`pages` jobs and
  in `.github/workflows/docs.yml`. Only `uv` is needed.
- `docs/prompts/` and `docs/api/openapi.json` are kept out of the site; the API reference stays live at `/api/v1/docs`.

## Consequences

- Docs stay plain Markdown; nothing about writing an ADR or runbook changes. New pages should be added to `nav` in
  `mkdocs.yml`, otherwise they build but are only reachable by link or search.
- Material-specific syntax (admonitions, details) renders on the site and degrades to plain text on GitLab.
- GitLab Pages publishes only once CI is re-enabled; until then `make docs` is the check.

## Alternatives considered

- **Sphinx + MyST:** strong for API docs from Python, heavier configuration, and reST habits leak into Markdown.
- **Docusaurus / VitePress:** good sites, but add a second Node toolchain for docs and use MDX-flavoured Markdown.
- **GitLab wiki:** no review in merge requests and no GitHub mirror.
