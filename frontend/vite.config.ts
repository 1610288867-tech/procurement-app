import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      // ★ 用代理解决跨域，而不是靠 CORS。
      //
      // 前端仍然请求 /api/...，但由 Vite 转发到 8000，浏览器看到的始终是同源。
      // 好处：不需要 CORS 配置、不需要 credentials: "include"（Cookie 自动带）、
      //      M3 引入 httpOnly Cookie 时前端一行都不用改、
      //      M10 打包时也只需要换掉这个 target。
      //
      // 出错时的表现也更直观：代理层直接报 ECONNREFUSED，一眼看出是后端没起；
      // 而 CORS 错误会让人误以为后端有 bug。
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },

      // 健康检查在根路径下（这是通用约定，不改它去迁就代理），
      // 所以这里要单独转发一次——否则前端调 /health/ready 会 404。
      "/health": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
});
