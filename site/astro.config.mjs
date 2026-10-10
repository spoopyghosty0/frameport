// FramePort's homepage, https://frameport.app/: GitHub Pages with a custom domain (see
// .github/workflows/pages.yml); everything is static.
import { defineConfig } from 'astro/config';

export default defineConfig({
  site: 'https://frameport.app',
  trailingSlash: 'always',
  build: { format: 'directory' },
  // Astro 7's compression drops the space between a line of text and a link on the next source line
  compressHTML: false,
});
