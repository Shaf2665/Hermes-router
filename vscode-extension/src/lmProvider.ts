import * as vscode from "vscode";
import { RouterClient, ChatMessage, ToolDef } from "./client";

/**
 * Registers hermes-router as a VS Code Language Model provider, so it appears in
 * Copilot Chat's model picker (and is usable by any vscode.lm consumer). The one
 * logical model "hermes-router" fans out across the configured provider pool, including
 * tool calling for agent mode.
 */
export class HermesChatModelProvider implements vscode.LanguageModelChatProvider {
  constructor(private getClient: () => RouterClient) {}

  async provideLanguageModelChatInformation(
    _options: vscode.PrepareLanguageModelChatModelOptions,
    _token: vscode.CancellationToken
  ): Promise<vscode.LanguageModelChatInformation[]> {
    return [
      {
        id: "hermes-router",
        name: "hermes-router (provider pool)",
        family: "hermes-router",
        version: "1.0.0",
        maxInputTokens: 32000,
        maxOutputTokens: 8192,
        capabilities: { toolCalling: true, imageInput: false },
      },
    ];
  }

  async provideLanguageModelChatResponse(
    _model: vscode.LanguageModelChatInformation,
    messages: readonly vscode.LanguageModelChatRequestMessage[],
    options: vscode.ProvideLanguageModelChatResponseOptions,
    progress: vscode.Progress<vscode.LanguageModelResponsePart>,
    token: vscode.CancellationToken
  ): Promise<void> {
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

    await this.getClient().streamChat({
      messages: oai,
      tools,
      toolChoice,
      onText: (delta) => progress.report(new vscode.LanguageModelTextPart(delta)),
      onToolCall: (callId, name, input) =>
        progress.report(new vscode.LanguageModelToolCallPart(callId, name, input)),
      onAbort: (cancel) => token.onCancellationRequested(() => cancel()),
    });
  }

  async provideTokenCount(
    _model: vscode.LanguageModelChatInformation,
    text: string | vscode.LanguageModelChatRequestMessage,
    _token: vscode.CancellationToken
  ): Promise<number> {
    const s = typeof text === "string" ? text : messageText(text);
    return Math.ceil(s.length / 4); // cheap estimate (matches the router's char/4 fallback)
  }
}

/** Concatenate the text parts of a VS Code chat message. */
function messageText(msg: vscode.LanguageModelChatRequestMessage): string {
  return (msg.content || [])
    .map((p: any) => (p instanceof vscode.LanguageModelTextPart ? p.value : ""))
    .join("");
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
