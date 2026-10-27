// @ts-check
import { defineConfig } from 'astro/config';

import react from '@astrojs/react';
import mdx from '@astrojs/mdx';
import tailwindcss from '@tailwindcss/vite';

// https://astro.build/config
export default defineConfig({
  site: 'https://fluxora.dev',
  integrations: [react(), mdx()],

  markdown: {
    shikiConfig: {
      // Dark code blocks sit inside the light neobrutalist page on purpose: the slab of
      // near-black is part of the visual rhythm rather than an accident of theming.
      theme: 'github-dark-default',
      wrap: false,
    },
  },

  vite: {
    plugins: [tailwindcss()],
  },
});
