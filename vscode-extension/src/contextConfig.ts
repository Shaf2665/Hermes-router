export interface ClientConfiguration {
  schema_version: 1;
  revision: string;
  context_window: number;
  reason?: string;
  model: {
    id: string;
    context_window: number;
    max_input_tokens: number;
    max_output_tokens: number;
    capabilities: { tool_calling: boolean; image_input: boolean };
  } | null;
}

/** Reject incomplete or untrusted metadata instead of inventing a larger limit. */
export function parseClientConfiguration(value: unknown): ClientConfiguration {
  const d = value as ClientConfiguration;
  const count = (n: unknown, zero = false) => Number.isSafeInteger(n) &&
    (n as number) >= (zero ? 0 : 1) && (n as number) <= 16_000_000;
  if (!d || d.schema_version !== 1 || typeof d.revision !== "string" ||
      !/^[a-f0-9]{64}$/.test(d.revision) || !count(d.context_window, true)) {
    throw new Error("Router returned invalid context settings. Update the router and retry.");
  }
  const m = d.model;
  if (m !== null && (!m || typeof m.id !== "string" || !m.id || m.id.length > 256 ||
      !count(m.context_window) || !count(m.max_input_tokens) || !count(m.max_output_tokens) ||
      m.max_input_tokens + m.max_output_tokens !== m.context_window ||
      typeof m.capabilities?.tool_calling !== "boolean" || typeof m.capabilities?.image_input !== "boolean")) {
    throw new Error("Router returned invalid model limits.");
  }
  return d;
}
