import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Served same-origin by the Python gateway from a hostPath-mounted dist/, so
// asset URLs must be relative (base './'). Dev proxies the JSON/proxy API to a
// local gateway port-forward so the SPA and the real backends share an origin.
export default defineConfig({
  base: './',
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    outDir: 'dist',
    // Keep chunks well under the etcd/ConfigMap ceiling in case we ever fall
    // back to ConfigMap serving; hostPath is the primary path and has no limit.
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        manualChunks: {
          arco: ['@arco-design/web-vue'],
          vue: ['vue', 'vue-router', 'vue-i18n', 'pinia'],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      ['/papi', '/oapi', '/prom', '/am', '/auth'].map((p) => [
        p,
        { target: 'http://127.0.0.1:8888', changeOrigin: false },
      ]),
    ),
  },
})
