import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [svelte()],
  server: {
    port: 5174,
    proxy: {
      '/api': { target: 'http://192.168.8.215:8080', ws: true }
    }
  }
});
