import type { CapacitorConfig } from '@capacitor/cli';

// IMPORTANT: VoxBuddy needs a live backend (REST + WebSocket) to function
// at all — there's nothing meaningful to bundle as static offline assets
// the way a typical Capacitor app does. So this points the native shell
// at your REAL deployed backend URL (server.url mode) rather than
// bundling www/ as the source of truth. The www/ folder here is a
// required-but-unused placeholder Capacitor's CLI needs to exist.
//
// Replace the placeholder below with your real deployed URL once you've
// completed Step 1 of docs/PLAY_STORE_PUBLISHING.md (deploy to Render or
// similar) — see docs/MOBILE_BUILD.md for the full picture.
const config: CapacitorConfig = {
  appId: 'com.voxbuddy.app',
  appName: 'VoxBuddy',
  webDir: 'www',
  server: {
    // "/app" — the bare domain root serves a separate internal CIE debug
    // page (frontend/index.html), not the real app UI (app-preview.html).
    url: 'https://voxbuddy-backend.onrender.com/app',
    cleartext: false,
  },
  android: {
    allowMixedContent: false,
  },
};

export default config;
