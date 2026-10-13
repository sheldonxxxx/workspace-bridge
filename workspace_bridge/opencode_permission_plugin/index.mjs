// Trusted, dependency-free OpenCode v2 plugin. A lost policy service fails closed.
import { readFileSync } from 'node:fs';

export default {
  id: 'workspace-bridge.permissions.v1',
  async setup(ctx) {
    const { policyUrl, credentialFile, generation } = ctx.options;
    const token = readFileSync(credentialFile, 'utf8').trim();
    async function check(path, body) {
      const response = await fetch(policyUrl + path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Policy-Token': token },
        body: JSON.stringify({ ...body, generation }),
      });
      if (!response.ok) throw new Error('Workspace Bridge permission guard unavailable');
      return response.json();
    }
    async function ancestry(sessionID) {
      const ids = [];
      for (let i = 0; sessionID && i < 32; i++) {
        if (ids.includes(sessionID)) throw new Error('Invalid session ancestry');
        ids.push(sessionID);
        const response = await ctx.session.get({ sessionID });
        sessionID = (response.data ?? response).parentID;
      }
      return ids;
    }
    await ctx.tool.hook('execute.before', async (event) => {
      const inventory = await ctx.tool.list();
      const definition = inventory.find((tool) => tool.id === event.tool);
      const answer = await check('/tool', {
        registeredExtension: Boolean(definition?.options?.namespace),
        sessions: await ancestry(event.sessionID), tool: event.tool,
        input: event.input, nativeId: event.id, directory: ctx.location.directory,
      });
      if (answer.effect !== 'allow') throw new Error('Denied by Workspace Bridge security profile');
    });
    await ctx.permission.hook('evaluate', async (event) => {
      const answer = await check('/permission', {
        sessions: await ancestry(event.sessionID), action: event.action,
        resources: event.resources, directory: ctx.location.directory,
      });
      // Never widen a native denial. Manual handoff approvals remain in the TUI.
      if (answer.effect === 'deny') event.effect = 'deny';
      else if (answer.effect === 'ask' && event.effect !== 'deny') event.effect = 'ask';
    });
    await ctx.shell.hook('create.before', (event) => {
      for (const name of Object.keys(event.env)) {
        if (name.startsWith('WB_') || name.startsWith('WORKSPACE_BRIDGE_') ||
            name === 'OPENCODE_PASSWORD' || name === 'OPENCODE_SERVER_PASSWORD') {
          delete event.env[name];
        }
      }
    });
    await check('/ready', { directory: ctx.location.directory });
  },
};
