# VoiceCRM frontend

React 18 + TypeScript (strict) + Vite + Tailwind 3. All data shown is synthetic.

```powershell
cd frontend
npm install
npm run dev          # http://localhost:5173, proxies /api to the API on 127.0.0.1:8010
npm run dev:mock     # same UI with canned data in the browser: no backend needed
npm run typecheck
npm run build        # tsc -b && vite build -> dist/ (the API serves it when VOICECRM_SPA_DIR points here)
npm run preview      # serves dist/ on :4173 with the same /api proxy
```

- The API client (`src/api/client.ts`) uses `baseURL: '/api/v1'`. Set `VITE_API_PROXY` to point the dev/preview proxy somewhere other than `http://127.0.0.1:8010`.
- Mock mode is `VITE_USE_MOCK=1` (`.env.mock`, used by `dev:mock`). An axios adapter in `src/api/mock/` serves the canned data and keeps state in memory, so approve / reject / reprocess / simulate / policy changes behave like the real API until you reload. It has its own scripted calls; against the real API the fixture list comes from `GET /fixtures`.
- `src/api/types.ts` mirrors `backend/voicecrm/api/schemas.py` and `backend/voicecrm/domain/*.py` (snake_case on the wire).
- The Capture page is an installable PWA (`public/manifest.webmanifest`, hand-written `public/sw.js`, registered in production builds only). Recordings made offline wait in an IndexedDB queue and upload when the connection returns.
- `public/architecture.png` is the diagram shown on the Architecture page (copy of `docs/diagrams/voicecrm-architecture-v2.png`).
