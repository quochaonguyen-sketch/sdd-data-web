import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

const folder = fileURLToPath(new URL("./", import.meta.url));
const [day, ...options] = process.argv.slice(2);
if (!/^\d{4}-\d{2}-\d{2}$/.test(day ?? "") || options.some(option => option !== "--local")) {
  throw new Error("Usage: node snapshot_jobs/archive-pickup-daily.mjs YYYY-MM-DD [--local]");
}
const raw = readFileSync(join(folder, "..", "data", "pickup-daily", `${day}.json`), "utf8");
const { report_date, observed_at, orders } = JSON.parse(raw);
const observed = new Date(observed_at);
if (report_date !== day || !Number.isFinite(observed.getTime()) || !Array.isArray(orders)
  || orders.some(row => typeof row.shipmentId !== "string" || !row.shipmentId.trim()
    || !["picked", "pending", "hold"].includes(row.status))
  || new Set(orders.map(row => row.shipmentId)).size !== orders.length) {
  throw new Error("Invalid Pickup daily snapshot");
}
const timestamp = observed.toISOString();
const chunks = gzipSync(raw).toString("base64").match(/.{1,60000}/g);
const sql = [
  `delete from pickup_daily_reports where report_date = '${day}' and observed_at < '${timestamp}';`,
  `insert into pickup_daily_reports values ('${day}', '${timestamp}', ${orders.length}, ${chunks.length}) on conflict do nothing;`,
  ...chunks.map((chunk, index) => `insert into pickup_daily_chunks
    select '${day}', ${index}, '${chunk}'
    where exists (select 1 from pickup_daily_reports where report_date = '${day}' and observed_at = '${timestamp}')
    on conflict do nothing;`),
].join("\n");

const scratch = mkdtempSync(join(tmpdir(), "sdd-pickup-daily-"));
try {
  const file = join(scratch, "upload.sql");
  writeFileSync(file, sql);
  const command = spawnSync(process.execPath, [
    join(folder, "node_modules", "wrangler", "bin", "wrangler.js"), "d1", "execute", "PICKUP_DAILY",
    "--config", join(folder, "wrangler.jsonc"), options.includes("--local") ? "--local" : "--remote", "--file", file, "--yes",
  ], { cwd: folder, stdio: "inherit" });
  if (command.error) throw command.error;
  if (command.status !== 0) throw new Error(`D1 import failed (exit ${command.status})`);
  console.log(`Archived Pickup ${day}: ${orders.length} orders in the source snapshot.`);
} finally {
  rmSync(join(scratch, "upload.sql"), { force: true });
  rmdirSync(scratch);
}
