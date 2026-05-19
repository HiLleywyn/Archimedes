/**
 * Archimedes agent sidecar.
 *
 * A small WebSocket service that runs the OpenRouter Agent SDK's multi-step
 * tool-calling loop (callModel) on behalf of the Python bot. The bot speaks a
 * compact JSON protocol over one WebSocket per chat turn:
 *
 *   sidecar -> bot  : { type: "hello", protocol_version, sdk_version }
 *   bot  -> sidecar : { type: "start", protocol_version, turn_id, model, ... }
 *   sidecar -> bot  : { type: "delta", text }              streamed model text
 *   sidecar -> bot  : { type: "tool_call", call_id, name, arguments }
 *   bot  -> sidecar : { type: "tool_result", call_id, result }
 *   sidecar -> bot  : { type: "done", text, finish_reason, usage, tool_names }
 *   sidecar -> bot  : { type: "error", error }
 *
 * Every connection opens with a hello frame, so the two halves can confirm
 * they speak the same wire-protocol version before a turn is committed; a
 * mismatch fails the turn over to the bot's in-process loop.
 *
 * Tools live in the Python bot, so every tool the SDK invokes is bridged back
 * over the same socket: the SDK calls execute(), the sidecar emits a
 * tool_call, and the bot answers with a tool_result. That keeps the tool
 * registry, the execution pipeline and the Lua plugins exactly where they are.
 */
import { createServer } from 'node:http';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
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

/**
 * Wire-protocol version. Bump this whenever a message shape changes. The bot
 * sends its own version in the start frame and receives this one in the hello
 * frame; either side treats a mismatch as a reason to fall back rather than
 * risk talking past a half-deployed peer.
 */
const PROTOCOL_VERSION = 1;

/** Best-effort version of the bundled Agent SDK, surfaced for logs only. */
function readSdkVersion(): string {
  try {
    const here = dirname(fileURLToPath(import.meta.url));
    const pkgPath = join(
      here, '..', 'node_modules', '@openrouter', 'agent', 'package.json',
    );
    const pkg = JSON.parse(readFileSync(pkgPath, 'utf8')) as {
      version?: unknown;
    };
    if (typeof pkg.version === 'string' && pkg.version) {
      return pkg.version;
    }
  } catch {
    // The SDK version is a log field, not a gate -- 'unknown' is acceptable.
  }
  return 'unknown';
}

const SDK_VERSION = readSdkVersion();

interface StartMessage {
  type: 'start';
  protocol_version?: number | null;
  turn_id?: string | null;
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

/**
 * One chat turn: drives callModel and bridges tool calls back to the bot.
 *
 * The sidecar is deliberately stateless per turn -- it opens one WebSocket,
 * runs one turn and closes. Conversation history, memory and traits all live
 * in the Python bot. The Agent SDK also offers a stateful mode (a persistent
 * turn-state accessor and approval-gated tool pausing); the bridge does NOT
 * use it, because that would split one turn's state across two runtimes.
 * Anything stateful belongs on the Python side. A build guard
 * (tests/test_sidecar_guards.py) fails CI if that surface appears here -- if a
 * future change genuinely needs it, that guard must be revisited deliberately.
 */
class Session {
  private readonly ws: WebSocket;
  private readonly client: OpenRouter;
  private readonly pending = new Map<string, PendingToolCall>();
  private readonly toolNames: string[] = [];
  private callCounter = 0;
  private started = false;
  private turnId = '-';

  constructor(ws: WebSocket, client: OpenRouter) {
    this.ws = ws;
    this.client = client;
    ws.on('message', (data) => this.onMessage(data));
    ws.on('close', () => this.failPending(new Error('connection closed')));
    ws.on('error', () => this.failPending(new Error('connection error')));
    // Greet every connection so the bot can verify the protocol version
    // before it commits a turn to this sidecar.
    this.send({
      type: 'hello',
      protocol_version: PROTOCOL_VERSION,
      sdk_version: SDK_VERSION,
    });
  }

  private send(payload: Record<string, unknown>): void {
    if (this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(payload));
    }
  }

  /** Log a line tagged with this turn's correlation id. */
  private log(text: string): void {
    console.log(`[turn ${this.turnId}] ${text}`);
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
      if (typeof msg.turn_id === 'string' && msg.turn_id) {
        this.turnId = msg.turn_id;
      }
      const peer = msg.protocol_version;
      if (typeof peer === 'number' && peer !== PROTOCOL_VERSION) {
        this.log(
          `protocol mismatch: bot v${peer}, sidecar v${PROTOCOL_VERSION}`,
        );
        this.send({
          type: 'error',
          error:
            `protocol version mismatch (sidecar v${PROTOCOL_VERSION}, ` +
            `bot v${peer})`,
        });
        this.ws.close();
        return;
      }
      this.run(msg as StartMessage).catch((err) => {
        this.log(`error: ${errorMessage(err)}`);
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
    this.log(`start model=${msg.model || 'default'} tools=${tools.length}`);

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
    let modelUsed = '';
    let usage: Record<string, unknown> = {};
    try {
      const response: any = await result.getResponse();
      const raw = response?.usage ?? response ?? {};
      usage = {
        input_tokens: raw.inputTokens ?? raw.input_tokens ?? 0,
        output_tokens: raw.outputTokens ?? raw.output_tokens ?? 0,
        cost: raw.cost ?? 0,
      };
      modelUsed = String(response?.model ?? '');
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

    this.log(
      `done tools=${this.toolNames.length} ` +
      `finish=${finishReason || 'stop'} text_len=${text.length}`,
    );
    this.send({
      type: 'done',
      text,
      finish_reason: finishReason,
      model: modelUsed,
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
    console.log(
      `agent-sidecar: listening on ws://${HOST}:${PORT}/agent ` +
      `(protocol v${PROTOCOL_VERSION}, sdk ${SDK_VERSION})`,
    );
  });

  const shutdown = (): void => {
    wss.close();
    httpServer.close(() => process.exit(0));
  };
  process.on('SIGTERM', shutdown);
  process.on('SIGINT', shutdown);
}

main();
