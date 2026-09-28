// Every backend path is resolved against the app base so the same bundle works
// behind a reverse proxy at "/" and as a static snapshot served from a
// subdirectory (the GitHub Pages demo).
// `import.meta.env` is injected by Vite at build time and is absent under plain
// `node --test`, so fall back to the root base there.
const ENV = import.meta.env || {};
const BASE = ENV.BASE_URL || '/';

export function endpoint(path) {
  const base = BASE.endsWith('/') ? BASE.slice(0, -1) : BASE;
  const suffix = String(path).replace(/^\/+/, '');
  return `${base}/${suffix}`;
}

export const stateUrl = () => endpoint('/api/state');
export const runUrl = id => endpoint('/api/runs/' + id);
export const artifactUrl = id => endpoint('/artifacts/' + id + '.html');
export const pauseUrl = id => endpoint('/api/accounts/' + id + '/pause');
export const priceUrl = id => endpoint('/api/accounts/' + id + '/price');

// The static demo has no backend: the snapshot ships a read-only state file and
// every control endpoint is intentionally absent.
export const isStaticDemo = () => typeof document !== 'undefined'
  && document.querySelector('meta[name="drool-demo"]') !== null;
