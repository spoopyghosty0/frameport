// Values the pages workflow passes in from the repo's variables (both optional; see components/Analytics.astro).
interface ImportMetaEnv {
  readonly PUBLIC_CF_ANALYTICS_TOKEN?: string;
  readonly PUBLIC_GA_ID?: string;
}

interface Window {
  /** records an analytics event once the visitor allowed Google Analytics; else does nothing */
  fpTrack?: (name: string, params?: Record<string, unknown>) => void;
}
