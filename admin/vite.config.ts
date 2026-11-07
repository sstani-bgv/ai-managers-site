import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Сборка в ../site/admin/: Caddy отдаёт её как статику, /api/* уходит в app.py
export default defineConfig({
  base: '/admin/',
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': path.resolve(__dirname, './src') } },
  build: { outDir: '../site/admin', emptyOutDir: true },
  server: { proxy: { '/api': process.env.API || 'http://127.0.0.1:8091' } },
})
