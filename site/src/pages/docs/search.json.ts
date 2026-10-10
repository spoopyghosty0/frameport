// The docs search index: every section of every doc page as plain text, fetched by the search box on first use.
import { DOCS, loadDoc, sections } from '../../data/docs';

export function GET() {
  return new Response(JSON.stringify(DOCS.flatMap((d) => sections(loadDoc(d)))), { headers: { 'Content-Type': 'application/json' } });
}
