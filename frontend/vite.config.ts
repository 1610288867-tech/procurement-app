import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    watch: {
      // 编辑器保存时常用"原子写"：先写临时文件再改名。AI 助手（以及不少编辑器）
      // 会在源码目录下留下形如 `.Login.tsx.<pid>.<uuid>.tmpdir/Login.tsx.tmp` 的
      // 瞬时目录。Vite 的文件监听器正好去 watch 这个瞬时文件，Windows 上直接抛
      //     Error: EBUSY: resource busy or locked, watch '...tmpdir\Login.tsx.tmp'
      // 而且它是 **未捕获的 error 事件 → 整个 dev server 进程退出**：
      // 现象是"改一次代码，页面就打不开了"，很容易误判成自己改坏了代码（已实测两次）。
      //
      // 这类路径对 Vite 毫无价值（既不是源码也不是依赖），直接不看即可。
      // 不用 usePolling：轮询会让整个项目的改动检测变慢，代价大于收益。
      ignored: ["**/.*.tmpdir/**", "**/*.tmp"],
    },
    proxy: {
      // ★ 用代理解决跨域，而不是靠 CORS。
      //
      // 前端仍然请求 /api/...，但由 Vite 转发到 8000，浏览器看到的始终是同源。
      // 好处：不需要 CORS 配置、不需要 credentials: "include"（Cookie 自动带）、
      //      M3 引入 httpOnly Cookie 时前端一行都不用改、
      //      M12 打包时也只需要换掉这个 target。
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
