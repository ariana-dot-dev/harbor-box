#!/usr/bin/env node
// From-scratch coding agent built on the Claude Agent SDK.
//
// It runs an autonomous agent loop (reason -> use tools -> observe) entirely
// against the local filesystem and shell of wherever it is executed. Harbor
// deploys this into an Ascii Box and runs it there, so "the local filesystem"
// is the Box. It never calls Box's built-in agent/prompt endpoint -- the whole
// brain is this program plus the Claude Agent SDK.
//
// Inputs (all overridable, sensible defaults):
//   argv[2]            instruction (falls back to $AGENT_INSTRUCTION)
//   $AGENT_CWD         working directory for the agent (default: process.cwd())
//   $AGENT_MODEL       model name/alias (default: "sonnet")
//   $AGENT_MAX_TURNS   max agentic turns (default: 40)
//   $AGENT_LOG         trajectory JSONL path (default: <cwd>/agent-trajectory.jsonl)
//   $ANTHROPIC_API_KEY or $CLAUDE_CODE_OAUTH_TOKEN — one is required by the SDK
//                      (API key, or a Claude subscription token from `claude setup-token`)
//
// Output: streams a compact trace to stdout and prints a final line
//   AGENT_RESULT <json> with {ok, subtype, num_turns, total_cost_usd, result}.
// Exit code is non-zero if the run did not end in a success result.

import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { query } from "@anthropic-ai/claude-agent-sdk";

const instruction = process.argv[2] ?? process.env.AGENT_INSTRUCTION;
if (!instruction || !instruction.trim()) {
  console.error("usage: node agent.mjs <instruction>  (or set $AGENT_INSTRUCTION)");
  process.exit(2);
}
if (!process.env.ANTHROPIC_API_KEY && !process.env.CLAUDE_CODE_OAUTH_TOKEN) {
  console.error("Set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN for the Claude Agent SDK.");
  process.exit(2);
}

const cwd = process.env.AGENT_CWD || process.cwd();
const model = process.env.AGENT_MODEL || "sonnet";
const maxTurns = Number(process.env.AGENT_MAX_TURNS || 40);
const logPath = process.env.AGENT_LOG || `${cwd}/agent-trajectory.jsonl`;
mkdirSync(dirname(logPath), { recursive: true });

function record(event) {
  try {
    appendFileSync(logPath, JSON.stringify(event) + "\n");
  } catch {
    /* logging must never crash the agent */
  }
}

const systemPrompt =
  "You are an autonomous software engineering agent working inside a fresh Linux sandbox. " +
  "Complete the user's task end to end by editing files and running shell commands in the " +
  "current working directory. Verify your own work by running the relevant command or test " +
  "before finishing. Be decisive and do not ask the user questions; you have no interactive user.";

console.log(`[agent] model=${model} cwd=${cwd} maxTurns=${maxTurns}`);
console.log(`[agent] task: ${instruction.replace(/\s+/g, " ").slice(0, 300)}`);

let finalResult = null;

try {
  for await (const message of query({
    prompt: instruction,
    options: {
      cwd,
      model,
      maxTurns,
      permissionMode: "bypassPermissions",
      allowDangerouslySkipPermissions: true,
      // Reproducible runs: ignore any ambient ~/.claude or project settings.
      settingSources: [],
      allowedTools: ["Bash", "Read", "Write", "Edit", "Glob", "Grep"],
      systemPrompt,
      stderr: (data) => process.stderr.write(data),
    },
  })) {
    record(message);

    if (message.type === "assistant") {
      for (const block of message.message.content) {
        if (block.type === "text" && block.text.trim()) {
          console.log(`[assistant] ${block.text.trim().slice(0, 500)}`);
        } else if (block.type === "tool_use") {
          const brief = JSON.stringify(block.input).slice(0, 200);
          console.log(`[tool] ${block.name} ${brief}`);
        }
      }
    } else if (message.type === "result") {
      finalResult = message;
    }
  }
} catch (err) {
  record({ type: "fatal", error: String(err) });
  console.error(`[agent] fatal: ${err?.stack || err}`);
  process.exit(1);
}

const ok = finalResult?.subtype === "success";
const summary = {
  ok,
  subtype: finalResult?.subtype ?? "no_result",
  num_turns: finalResult?.num_turns ?? null,
  total_cost_usd: finalResult?.total_cost_usd ?? null,
  result: finalResult?.subtype === "success" ? finalResult.result : (finalResult?.errors ?? null),
};
console.log("AGENT_RESULT " + JSON.stringify(summary));
process.exit(ok ? 0 : 1);
