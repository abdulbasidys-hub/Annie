import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// No dev proxy is configured on purpose.
//
// A proxy would let the app reach the API through a same-origin path, which
// means VITE_API_BASE_URL could go missing without anything breaking locally —
// and then fail in the deployed build. Keeping the app pointed at an absolute
// injected URL in every environment means a misconfiguration surfaces on the
// first run rather than the first deploy.
export default defineConfig({
  plugins: [react()],
  server: {
    // Not 5173. That is Vite's default, so every Vite project on the machine
    // claims it, and the loser silently binds a different interface — you then
    // open localhost:5173 and reach whichever project won, with no error
    // anywhere to explain why you are looking at the wrong app.
    port: 5180,

    // Fail rather than silently incrementing to 5181. A dev server that moves
    // its own URL breaks the API's CORS_ORIGINS allowlist, and the resulting
    // "can't reach the API" gives no hint that the port changed.
    strictPort: true,

    // Bind by name, not by IP. Windows resolves `localhost` to ::1 first, so a
    // server bound only to 127.0.0.1 is unreachable by name even while it is
    // running and holding the port — which reads as "the server is down".
    //
    // In practice this resolves to ::1 alone, so reach it as `localhost:5180`.
    // `http://127.0.0.1:5180` will refuse the connection. Left localhost-only
    // rather than 0.0.0.0 so the dev server is not exposed to the network.
    host: 'localhost',
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
})
