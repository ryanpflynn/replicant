#!/usr/bin/env node
// Fixture MCP server (stdio, newline-delimited JSON-RPC) standing in for @acme/schema-mcp@1.2.0, which the billing-py
// sample repo's .mcp.json runs with npx. Environment images install it on the MCP shelf (/opt/mcp) so the
// auto-allowed server starts without a package registry. One tool: lookup_schema.
"use strict";
const readline = require("node:readline");

const TABLES = {
  invoices: ["invoice_id text", "customer_id text", "currency char(3)", "discount_percent numeric", "tax_rate numeric"],
  invoice_lines: ["invoice_id text", "description text", "unit_cents bigint", "quantity int"],
};

function reply(id, result) {
  process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id, result }) + "\n");
}

function fail(id, code, message) {
  process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id, error: { code, message } }) + "\n");
}

readline.createInterface({ input: process.stdin }).on("line", (line) => {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    return;
  }
  if (msg.id === undefined) return; // notifications
  switch (msg.method) {
    case "initialize":
      return reply(msg.id, {
        protocolVersion: (msg.params && msg.params.protocolVersion) || "2025-06-18",
        capabilities: { tools: {} },
        serverInfo: { name: "schema-mcp", version: "1.2.0" },
      });
    case "ping":
      return reply(msg.id, {});
    case "tools/list":
      return reply(msg.id, {
        tools: [{
          name: "lookup_schema",
          description: "Columns of a billing database table (invoices, invoice_lines).",
          inputSchema: { type: "object", properties: { table: { type: "string" } }, required: ["table"] },
        }],
      });
    case "tools/call": {
      const table = String((msg.params && msg.params.arguments && msg.params.arguments.table) || "");
      const cols = TABLES[table];
      return reply(msg.id, {
        content: [{ type: "text", text: cols ? `${table}: ${cols.join(", ")}` : `unknown table ${table}` }],
        isError: !cols,
      });
    }
    default:
      return fail(msg.id, -32601, `method not found: ${msg.method}`);
  }
});
