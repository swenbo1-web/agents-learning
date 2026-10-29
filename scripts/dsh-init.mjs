#!/usr/bin/env node
import { resolve } from "node:path";
import { realpathSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { initialize } from "../host_resources/dsh/bootstrap.mjs";

export async function main(args = process.argv.slice(2)) {
  if (args.includes("--help")) {
    console.log("agent-learning-dsh-init --profile-directory <DSH_HOME/profiles/name> --workspace <absolute project path> [--python <absolute Python executable>]");
    return 0;
  }
  const values = {};
  for (let index = 0; index < args.length; index += 2) {
    const key = { "--profile-directory": "profileDirectory", "--workspace": "workspace", "--python": "python" }[args[index]];
    if (!key || !args[index + 1] || args[index + 1].startsWith("--") || key in values) throw new Error("Invalid initializer arguments; use --help");
    values[key] = args[index + 1];
  }
  if (!values.profileDirectory || !values.workspace) throw new Error("--profile-directory and --workspace are required; use --help");
  const result = await initialize(values);
  console.log(JSON.stringify(result));
  return 0;
}

let entry;
try { if (process.argv[1]) entry = pathToFileURL(realpathSync.native(resolve(process.argv[1]))).href; } catch {}
if (import.meta.url === entry) {
  try { process.exitCode = await main(); }
  catch (error) { console.error(JSON.stringify({ status: "error", reason: error.message })); process.exitCode = 1; }
}
