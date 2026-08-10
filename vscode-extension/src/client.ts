import * as http from "http";
import * as https from "https";
import { URL } from "url";

export interface ProviderStatus {
  available?: boolean;
  rating?: number;
  latency_ms?: number;
  model?: string;
  models?: string[];
  supports_tools?: boolean;
  reasoning?: boolean;
  cost_usd?: number;
  tokens?: number;
  keys?: { key_tail: string; status: string; ready_in: number; requests?: number }[];
  breaker?: { open?: boolean };
  stats?: { total_requests?: number; error_rate?: number; errors?: number; avg_latency_ms?: number };
}

export interface RouterStatus {
  providers: Record<string, ProviderStatus>;
  cache?: { enabled?: boolean; hit_rate?: number; size?: number; max_size?: number; semantic?: { enabled?: boolean; hits?: number } };
  rotation?: { mode?: string };
}

export interface Health {
  status: string;
  providers: string[];
}

function request(
  urlStr: string,
  method: "GET" | "POST",
  headers: Record<string, string>,
  body?: string,
  timeoutMs = 6000
): Promise<{ code: number; body: string }> {
  return new Promise((resolve, reject) => {
    let url: URL;
    try {
      url = new URL(urlStr);
    } catch (e) {
      return reject(new Error(`bad URL: ${urlStr}`));
    }
    const lib = url.protocol === "https:" ? https : http;
    const req = lib.request(
      url,
      { method, headers, timeout: timeoutMs },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => resolve({ code: res.statusCode || 0, body: data }));
      }
    );
    req.on("timeout", () => req.destroy(new Error("request timed out")));
    req.on("error", reject);
    if (body) req.write(body);
    req.end();
  });
}

function get(urlStr: string, headers: Record<string, string>, timeoutMs = 6000): Promise<{ code: number; body: string }> {
  return request(urlStr, "GET", headers, undefined, timeoutMs);
}

function post(urlStr: string, headers: Record<string, string>, body?: string, timeoutMs = 6000): Promise<{ code: number; body: string }> {
  return request(urlStr, "POST", headers, body, timeoutMs);
}

export class RouterClient {
  constructor(private baseUrl: string, private apiKey: string) {}

  private base(): string {
    // Every SDK example shows base_url = http://host:8319/v1, so that's what people
    // paste here — then we'd request /v1/v1/status and 404 on everything. Strip it.
    // ponytail: a router genuinely mounted under a path ending in /v1 would break;
    // no such deployment exists (the router owns /v1/* itself).
    return this.baseUrl.trim().replace(/\/+$/, "").replace(/\/v1$/, "");
  }

  /** URL of the browser monitoring dashboard served by the router. */
  dashboardUrl(): string {
    return `${this.base()}/dashboard`;
  }

  async getHealth(): Promise<Health> {
    const { code, body } = await get(`${this.base()}/health`, {});
    if (code !== 200) {
      throw new Error(`health HTTP ${code}`);
    }
    return JSON.parse(body) as Health;
  }

  async getStatus(): Promise<RouterStatus> {
    const { code, body } = await get(`${this.base()}/v1/status`, {
      Authorization: `Bearer ${this.apiKey}`,
    });
    if (code === 401) {
      throw new Error("unauthorized — check hermesRouter.apiKey (must match PROXY_API_KEYS)");
    }
    if (code !== 200) {
      throw new Error(`status HTTP ${code}`);
    }
    return JSON.parse(body) as RouterStatus;
  }

  async restart(): Promise<void> {
    const { code, body } = await post(`${this.base()}/v1/config/restart`, {
      Authorization: `Bearer ${this.apiKey}`,
    });
    if (code === 401) {
      throw new Error("unauthorized — check hermesRouter.apiKey");
    }
    if (code < 200 || code >= 300) {
      throw new Error(`restart HTTP ${code}: ${body.slice(0, 200)}`);
    }
  }

  /**
   * Stream a chat completion from the router. POSTs an OpenAI-format request with
   * stream:true to /v1/chat/completions, invoking onText() for each content delta
   * and (once accumulated) onToolCall() for each tool call. Resolves when the
   * stream ends; aborts cleanly when the caller's cancel fires.
   */
  streamChat(opts: StreamOpts): Promise<void> {
    return new Promise((resolve, reject) => {
      let url: URL;
      try {
        url = new URL(`${this.base()}/v1/chat/completions`);
      } catch {
        return reject(new Error(`bad URL: ${this.base()}`));
      }
      const lib = url.protocol === "https:" ? https : http;
      const body: any = { model: "hermes-router", messages: opts.messages, stream: true };
      if (opts.tools && opts.tools.length) {
        body.tools = opts.tools;
        body.tool_choice = opts.toolChoice || "auto";
      }
      const payload = JSON.stringify(body);

      // Accumulate streamed tool-call fragments by index: id + name arrive once,
      // arguments come in pieces across deltas.
      const toolAcc: Record<number, { id: string; name: string; args: string }> = {};
      const flushToolCalls = () => {
        if (!opts.onToolCall) return;
        for (const k of Object.keys(toolAcc)) {
          const tc = toolAcc[+k];
          if (!tc.name) continue;
          let input: object = {};
          try {
            input = tc.args ? JSON.parse(tc.args) : {};
          } catch {
            input = {};
          }
          opts.onToolCall(tc.id || `call_${k}`, tc.name, input);
        }
      };

      const req = lib.request(
        url,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Content-Length": Buffer.byteLength(payload),
            Authorization: `Bearer ${this.apiKey}`,
          },
          timeout: 120_000,
        },
        (res) => {
          if (res.statusCode === 401) {
            res.resume();
            return reject(new Error("unauthorized — check hermesRouter.apiKey"));
          }
          if ((res.statusCode || 0) < 200 || (res.statusCode || 0) >= 300) {
            let err = "";
            res.on("data", (c) => (err += c));
            res.on("end", () => reject(new Error(`router HTTP ${res.statusCode}: ${err.slice(0, 300)}`)));
            return;
          }
          let buf = "";
          res.setEncoding("utf-8");
          res.on("data", (chunk: string) => {
            buf += chunk;
            let nl: number;
            while ((nl = buf.indexOf("\n")) >= 0) {
              const line = buf.slice(0, nl).trim();
              buf = buf.slice(nl + 1);
              if (!line.startsWith("data:")) continue;
              const data = line.slice(5).trim();
              if (!data || data === "[DONE]") continue;
              try {
                const ev = JSON.parse(data);
                const delta = ev?.choices?.[0]?.delta;
                if (!delta) continue;
                if (typeof delta.content === "string" && delta.content) opts.onText(delta.content);
                if (Array.isArray(delta.tool_calls)) {
                  for (const tc of delta.tool_calls) {
                    const i = typeof tc.index === "number" ? tc.index : 0;
                    const acc = (toolAcc[i] = toolAcc[i] || { id: "", name: "", args: "" });
                    if (tc.id) acc.id = tc.id;
                    if (tc.function?.name) acc.name = tc.function.name;
                    if (tc.function?.arguments) acc.args += tc.function.arguments;
                  }
                }
              } catch {
                /* ignore keepalive / partial lines */
              }
            }
          });
          res.on("end", () => {
            flushToolCalls();
            resolve();
          });
          res.on("error", reject);
        }
      );
      req.on("timeout", () => req.destroy(new Error("request timed out")));
      let cancelled = false;
      req.on("error", (e: any) => {
        if (cancelled) resolve(); // aborted by user
        else reject(e);
      });
      opts.onAbort?.(() => {
        cancelled = true;
        req.destroy();
      });
      req.write(payload);
      req.end();
    });
  }
}

export interface ToolCall {
  id: string;
  type: "function";
  function: { name: string; arguments: string };
}

export interface ChatMessage {
  role: "system" | "user" | "assistant" | "tool";
  content: string | null;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
}

export interface ToolDef {
  type: "function";
  function: { name: string; description: string; parameters: object };
}

export interface StreamOpts {
  messages: ChatMessage[];
  tools?: ToolDef[];
  toolChoice?: "auto" | "required";
  onText: (delta: string) => void;
  onToolCall?: (callId: string, name: string, input: object) => void;
  onAbort?: (cancel: () => void) => void;
}
