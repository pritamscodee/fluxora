# Fluxora documentation

The project's documentation site: Astro 7, Tailwind 4, shadcn/ui components as React
islands, react-icons, and Mermaid diagrams rendered client-side in a theme matched to the
page.

```bash
npm install
npm run dev      # http://localhost:4321
npm run build    # static output in dist/
```

## Adding a page

Drop an `.mdx` file in `src/content/docs/`:

```mdx
---
title: My page
description: One sentence, shown in the sidebar card and as the page subtitle.
section: Core concepts     # see src/content.config.ts for the allowed set
order: 6                   # position within the section
accent: cyan               # yellow | lime | cyan | pink | orange | violet
---

import Callout from '@/components/docs/Callout.astro';
import Mermaid from '@/components/Mermaid.astro';
```

The sidebar, the docs index and previous/next links all build themselves from the content
collection, so there is no navigation file to keep in sync.

## Components

| Component | Use |
| --- | --- |
| `Callout` | `note`, `tip`, `warning`, `danger`, `decision` |
| `Mermaid` | `chart` plus an optional `caption` |
| `Endpoint` | An API endpoint row: method, path, auth |
| `Stat` | A number with a label |

## Design

Neobrutalism, defined in `src/styles/global.css`:

- Every surface has a hard black border; no soft greys standing in for structure.
- Shadows are solid offsets, never blurred — depth is drawn, not simulated.
- Colour is flat and loud, and labels meaning: HTTP verbs, risk levels, callout kinds.
- Interaction is physical — elements press into their own shadow.
- Radii stay near zero.

Both light and dark themes are first-class. The theme is applied before first paint to
avoid a flash, and Mermaid re-renders its diagrams on theme change.

## Gotcha

In `.astro` and `.mdx`, a `{` in markup starts an expression. JSON samples belong in
fenced code blocks, or in a frontmatter variable rendered with `set:html`.
