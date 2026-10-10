import type { APIRoute } from 'astro';
import { url } from '../data/site';

// pages that shouldn't show in search say so themselves (noindex): blocking them here would hide that

export const GET: APIRoute = ({ site }) => new Response(`User-agent: *
Allow: /

Sitemap: ${new URL(url('sitemap.xml'), site)}
`, { headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
