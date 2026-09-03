/** @type {import('next').NextConfig} */
const nextConfig = {
  // react-leaflet v4's MapContainer does not clean up Leaflet's internal
  // `_leaflet_id` on its DOM node before React 18 Strict Mode's dev-only
  // double-invoke remounts it, throwing "Map container is already
  // initialized" (known upstream react-leaflet/Leaflet issue). Strict Mode's
  // double-invoke is a dev-only diagnostic; disabling it is the pragmatic
  // fix here rather than fighting the library or forcing a React 19 bump.
  reactStrictMode: false,
  agentRules: false,
};

module.exports = nextConfig;
