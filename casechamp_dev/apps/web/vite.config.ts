import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: { manualChunks: { validation: ['ajv/dist/2020', 'ajv-formats'] } },
    },
  },
  server: {
    proxy: {
      '/api/v2': { target: process.env.CASECHAMP_API_PROXY ?? 'http://localhost:18081', changeOrigin: false },
    },
  },
});
