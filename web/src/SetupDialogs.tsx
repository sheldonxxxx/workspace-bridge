import { useEffect, useState, type FormEvent } from "react";
import {
  api,
  runtimeName,
  type AdapterInfo,
  type NodeInfo,
  type NodeAdapterConfig,
  type PluginInfo,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

type Done = (message: string) => void;

type Pairing = { pairing: string; name: string; expires: string; note: string };

export type NodeDialogState = { mode: "pair" | "edit"; node?: NodeInfo };

/** Pair a Node, or rename, disable and test one. Nodes dial in; nothing here sets a URL or token. */
export function NodeDialog({
  dialog,
  onClose,
  onDone,
}: {
  dialog: NodeDialogState;
  onClose: () => void;
  onDone: Done;
}) {
  const [issued, setIssued] = useState<Pairing | null>(null);
  const [copied, setCopied] = useState(false);
  const [test, setTest] = useState("");
  const [error, setError] = useState("");
  const pairing = dialog.mode === "pair";

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setError("");
    try {
      if (pairing) {
        setIssued(
          await api<Pairing>("/api/nodes/pairings", "POST", {
            name: data.get("name"),
            replace: data.get("replace") === "on",
          }),
        );
        return;
      }
      await api(`/api/nodes/${dialog.node?.id}`, "PATCH", {
        name: data.get("name"),
        enabled: data.get("enabled") === "on",
      });
      onDone("Node updated.");
    } catch (failure) {
      setError((failure as Error).message);
    }
  }

  async function testConnection() {
    try {
      const result = await api<{
        success: boolean;
        message?: string;
        code?: string;
      }>(`/api/nodes/${dialog.node?.id}/test`, "POST");
      setTest(
        result.success
          ? "Connected: Node Protocol is ready."
          : `Connection failed: ${result.message || result.code || "Unavailable"}`,
      );
    } catch (failure) {
      setTest(`Connection failed: ${(failure as Error).message}`);
    }
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(issued?.pairing || "");
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (open) return;
        if (issued) onDone("Pairing string issued.");
        else onClose();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {pairing ? "Pair a Node" : `Edit ${dialog.node?.name}`}
          </DialogTitle>
          <DialogDescription>
            {pairing
              ? "The Node dials the Bridge with a one-time pairing string, so it needs no inbound port. Allowed roots are configured on the Node host."
              : "Allowed roots are configured on the Node host. Disabling a Node closes its connection until it is enabled again."}
          </DialogDescription>
        </DialogHeader>
        {issued ? (
          <div className="dialog-form">
            <div className="form-field">
              <Label htmlFor="pairing-string">Pairing string</Label>
              <Input
                id="pairing-string"
                readOnly
                value={issued.pairing}
                onFocus={(event) => event.currentTarget.select()}
              />
              <small>{issued.note}</small>
              <small>
                Expires {new Date(issued.expires).toLocaleString()}.
              </small>
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => void copy()}
              >
                {copied ? "Copied" : "Copy"}
              </Button>
              <Button
                type="button"
                onClick={() => onDone("Pairing string issued.")}
              >
                Done
              </Button>
            </DialogFooter>
          </div>
        ) : (
          <form className="dialog-form" onSubmit={submit}>
            <div className="form-field">
              <Label htmlFor="node-name">Name</Label>
              <Input
                id="node-name"
                name="name"
                defaultValue={dialog.node?.name || ""}
                required
                maxLength={80}
                placeholder="Laptop"
              />
            </div>
            {pairing ? (
              <label className="adapter-enabled-field">
                <input name="replace" type="checkbox" /> Re-pair the existing
                Node with this name (interrupts its active runs)
              </label>
            ) : (
              <label className="adapter-enabled-field">
                <input
                  name="enabled"
                  type="checkbox"
                  defaultChecked={dialog.node?.enabled ?? true}
                />{" "}
                Node enabled
              </label>
            )}
            {test && (
              <p className="adapter-test-result" role="status">
                {test}
              </p>
            )}
            {error && (
              <p className="adapter-test-result" role="alert">
                {error}
              </p>
            )}
            <DialogFooter>
              {!pairing && (
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => void testConnection()}
                >
                  Test connection
                </Button>
              )}
              <Button type="button" variant="outline" onClick={onClose}>
                Cancel
              </Button>
              <Button type="submit">
                {pairing ? "Create pairing string" : "Save Node"}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}

export type AdapterDialogState = {
  mode: "create" | "edit";
  adapter?: AdapterInfo;
  nodeId?: string;
};

/** Add or edit an agent the Node runs as a supervised plugin. */
export function AdapterDialog({
  dialog,
  nodes,
  onClose,
  onDone,
}: {
  dialog: AdapterDialogState;
  nodes: NodeInfo[];
  onClose: () => void;
  onDone: Done;
}) {
  const create = dialog.mode === "create";
  const [nodeId, setNodeId] = useState(
    dialog.adapter?.node_id || dialog.nodeId || nodes[0]?.id || "",
  );
  const [plugins, setPlugins] = useState<PluginInfo[]>([]);
  const [pluginId, setPluginId] = useState("");
  const [saved, setSaved] = useState<NodeAdapterConfig | null>(null);
  const [test, setTest] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (!nodeId) return;
    let live = true;
    void (async () => {
      try {
        const [catalog, config] = await Promise.all([
          api<{ plugins: PluginInfo[] }>(`/api/nodes/${nodeId}/plugins`),
          create
            ? Promise.resolve(null)
            : api<{ adapters: NodeAdapterConfig[] }>(
                `/api/nodes/${nodeId}/adapters`,
              ),
        ]);
        if (!live) return;
        setPlugins(catalog.plugins);
        const mine =
          config?.adapters.find((item) => item.id === dialog.adapter?.id) ??
          null;
        setSaved(mine);
        setPluginId(mine?.plugin || catalog.plugins[0]?.id || "");
        setError("");
      } catch (failure) {
        if (live) {
          setPlugins([]);
          setError((failure as Error).message);
        }
      }
    })();
    return () => {
      live = false;
    };
  }, [nodeId, create, dialog.adapter?.id]);

  const plugin = plugins.find((item) => item.id === pluginId);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const settings: Record<string, string> = {};
    for (const key of plugin?.settings || []) {
      const value = String(data.get(`setting:${key}`) || "").trim();
      if (value) settings[key] = value;
    }
    const projectsRoot = String(data.get("projects_root") || "").trim();
    const body = {
      name: data.get("name"),
      ...(projectsRoot ? { projects_root: projectsRoot } : {}),
      settings,
      enabled: data.get("enabled") === "on",
    };
    try {
      if (create)
        await api("/api/adapters", "POST", {
          ...body,
          node_id: nodeId,
          plugin: pluginId,
        });
      else await api(`/api/adapters/${dialog.adapter?.id}`, "PATCH", body);
      onDone(create ? "Adapter added." : "Adapter updated.");
    } catch (failure) {
      setError((failure as Error).message);
    }
  }

  async function testConnection() {
    try {
      const result = await api<{
        success: boolean;
        message?: string;
        code?: string;
        native_runtime?: string;
        adapter_version?: string;
        native_version?: string;
      }>("/api/adapters/test", "POST", { adapter_id: dialog.adapter?.id });
      setTest(
        result.success
          ? `Connected: ${result.native_runtime} · ${result.native_version || result.adapter_version || "version unavailable"}`
          : `Connection failed: ${result.message || result.code || "Unavailable"}`,
      );
    } catch (failure) {
      setTest(`Connection failed: ${(failure as Error).message}`);
    }
  }

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {create ? "Add adapter" : `Edit ${dialog.adapter?.name}`}
          </DialogTitle>
          <DialogDescription>
            The Node starts this agent as a supervised plugin and keeps its
            settings. Nothing here holds a URL or token.
          </DialogDescription>
        </DialogHeader>
        <form className="dialog-form" onSubmit={submit}>
          <div className="form-field">
            <Label htmlFor="adapter-node">Authoritative Node</Label>
            {create ? (
              <select
                id="adapter-node"
                className="native-select"
                value={nodeId}
                onChange={(event) => setNodeId(event.target.value)}
                required
              >
                <option value="">Choose a Node</option>
                {nodes.map((node) => (
                  <option key={node.id} value={node.id}>
                    {node.name} · {node.health || "unobserved"}
                  </option>
                ))}
              </select>
            ) : (
              <p className="form-readonly">
                {dialog.adapter?.node_name || "Node unknown"} · fixed for this
                adapter
              </p>
            )}
          </div>
          <div className="form-field">
            <Label htmlFor="adapter-name">Name</Label>
            <Input
              id="adapter-name"
              name="name"
              defaultValue={dialog.adapter?.name || ""}
              maxLength={80}
              required
            />
          </div>
          <div className="form-field">
            <Label htmlFor="adapter-plugin">Agent plugin</Label>
            {create ? (
              <select
                id="adapter-plugin"
                className="native-select"
                value={pluginId}
                onChange={(event) => setPluginId(event.target.value)}
                required
              >
                {plugins.map((item) => (
                  <option key={item.id} value={item.id}>
                    {runtimeName(item.runtime_type)} · {item.id}
                  </option>
                ))}
              </select>
            ) : (
              <p className="form-readonly">
                {runtimeName(dialog.adapter?.runtime_type || "pi")} ·{" "}
                {saved?.plugin || pluginId} · fixed for this adapter
              </p>
            )}
            {plugin?.description && <small>{plugin.description}</small>}
          </div>
          <div className="form-field">
            <Label htmlFor="adapter-root">Projects root</Label>
            <Input
              id="adapter-root"
              name="projects_root"
              key={saved?.id || "new"}
              defaultValue={saved?.projects_root || ""}
              placeholder="Blank uses the Node's default"
              maxLength={4096}
            />
          </div>
          {(plugin?.settings || []).map((key) => (
            <div className="form-field" key={`${pluginId}:${key}`}>
              <Label htmlFor={`adapter-setting-${key}`}>{key}</Label>
              <Input
                id={`adapter-setting-${key}`}
                name={`setting:${key}`}
                defaultValue={saved?.settings?.[key] || ""}
                placeholder="Plugin default"
                maxLength={1024}
              />
            </div>
          ))}
          <label className="adapter-enabled-field">
            <input
              name="enabled"
              type="checkbox"
              defaultChecked={dialog.adapter?.enabled ?? true}
            />{" "}
            Adapter enabled
          </label>
          {test && (
            <p className="adapter-test-result" role="status">
              {test}
            </p>
          )}
          {error && (
            <p className="adapter-test-result" role="alert">
              {error}
            </p>
          )}
          <DialogFooter>
            {!create && (
              <Button
                type="button"
                variant="outline"
                onClick={() => void testConnection()}
              >
                Test connection
              </Button>
            )}
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={create && !pluginId}>
              {create ? "Add adapter" : "Save adapter"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
