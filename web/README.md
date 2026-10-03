# Workspace Bridge manager

React and TypeScript UI for the loopback administrator console. Vite builds
into `workspace_bridge/static/dist`; Starlette serves those assets and the
API on the same origin. Component files under `src/components/ui` are owned
source.

```sh
cd web
npm ci
npm run dev       # proxies /api to the local manager at 127.0.0.1:8766
npm run build     # refreshes the packaged static assets
npm run lint
```

For runtime behavior see [Runtimes](../docs/RUNTIMES.md); for repo checks
see [Contributing](../CONTRIBUTING.md). Run `npm run build` after UI
changes so the packaged assets stay current.

Do not put the admin password in frontend storage or source. The login form
exchanges the username and password for an HttpOnly same-origin session cookie.
Temporary passwords must be changed before Manager access. Show all untrusted
run, transcript, and path data as text.
