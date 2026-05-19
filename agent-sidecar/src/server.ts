/**
 * Archimedes agent sidecar.
 *
 * A small WebSocket service that runs the OpenRouter Agent SDK's multi-step
 * tool-calling loop (callModel) on behalf of the Python bot. The bot speaks a
 * compact JSON protocol over one WebSocket per chat turn:
 *
 *   bot  -> sidecar : { type: "start", model, messages, tools, ... }
 *   sidecar -> bot  : { type: "delta", text }              streamed model text
 *   sidecar -> bot  : { type: "tool_call", call_id, name, arguments }
 *   bot  -> sidecar : { type: "tool_result", call_id, result }
 *   sidecar -> bot  : { type: "done", text, finish_reason, usage, tool_names }
 *   sidecar -> bot  : { type: "error", error }
 *
 * Tools live in the Python bot, so every tool the SDK invokes is bridged back
 * over the same socket: the SDK calls execute(), the sidecar emits a
 * tool_call, and the bot answers with a tool_result. That keeps the tool
 * registry, the execution pipeline and the Lua plugins exactly where they are.
 */
import { createServer } from 'node:http';
import { WebSocketServer, WebSocket, type RawData } from 'ws';
import {
  OpenRouter,
  tool,
  stepCountIs,
  maxCost,
  fromChatMessages,
} from '@openrouter/agent';
import { toZodObject } from './jsonschema.js';

const HOST = process.env.AGENT_SIDECAR_HOST || '127.0.0.1';
const PORT = Number(process.env.AGENT_SIDECAR_PORT || '8770');
const API_KEY = process.env.OPENROUTER_API_KEY || '';

interface StartMessage {
  type: 'start';
  model?: string | null;
  messages?: unknown[];
  temperature?: number | null;
  max_output_tokens?: number | null;
  tools?: Array<Record<string, any>>;
  max_steps?: number | null;
  max_cost?: number | null;
}

interface PendingToolCall {
  resolve: (value: unknown) => void;
  reject: (reason: unknown) => void;
}

function errorMessage(err: unknown): string {
  if (err instanceof Error) {
    return err.message || err.name;
  }
  return String(err);
}

/** One chat turn: drives callModel and bridges tool calls back to the bot. */
class Session {
  private readonly ws: WebSocket;
  private readonly client: OpenRouter;
  private readonly pending = new Map<string, PendingToolCall>();
  private readonly toolNames: string[] = [];
  private callCounter = 0;
  private started = false;

  constructor(ws: WebSocket, client: OpenRouter) {
    this.ws = ws;
    this.client = client;
    ws.on('message', (data) => this.onMessage(data));
    ws.on('close', () => this.failPending(new Error('connection closed')));
    ws.on('error', () => this.failPending(new Error('connection error')));
  }

  private send(payload: Record<string, unknown>): void {
    if (this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(payload));
    }
  }

  private failPending(reason: unknown): void {
    for (const entry of this.pending.values()) {
      entry.reject(reason);
    }
    this.pending.clear();
  }

  private onMessage(data: RawData): void {
    let msg: any;
    try {
      msg = JSON.parse(data.toString());
    } catch {
      return;
    }
    if (!msg || typeof msg !== 'object') {
      return;
    }
    if (msg.type === 'start') {
      if (this.started) {
        return;
      }
      this.started = true;
      this.run(msg as StartMessage).catch((err) => {
        this.send({ type: 'error', error: errorMessage(err) });
        this.failPending(err);
        this.ws.close();
      });
    } else if (msg.type === 'tool_result') {
      const entry = this.pending.get(String(msg.call_id));
      if (entry) {
        this.pending.delete(String(msg.call_id));
        entry.resolve(msg.result);
      }
    }
  }

  /** Bridge one SDK tool call back to the bot and await its result. */
  private runTool(name: string, args: unknown): Promise<unknown> {
    const callId = `t${++this.callCounter}`;
    this.toolNames.push(name);
    const promise = new Promise<unknown>((resolve, reject) => {
      this.pending.set(callId, { resolve, reject });
    });
    this.send({
      type: 'tool_call',
      call_id: callId,
      name,
      arguments: args ?? {},
    });
    return promise;
  }

  private buildTools(schemas: Array<Record<string, any>>): any[] {
    return schemas.map((entry) => {
      const fn = (entry.function ?? entry) as Record<string, any>;
      const name = String(fn.name || '');
      return tool({
        name,
        description: String(fn.description || ''),
        inputSchema: toZodObject(fn.parameters),
        execute: async (args: unknown) => this.runTool(name, args),
      });
    });
  }

  private async run(msg: StartMessage): Promise<void> {
    const messages = Array.isArray(msg.messages) ? msg.messages : [];
    const tools = this.buildTools(Array.isArray(msg.tools) ? msg.tools : []);

    const request: Record<string, unknown> = {
      input: fromChatMessages(messages as any),
    };
    if (msg.model) {
      request.model = msg.model;
    }
    if (typeof msg.temperature === 'number') {
      request.temperature = msg.temperature;
    }
    if (typeof msg.max_output_tokens === 'number') {
      request.maxOutputTokens = msg.max_output_tokens;
    }
    if (tools.length > 0) {
      const steps = Math.max(1, Math.trunc(msg.max_steps || 4));
      const stopWhen: unknown[] = [stepCountIs(steps)];
      if (typeof msg.max_cost === 'number' && msg.max_cost > 0) {
        stopWhen.push(maxCost(msg.max_cost));
      }
      request.tools = tools;
      request.stopWhen = stopWhen;
      request.allowFinalResponse = true;
    }

    const result = this.client.callModel(request as any);

    for await (const delta of result.getTextStream()) {
      if (delta) {
        this.send({ type: 'delta', text: delta });
      }
    }

    const text = await result.getText();
    let finishReason = '';
    let usage: Record<string, unknown> = {};
    try {
      const response: any = await result.getResponse();
      const raw = response?.usage ?? response ?? {};
      usage = {
        input_tokens: raw.inputTokens ?? raw.input_tokens ?? 0,
        output_tokens: raw.outputTokens ?? raw.output_tokens ?? 0,
        cost: raw.cost ?? 0,
      };
      const reason =
        response?.incompleteDetails?.reason ??
        response?.incomplete_details?.reason ??
        '';
      if (reason === 'max_output_tokens') {
        finishReason = 'length';
      }
    } catch {
      // Usage is best-effort; a missing response object is not fatal.
    }

    this.send({
      type: 'done',
      text,
      finish_reason: finishReason,
      usage,
      tool_names: this.toolNames,
    });
    this.ws.close();
  }
}

function main(): void {
  if (!API_KEY) {
    console.error('agent-sidecar: OPENROUTER_API_KEY is not set');
  }
  const client = new OpenRouter({ apiKey: API_KEY });

  const httpServer = createServer((req, res) => {
    if (req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'text/plain' });
      res.end('ok');
      return;
    }
    res.writeHead(404);
    res.end();
  });

  const wss = new WebSocketServer({ server: httpServer, path: '/agent' });
  wss.on('connection', (ws) => {
    new Session(ws, client);
  });

  httpServer.listen(PORT, HOST, () => {
    console.log(`agent-sidecar: listening on ws://${HOST}:${PORT}/agent`);
  });

  const shutdown = (): void => {
    wss.close();
    httpServer.close(() => process.exit(0));
  };
  process.on('SIGTERM', shutdown);
  process.on('SIGINT', shutdown);
}

main();
