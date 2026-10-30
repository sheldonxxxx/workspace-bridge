// The security block the Bridge attaches to a conversation or run.
import { BUILTINS, profileRevision, validateRules } from "../policy.mjs";

export function securityBlock(rules = BUILTINS["read-only"], id = "read-only") {
  const canonical = validateRules(rules);
  return { source: "profile", profile: { id, revision: profileRevision(canonical), rules: canonical } };
}
