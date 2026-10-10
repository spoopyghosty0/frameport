// FramePort's releases as an Atom feed, for feed readers (every page links it in its head).
import type { APIRoute } from 'astro';
import { LATEST, RELEASES } from '../../data/releases';
import { url } from '../../data/site';

const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

export const GET: APIRoute = ({ site }) => {
  const page = new URL(url('changelog/'), site).href;
  const self = new URL(url('changelog/feed.xml'), site).href;
  const entries = RELEASES.slice(0, 30).map((r) => {
    const link = `${page}#v${r.version.replaceAll('.', '-')}`;
    return `  <entry>
    <title>FramePort ${esc(r.version)}</title>
    <id>${esc(link)}</id>
    <link href="${esc(link)}" />
    <updated>${r.date}T00:00:00Z</updated>
    <summary>${esc(r.highlights.join(' · '))}</summary>
    <content type="html">${esc(r.html)}</content>
  </entry>`;
  });
  return new Response(`<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>FramePort releases</title>
  <subtitle>What's new in each FramePort release</subtitle>
  <id>${esc(page)}</id>
  <link href="${esc(page)}" />
  <link rel="self" href="${esc(self)}" />
  <updated>${LATEST?.date ?? '2026-01-01'}T00:00:00Z</updated>
  <author><name>FramePort</name></author>
${entries.join('\n')}
</feed>
`, { headers: { 'Content-Type': 'application/atom+xml; charset=utf-8' } });
};
