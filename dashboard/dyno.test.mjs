// Self-check for the Dyno 0-60 timer: pulls dynoStep() out of telemetry.js and runs a few launches.
// Run on the Pi:  node ~/caguas-sky/dashboard/dyno.test.mjs   (or anywhere: node dyno.test.mjs path/to/telemetry.js)
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";

const src = readFileSync(process.argv[2] ?? new URL("./src/telemetry.js", import.meta.url), "utf8");
const consts = src.match(/const DYNO_MAX_C = [^\n]+/)[0];
const fn = src.match(/function dynoStep\([\s\S]*?\n}\n/)[0];
const dynoStep = new Function(`${consts}\n${fn}\nreturn dynoStep;`)();

const run = (samples, s = { st: "wait", t0: 0, last: null }) => {
  for (const [ms, kmh, temp] of samples) s = dynoStep(s, ms, kmh, temp);
  return s;
};

// normal launch: stopped -> moving at t=1000 -> 60 mph at t=9500 => 8.5 s
let s = run([[0, 0, 70], [1000, 2, 70], [5000, 60, 70], [9500, 97, 70]]);
assert.equal(s.st, "done"); assert.equal(s.last, 8.5);

// hot CVT: never arms, even from a stop
assert.equal(run([[0, 0, 83], [1000, 5, 83], [9000, 100, 83]]).st, "locked");
// exactly 82 is still allowed, no temperature reading is not
assert.equal(run([[0, 0, 82]]).st, "ready");
assert.equal(run([[0, 0, null]]).st, "locked");

// rolling start does not count: moving without stopping first -> wait, never done
assert.equal(run([[0, 30, 70], [4000, 100, 70]]).st, "wait");

// CVT gets hot mid-run: aborts
assert.equal(run([[0, 0, 80], [1000, 5, 80], [3000, 40, 83]]).st, "locked");

// too slow (> 30 s): gives up
assert.equal(run([[0, 0, 70], [1000, 2, 70], [32000, 50, 70]]).st, "wait");

// stopping mid-run re-arms
assert.equal(run([[0, 0, 70], [1000, 5, 70], [3000, 0, 70]]).st, "ready");

console.log("dyno timer: all checks passed");
