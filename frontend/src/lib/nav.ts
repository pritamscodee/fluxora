import { getCollection } from 'astro:content';

export const SECTION_ORDER = [
  'Overview',
  'Getting started',
  'Core concepts',
  'API reference',
  'The agent',
  'Operations',
  'Project',
] as const;

export type Section = (typeof SECTION_ORDER)[number];

export interface NavEntry {
  slug: string;
  title: string;
  description: string;
  icon: string;
  accent: string;
  order: number;
}

/**
 * Sidebar tree, grouped by section and ordered within each. Built from the content
 * collection so adding an MDX file is all it takes to appear in the nav.
 */
export async function buildNav(): Promise<{ section: Section; entries: NavEntry[] }[]> {
  const docs = await getCollection('docs');
  const grouped = new Map<Section, NavEntry[]>();

  for (const doc of docs) {
    const section = doc.data.section as Section;
    const list = grouped.get(section) ?? [];
    list.push({
      slug: doc.id,
      title: doc.data.title,
      description: doc.data.description,
      icon: doc.data.icon,
      accent: doc.data.accent,
      order: doc.data.order,
    });
    grouped.set(section, list);
  }

  return SECTION_ORDER.filter((s) => grouped.has(s)).map((section) => ({
    section,
    entries: (grouped.get(section) ?? []).sort((a, b) => a.order - b.order),
  }));
}

/** Flattened reading order, for previous/next links at the foot of each page. */
export async function flatNav(): Promise<NavEntry[]> {
  return (await buildNav()).flatMap((group) => group.entries);
}
