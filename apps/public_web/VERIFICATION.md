**B1 Web verification notes**

- `npm run typecheck` passes.
- `npm test -- --run` passes (6 tests) using the Windows-compatible native config loader and thread pool configured in `vite.config.ts`.
- `npm run build` passes and writes `dist/` (generated output; do not commit).
- In this restricted Windows host, Vite's default bundled config loader and Vitest's default fork pool fail with `spawn EPERM`; the project scripts/config avoid child-process config bundling and fork workers while retaining standard Vite/Vitest behavior.
