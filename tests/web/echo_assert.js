// Tiny assertion + runner shared by the echo_* tests. Exits non-zero on the first failure so CI fails loudly.
"use strict";
function ok(cond, msg) { if (!cond) throw new Error('expected truthy: ' + (msg || '')); }
function eq(a, b, msg) { const sa = JSON.stringify(a), sb = JSON.stringify(b); if (sa !== sb) throw new Error(`not equal${msg ? ' (' + msg + ')' : ''}:\n  got      ${sa}\n  expected ${sb}`); }
function near(a, b, tol, msg) { if (!(Math.abs(a - b) <= tol)) throw new Error(`not near${msg ? ' (' + msg + ')' : ''}: ${a} vs ${b} (±${tol})`); }

function run(name, body) {
  let pass = 0, fail = 0;
  const t = (desc, fn) => {
    const go = r => { if (r && typeof r.then === 'function') return r.then(() => { pass++; console.log('  ✓', desc); }, e => { fail++; console.error('  ✗', desc, '\n    ', e.message); }); pass++; console.log('  ✓', desc); };
    try { const r = fn(); if (r && typeof r.then === 'function') { pendings.push(go(r)); return; } pass++; console.log('  ✓', desc); }
    catch (e) { fail++; console.error('  ✗', desc, '\n    ', (e && e.message) || e); }
  };
  const pendings = [];
  console.log(name);
  body(t);
  Promise.all(pendings).then(() => {
    console.log(`${name}: ${pass} passed, ${fail} failed\n`);
    if (fail) process.exit(1);
  });
}
module.exports = { ok, eq, near, run };
