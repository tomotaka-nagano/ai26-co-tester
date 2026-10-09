import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  base: '/ui/',
  plugins: [vue()],
  server: {
    proxy: {
      '/verifications': 'http://127.0.0.1:8000',
      '/test-environments': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
    },
  },
})
