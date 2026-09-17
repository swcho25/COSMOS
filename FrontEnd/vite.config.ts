import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

const root = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig(({ mode }) => {
  // .env 는 import.meta.env 로만 들어오고 process.env 에는 실리지 않는다 — 설정 파일에서는 loadEnv 로 직접 읽는다
  const env = loadEnv(mode, root, "VITE_");
  // /api 는 항상 실 백엔드로 넘긴다. 로컬 Spring 은 18081(application.yml SERVER_PORT 기본값), 배포 서버는 https://j15c205.p.ssafy.io
  const apiProxy = env.VITE_API_PROXY || "http://localhost:18081";

  return {
    plugins: [react()],
    resolve: {
      alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
    },
    server: {
      port: 5173,
      proxy: {
        "/api": { target: apiProxy, changeOrigin: true },
      },
    },
    build: {
      target: "es2022",
      chunkSizeWarningLimit: 1400,
      rollupOptions: {
        output: {
          manualChunks(id) {
            if (!id.includes("node_modules")) return undefined;
            if (/[\/](three|@react-three|postprocessing|maath|troika|three-stdlib)[\/]/.test(id)) return "three";
            return "vendor";
          },
        },
      },
    },
  };
});
