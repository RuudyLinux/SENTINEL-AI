/** @type {import('next').NextConfig} */
const nextConfig = {
  // Strict mode was turned off for react-leaflet's MapContainer, which threw
  // "Map container is already initialized" on the dev double-mount.
  // CameraMap uses plain Leaflet now, so this may be safe to turn back on.
  reactStrictMode: false,
  agentRules: false,
};

module.exports = nextConfig;
