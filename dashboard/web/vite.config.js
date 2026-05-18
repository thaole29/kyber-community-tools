import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// During `vite` dev, proxy /api to the FastAPI server.
// Production build (`vite build`) emits to dist/, served by FastAPI directly.
export default defineConfig({
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
