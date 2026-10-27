import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';

/**
 * Docs pages. `order` drives sidebar position within a section, and `section` groups
 * them — both are required so a new page cannot quietly land in the wrong place or at
 * the bottom of the nav.
 */
const docs = defineCollection({
  loader: glob({ pattern: '**/*.{md,mdx}', base: './src/content/docs' }),
  schema: z.object({
    title: z.string(),
    description: z.string(),
    section: z.enum([
      'Overview',
      'Getting started',
      'Core concepts',
      'API reference',
      'The agent',
      'Operations',
      'Project',
    ]),
    order: z.number(),
    icon: z.string().default('RiFileTextLine'),
    accent: z
      .enum(['yellow', 'lime', 'cyan', 'pink', 'orange', 'violet'])
      .default('yellow'),
  }),
});

export const collections = { docs };
