import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The build is served by server.py from ui/dist; `npm run dev` is only for
// working on the look, and has no API behind it.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true, assetsDir: "assets" },
});
