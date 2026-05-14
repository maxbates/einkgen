import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  // SPA is hosted at s3://<bucket>/web/ behind CloudFront; without this base
  // path, index.html references /assets/* which the CloudFront default
  // behavior can't resolve back to /web/assets/*.
  base: "/web/",
  plugins: [react()],
  server: {
    port: 5173,
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
