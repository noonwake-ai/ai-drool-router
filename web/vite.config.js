import { defineConfig } from 'vite';

// DROOL_BASE lets the same source build two artefacts:
//   "/"   -> served by detector.server behind a reverse proxy (default)
//   "./"  -> the static GitHub Pages demo, which has no backend
// Relative asset and API paths keep the demo working from a subdirectory.
export default defineConfig({
  base: process.env.DROOL_BASE || '/',
  server: {proxy: {'/api': 'http://127.0.0.1:4191', '/artifacts': 'http://127.0.0.1:4191'}},
  build: {target: 'es2022'},
});
