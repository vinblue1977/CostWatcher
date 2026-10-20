import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The CDK stack deploys site/dist and adds config.json beside it. In dev, serve a copy of the deployed
// config.json from site/public (not committed) and run the stack with -c devOrigin=true.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", sourcemap: false },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
