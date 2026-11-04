import * as vscode from "vscode";
import { RouterClient, ChatMessage, ToolDef } from "./client";
import { ClientConfiguration } from "./contextConfig";

/**
 * Registers hermes-router as a VS Code Language Model provider, so it appears in
 * Copilot Chat's model picker (and is usable by any vscode.lm consumer). The one
 * logical model "hermes-router" fans out across the configured provider pool, including
 * tool calling for agent mode.
 */
export class HermesChatModelProvider implements vscode.LanguageModelChatProvider {
  private changed = new vscode.EventEmitter<void>();
  readonly onDidChangeLanguageModelChatInformation = this.changed.event;
  private configuration?: ClientConfiguration;
  private pending?: { client: RouterClient; promise: Promise<ClientConfiguration> };
  private generation = 0;
  private disposed = false;

  constructor(private getClient: () => RouterClient) {}

  dispose(): void {
    this.disposed = true;
    this.generation++;
    this.changed.dispose();
  }

  invalidate(): void {
    this.generation++;
    this.pending = undefined;
    this.configuration = undefined;
    this.changed.fire();
  }

  async refreshConfiguration(): Promise<ClientConfiguration> {
    if (this.disposed) throw new Error("Hermes provider is disposed.");
    const client = this.getClient();
    if (this.pending?.client.sameConnection(client)) return this.pending.promise;
    const generation = ++this.generation;
    const current = () => !this.disposed && generation === this.generation && this.getClient().sameConnection(client);
    const promise = (async () => {
      try {
        const next = await client.getClientConfiguration();
        if (!current()) throw new Error("Router connection changed. Retry with the current router.");
        const changed = next.revision !== this.configuration?.revision;
        this.configuration = next;
        if (changed) this.changed.fire();
        return next;
      } catch (error) {
        if (current() && this.configuration) {
          this.configuration = undefined;
          this.changed.fire();
        }
        throw error;
      } finally {
        if (generation === this.generation) this.pending = undefined;
      }
    })();
    this.pending = { client, promise };
    return promise;
  }

  async provideLanguageModelChatInformation(
    _options: vscode.PrepareLanguageModelChatModelOptions,
    _token: vscode.CancellationToken
  ): Promise<vscode.LanguageModelChatInformation[]> {
    const config = await this.refreshConfiguration();
    if (_token.isCancellationRequested || !config.model) return [];
    const m = config.model;
    return [{
      id: m.id,
      name: "hermes-router (provider pool)",
      family: "hermes-router",
      version: "1.0.0",
      maxInputTokens: m.max_input_tokens,
      maxOutputTokens: m.max_output_tokens,
      tooltip: "Context limits synchronized with the router. Token counting uses a conservative UTF-8 byte estimate.",
      capabilities: { toolCalling: m.capabilities.tool_calling, imageInput: m.capabilities.image_input },
    }];
  }

  async provideLanguageModelChatResponse(
    _model: vscode.LanguageModelChatInformation,
    messages: readonly vscode.LanguageModelChatRequestMessage[],
    options: vscode.ProvideLanguageModelChatResponseOptions,
    progress: vscode.Progress<vscode.LanguageModelResponsePart>,
    token: vscode.CancellationToken
  ): Promise<void> {
    if (token.isCancellationRequested) return;
    const client = this.getClient();
    const config = await this.refreshConfiguration();
    if (token.isCancellationRequested) return;
    if (!this.getClient().sameConnection(client)) throw new Error("Router connection changed. Retry.");
    const model = config.model;
    if (!model) throw new Error(config.reason || "No eligible models have confirmed context limits.");
    if (_model.id !== model.id) throw new Error("Router model changed. Select the refreshed model and retry.");
    // The host may still hold old metadata for this conversation. Always use
    // fresh budgets below; a smaller limit only blocks a request that no longer fits.
    const oai: ChatMessage[] = messages.flatMap(toOpenAIMessages);

    let tools: ToolDef[] | undefined;
    let toolChoice: "auto" | "required" | undefined;
    if (options.tools && options.tools.length) {
      tools = options.tools.map((t) => ({
        type: "function" as const,
        function: {
          name: t.name,
          description: t.description || "",
          parameters: (t.inputSchema as object) || { type: "object", properties: {} },
        },
      }));
      toolChoice = options.toolMode === vscode.LanguageModelChatToolMode.Required ? "required" : "auto";
    }

    const input = { messages: oai, ...(tools ? { tools } : {}) };
    if (Buffer.byteLength(JSON.stringify(input), "utf8") > model.max_input_tokens) {
      throw new Error("Conversation exceeds the current router input budget. Start a shorter conversation.");
    }
    let cancellation: vscode.Disposable | undefined;
    try {
      await client.streamChat({
        model: model.id,
        maxOutputTokens: model.max_output_tokens,
        configRevision: config.revision,
        messages: oai,
        tools,
        toolChoice,
        onText: (delta) => progress.report(new vscode.LanguageModelTextPart(delta)),
        onToolCall: (callId, name, input) =>
          progress.report(new vscode.LanguageModelToolCallPart(callId, name, input)),
        onAbort: (cancel) => {
          cancellation = token.onCancellationRequested(cancel);
          if (token.isCancellationRequested) cancel();
        },
      });
    } finally {
      cancellation?.dispose();
    }
  }

  async provideTokenCount(
    _model: vscode.LanguageModelChatInformation,
    text: string | vscode.LanguageModelChatRequestMessage,
    _token: vscode.CancellationToken
  ): Promise<number> {
    const s = typeof text === "string" ? text : JSON.stringify(toOpenAIMessages(text));
    // Include Unicode, tool arguments/results and framing instead of silently
    // undercounting them with the old characters/4 approximation.
    return Buffer.byteLength(s, "utf8") + 32;
  }
}

/** Extract plain text from a tool result's content parts. */
function toolResultText(part: vscode.LanguageModelToolResultPart): string {
  const out: string[] = [];
  for (const c of (part.content as any[]) || []) {
    if (c instanceof vscode.LanguageModelTextPart) out.push(c.value);
    else if (typeof c === "string") out.push(c);
    else {
      try {
        out.push(JSON.stringify(c));
      } catch {
        /* skip non-serializable parts */
      }
    }
  }
  return out.join("");
}

/**
 * Translate one VS Code chat message into one OR MORE OpenAI messages:
 * - Assistant + tool-call parts → assistant message with `tool_calls`
 * - User + tool-result parts → one `tool` message per result (+ any user text)
 * - otherwise → a plain user/assistant text message
 */
function toOpenAIMessages(msg: vscode.LanguageModelChatRequestMessage): ChatMessage[] {
  const isAssistant = msg.role === vscode.LanguageModelChatMessageRole.Assistant;
  const parts = (msg.content as any[]) || [];
  const text = parts
    .filter((p) => p instanceof vscode.LanguageModelTextPart)
    .map((p: any) => p.value)
    .join("");
  const toolCalls = parts.filter((p) => p instanceof vscode.LanguageModelToolCallPart) as vscode.LanguageModelToolCallPart[];
  const toolResults = parts.filter((p) => p instanceof vscode.LanguageModelToolResultPart) as vscode.LanguageModelToolResultPart[];

  const out: ChatMessage[] = [];

  if (isAssistant) {
    if (toolCalls.length) {
      out.push({
        role: "assistant",
        content: text || null,
        tool_calls: toolCalls.map((tc) => ({
          id: tc.callId,
          type: "function",
          function: { name: tc.name, arguments: JSON.stringify(tc.input ?? {}) },
        })),
      });
    } else {
      out.push({ role: "assistant", content: text });
    }
    return out;
  }

  // User message: tool results become `tool` messages (must precede any user text
  // so they immediately follow the assistant tool-call message).
  for (const tr of toolResults) {
    out.push({ role: "tool", tool_call_id: tr.callId, content: toolResultText(tr) });
  }
  if (text || out.length === 0) {
    out.push({ role: "user", content: text });
  }
  return out;
}
