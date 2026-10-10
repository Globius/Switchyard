# 0042. UI theme: shadcn tokens, status tokens and fonts

Status: Accepted
Date: 2026-10-10

## Context

The frontend design (03) says the shadcn theme is picked once and committed, and that status colours
are Switchyard additions. Claude Design drew every screen against one concrete theme (the
"Foundations" artboard) and exported it as `globals.css` in the UI handoff
(`docs/design/ui-handoff/README.md`). No theme existed in `ui/` before this, only a neutral
placeholder from the scaffold.

## Decisions

1. **The handoff `globals.css` is the theme**, committed as `ui/src/globals.css` (Tailwind v4,
   OKLCH, light and dark through the `.dark` class). The names of the shadcn tokens, the status
   tokens (`status-healthy|degraded|down|draining`, `draft`, `live`, `pending-approval`, each with a
   `-bg` tint) and the diff tokens (`diff-add`, `diff-del`) are the contract between the canvas, the
   code and any Figma kit. Changing a value is a normal change; renaming a token needs a new ADR.
2. **Fonts:** Geist (UI) and Geist Mono (IPs, ports, ids, config), self-hosted through
   `@fontsource-variable/*` so the UI makes no request to a font CDN (the controller may run
   air-gapped). The theme's font names are the Fontsource family names (`Geist Variable`).
3. **Light is the default theme**; dark follows the user's choice, stored in the browser.
4. **Colour never carries meaning alone.** Every status token is used with an icon and a label.
5. **`tw-animate-css`** replaces the deprecated `tailwindcss-animate` for shadcn animations.

## Consequences

- Components are generated with the shadcn CLI against this theme and use only these tokens.
- Contrast was designed for 4.5:1 but not measured; `axe` in tests and CI is the check.
- Self-hosted fonts add about 100 kB to the build; acceptable for an internal tool.

## Alternatives considered

- **Stock shadcn neutral theme:** no brand or status vocabulary; every screen would need overrides.
- **Google Fonts link:** simpler, but a third-party request from an operator console. Rejected.
