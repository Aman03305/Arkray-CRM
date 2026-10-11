import react from "@vitejs/plugin-react";
import tsconfigPaths from "vite-tsconfig-paths";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [tsconfigPaths(), react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
    // One jsdom worker per logical CPU (Vitest's default, less one) oversubscribed the machine:
    // on a 16-core/24-thread laptop the slowest form test took 3.6 s alone and over 20 s in the
    // full run (final hardening: two timeouts). Half the CPUs ran the suite faster (69 s vs
    // 91 s) with the slowest test at 10 s. The per-test timeout is unchanged.
    maxWorkers: "50%",
    testTimeout: 20_000,
    unstubGlobals: true,
  },
});
