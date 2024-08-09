import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../static',
    emptyOutDir: true,
    sourcemap: true,
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ['react', 'react-dom']
        }
      }
    }
  },
  server: {
    open: true, // Automatically open the browser
    hmr: {
      protocol: 'ws', // WebSocket protocol
      host: 'localhost',
    },
    proxy: {
      '/ask': 'http://localhost:5000',
      '/chat': 'http://localhost:5000'
    }
  }
});
