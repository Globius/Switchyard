"""MkDocs hook: point links that leave ``docs/`` at the repository instead of the site.

Docs link to code (``../controller/...``) and deployment files. Those resolve on GitLab but not in the
built site, so this hook rewrites any relative Markdown link whose target lies outside ``docs/`` to the
file's URL on the repository's main branch. Links inside ``docs/`` are untouched and still checked by
``mkdocs build --strict``.

Example:
    In ``docs/production.md``, ``[x](../deploy/prod/README.md)`` becomes
    ``[x](https://git.example.com/globius-oss/switchyard/-/blob/main/deploy/prod/README.md)``.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterator
from typing import Any

from mkdocs.exceptions import PluginError

_LINK = re.compile(r"(\]\()(\.\./[^)\s#]+)([^)]*\))")


def on_page_markdown(markdown: str, page: Any, config: Any, files: Any) -> str:
    """Rewrite out-of-tree relative links on one page.

    Args:
        markdown: The page source.
        page: The MkDocs page; its ``file.src_uri`` is the path under ``docs/``.
        config: The MkDocs config; ``repo_url`` is the link base.
        files: Unused.

    Returns:
        The page source with out-of-tree links made absolute.
    """
    base = config["repo_url"].rstrip("/") + "/-/blob/main/"
    here = posixpath.dirname("docs/" + page.file.src_uri)

    def fix(m: re.Match[str]) -> str:
        target = posixpath.normpath(posixpath.join(here, m.group(2)))
        if target.startswith("docs/") or target.startswith(".."):
            return m.group(0)
        return m.group(1) + base + target + m.group(3)

    return _LINK.sub(fix, markdown)


def _titles(nav: Any) -> Iterator[str]:
    """Every title in a ``nav`` config, at any depth."""
    for item in nav or []:
        if isinstance(item, dict):
            for title, child in item.items():
                yield str(title)
                if isinstance(child, list):
                    yield from _titles(child)


def on_config(config: Any) -> Any:
    """Refuse nav titles with markup: a ``<`` in a title breaks every page of the site.

    Args:
        config: The MkDocs config.

    Returns:
        The config, unchanged.

    Raises:
        PluginError: A nav title contains ``<``.
    """
    bad = [title for title in _titles(config["nav"]) if "<" in title]
    if bad:
        raise PluginError(f"nav titles must be plain text: {bad}")
    return config
