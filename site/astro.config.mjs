// FramePort's homepage. Served by GitHub Pages at https://spoopyghosty0.github.io/frameport/ (see
// .github/workflows/pages.yml); everything is static.
import { defineConfig } from 'astro/config';

export default defineConfig({
  site: 'https://spoopyghosty0.github.io',
  base: '/frameport',
  trailingSlash: 'always',
  build: { format: 'directory' },
  // Astro 7's compression drops the space between a line of text and a link on the next source line
  compressHTML: false,
});
