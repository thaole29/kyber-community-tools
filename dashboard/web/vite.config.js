import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// `base` is the path the bundle is served under:
//   - GitHub Pages (project site): /kyber-community-tools/
//   - Local FastAPI / ngrok      : /
// Override via VITE_BASE env if you need a different prefix.
const BASE = process.env.VITE_BASE ?? (process.env.GITHUB_ACTIONS ? '/kyber-community-tools/' : '/');

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
