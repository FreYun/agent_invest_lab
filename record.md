
##  工具的刷新

Rust 后端：有自动刷新，也有手动刷新。
自动刷新：
Rust server 会按 hot_reload.poll_interval_ms 轮询配置相关文件变化，默认最小 1000ms；发现变更后 debounce，然后调用 reload，刷新 config + MCP tools。相关逻辑在 server.rs (line 246)。
手动刷新：
前端输入 /reload 会主动发 reload_config 给 Rust server，强制刷新 MCP registry。相关逻辑在 tui.tsx (line 1033) 和 rustRunner.ts (line 436)。
TS 前端/TS runner：主要是手动刷新。
TS 模式下 /reload 会重新 ensureMcpDiscovered(config)，然后重建 ChatSession。相关逻辑在 runner.ts (line 410)。
一句话：你用 Rust 后端时，新工具可以自动热加载；想立即确认或强制刷新，就用 /reload。TS 纯前端 runner 则主要靠 /reload。