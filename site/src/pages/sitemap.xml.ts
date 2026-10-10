// Every page search engines should know (robots.txt points here). The install-link page stays out: it is only for
// links that open FramePort.
import type { APIRoute } from 'astro';
import { DOCS } from '../data/docs';
import { LATEST } from '../data/releases';
import { url } from '../data/site';

export const GET: APIRoute = ({ site }) => {
  const pages: [string, string?][] = [
    [''], ['setup/'], ['download/'], ['changelog/', LATEST?.date], ['docs/'], ...DOCS.map((d) => [`docs/${d.slug}/`] as [string]),
  ];
  const body = pages.map(([path, mod]) => `  <url><loc>${new URL(url(path), site)}</loc>${mod ? `<lastmod>${mod}</lastmod>` : ''}</url>`);
  return new Response(`<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${body.join('\n')}
</urlset>
`, { headers: { 'Content-Type': 'application/xml; charset=utf-8' } });
};
