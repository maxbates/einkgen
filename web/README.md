# einkgen web

Read-only dashboard for the einkgen pipeline. React + Vite + TypeScript, vanilla CSS, no UI library, no router, no state library.

Three tabs: **Queue**, **History**, **Device**.

## Configure

Copy `.env.example` to `.env.local` and edit for your environment:

```
VITE_READ_API_URL=http://localhost:3001
VITE_CDN_BASE=http://localhost:3001/cdn
```

Both are baked at build time.

## Develop

```
npm install
npm run dev
```

The dev server runs at <http://localhost:5173>.

## Build

```
npm run build
```

Output goes to `dist/`. Sync that to the S3 `web/` prefix and invalidate CloudFront.

## Test

```
npm run test
```

Vitest unit tests on the pure helpers in `src/format.ts`.

## Typecheck

```
npm run typecheck
```
