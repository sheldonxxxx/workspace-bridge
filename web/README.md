# Workspace Bridge manager

React and TypeScript UI for the loopback administrator console. Vite builds into
`workspace_bridge/static/dist`; Starlette serves those assets and the existing API
on the same origin. shadcn/ui components are owned source files under
`src/components/ui`.

```sh
cd web
npm ci
npm run dev       # proxies /api to the local manager at 127.0.0.1:8766
npm run build     # refreshes the packaged static assets
npm run lint
npm run test:e2e # uses installed Chrome and mocked API responses
```

Do not put the admin token in frontend storage or source. The login form exchanges
it for an HttpOnly same-origin session cookie. The browser stores only the theme
preference. Show all untrusted run, transcript, and path data as text.

Production Docker builds the UI in a Node stage, then packages the result in the
Python wheel. The old plain-JavaScript manager has been removed.
