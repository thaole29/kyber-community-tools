import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// `base` is the path the bundle is served under. Cloudflare Pages serves
// at the root of <project>.pages.dev, so '/' is correct. Override via
// VITE_BASE env if you ever route under a sub-path (e.g. custom domain).
const BASE = process.env.VITE_BASE ?? '/';

export default defineConfig({
  base: BASE,
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
});
