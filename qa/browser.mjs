// Run with Node 24 and Chrome debugging on 19223; uses only built-in APIs.
import { mkdir, writeFile } from "node:fs/promises";

const base = "http://127.0.0.1:18766";
const artifacts = new URL("./artifacts/", import.meta.url);
await mkdir(artifacts, { recursive: true });
const pages = await fetch("http://127.0.0.1:19223/json").then(r => r.json());
const socket = new WebSocket(pages.find(p => p.type === "page").webSocketDebuggerUrl);
await new Promise(resolve => socket.addEventListener("open", resolve, { once: true }));
let id = 0;
const pending = new Map();
const errors = [];
socket.addEventListener("message", ({ data }) => {
  const m = JSON.parse(data);
  if (m.id) { pending.get(m.id)?.(m); pending.delete(m.id); }
  if (m.method === "Runtime.exceptionThrown") errors.push(m.params.exceptionDetails);
});
const send = (method, params = {}) => new Promise(resolve => {
  const n = ++id; pending.set(n, resolve); socket.send(JSON.stringify({ id: n, method, params }));
});
async function evaluate(expression) {
  const r = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) throw Error(JSON.stringify(r.result.exceptionDetails));
  return r.result?.result?.value;
}
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function waitFor(expression, timeout = 18000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await evaluate(expression)) return true;
    await pause(250);
  }
  return false;
}
const results = [];
function check(name, actual, expected = true) {
  results.push({ name, passed: actual === expected, actual, expected });
}
async function shot(name) {
  const r = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  await writeFile(new URL(name + ".png", artifacts), Buffer.from(r.result.data, "base64"));
}
async function navigate(path) {
  await send("Page.navigate", { url: base + path }); await pause(450);
}
async function upload(name, content) {
  await evaluate(`(() => { const transfer = new DataTransfer(); transfer.items.add(new File([${JSON.stringify(content)}], ${JSON.stringify(name)})); const input = document.querySelector('[data-file-input]'); input.files = transfer.files; input.dispatchEvent(new Event('change')); })()`);
  await pause(650);
}
await send("Page.enable");
await send("Runtime.enable");
await send("Emulation.setEmulatedMedia", { features: [{name:"prefers-color-scheme",value:"light"}] });
await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
await navigate("/");
check("projects page rendered", await evaluate(`!!document.querySelector('[data-file-input]')`));
for (let tab = 0; tab < 4; tab++) {
  await send("Input.dispatchKeyEvent", {type:"keyDown",key:"Tab",code:"Tab",windowsVirtualKeyCode:9});
  await send("Input.dispatchKeyEvent", {type:"keyUp",key:"Tab",code:"Tab",windowsVirtualKeyCode:9});
}
check("file chooser reachable by keyboard", await evaluate(`document.activeElement.matches('[data-file-input]')`));
check("healthy engine notice hidden", await evaluate(`getComputedStyle(document.querySelector('#engine-notice')).display`), "none");
await upload("wrong.txt", "not a blend");
check("invalid file recovery visible", await evaluate(`document.querySelector('[data-intake]').innerText.includes('not a .blend')`));
check("upload stage state and label do not overlap", await evaluate(`[...document.querySelectorAll('.stage')].every(row => { const range=document.createRange(); range.selectNodeContents(row.children[0]); return range.getBoundingClientRect().right <= row.children[1].getBoundingClientRect().left; })`));
await shot("upload-error-desktop");
await upload("QA Sculpture.blend", "BLENDER-synthetic-input");
await pause(1700);
check("successful upload opens workspace link", await evaluate(`!!document.querySelector('[data-intake] a[data-nav]')`));
const projectPath = await evaluate(`document.querySelector('[data-intake] a[data-nav]').getAttribute('href')`);
await evaluate(`document.querySelector('[data-intake] a[data-nav]').click()`);
await pause(600);
check("workspace navigation", await evaluate(`location.pathname`), projectPath);
check("four rendered plates", await evaluate(`document.querySelectorAll('.plate img').length`), 4);
check("render images loaded", await evaluate(`[...document.querySelectorAll('.plate img')].every(x=>x.complete && x.naturalWidth>0)`));
check("revision protection visible", await evaluate(`document.querySelector('.rev-guard').innerText.includes('protected object')`));
await shot("workspace-desktop");
await evaluate(`document.querySelector('[aria-label="STL units"]').value='m'; document.querySelector('.ws-actions button').click()`);
check("STL download appears", await waitFor(`[...document.querySelectorAll('.job a')].some(a=>a.textContent.includes('Download STL') && a.textContent.includes(', m)'))`));
for (const theme of ["light", "dark"]) {
  await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: theme }, { name: "prefers-reduced-motion", value: "reduce" }] });
  for (const width of [320, 375, 768, 1024, 1440]) {
    await send("Emulation.setDeviceMetricsOverride", { width, height: 1000, deviceScaleFactor: 1, mobile: false });
    await navigate(projectPath);
    const w = await evaluate(`({viewport:innerWidth,document:document.documentElement.scrollWidth})`);
    check(`workspace overflow ${theme} ${width}`, w.document <= w.viewport);
    await shot(`workspace-${theme}-${width}`);
    await navigate("/");
    const p = await evaluate(`({viewport:innerWidth,document:document.documentElement.scrollWidth})`);
    check(`projects overflow ${theme} ${width}`, p.document <= p.viewport);
    await shot(`projects-${theme}-${width}`);
  }
}
await navigate("/");
await upload('A'.repeat(120)+'.blend', "BLENDER-synthetic-input");
await navigate("/");
check("long project filename wraps on desktop", await evaluate(`document.documentElement.scrollWidth <= innerWidth`));
await shot("projects-long-filename");
await send("Emulation.setDeviceMetricsOverride", { width: 375, height: 1000, deviceScaleFactor: 1, mobile: false });
check("long project filename wraps on mobile", await evaluate(`document.documentElement.scrollWidth <= innerWidth`));
await shot("projects-long-filename-mobile");
await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
await navigate(projectPath);
// Inject a bounded response fixture to exercise the UI's ninth-job behavior.
await evaluate(`(async () => {
  const original=api;
  const path='/api'+location.pathname;
  const fixture=await original(path);
  const completed=fixture.jobs.find(j=>j.kind==='render');
  fixture.jobs=Array.from({length:8},(_,i)=>({...completed,job_id:'queued_'+i,state:'queued'})).concat({...completed,job_id:'still_running',state:'running'});
  api=async (p,...rest)=>p===path ? fixture : original(p,...rest);
  route();
  await new Promise(r=>setTimeout(r,100));
  api=original;
})()`);
check("ninth active job visible in jobs list", await evaluate(`!!document.querySelector('.job [data-state="running"]')`));
await shot("hidden-active-job");
await navigate(projectPath);
// Real API request creates a synthetic spatial query through the production queue.
await evaluate(`(async () => { const d=await api('/api'+location.pathname); await api('/api'+location.pathname+'/inspect', {method:'POST',body:{revision_id:d.current_revision.revision_id,query:{type:'nearest',point:[0,0,0]}}}); })()`);
await pause(300);
await navigate(projectPath);
check("spatial query result visible", await evaluate(`document.querySelector('.report pre')?.textContent.includes('obj_original') || false`));
check("query does not break jobs and revisions", await evaluate(`!!document.querySelector('.jobs') && !!document.querySelector('.revs')`));
await shot("query-result-desktop");
await navigate("/projects/nonexistent");
check("missing project recovery", await evaluate(`document.querySelector('main').innerText.includes('Project not found')`));
await navigate("/");
// Project names must remain text, never HTML.
await upload('<img src=x onerror=alert(1)>.blend', "BLENDER-synthetic-input");
check("project filename not interpreted as markup", await evaluate(`document.querySelectorAll('.project-name img').length`), 0);
await send("Page.navigate", {url:"http://127.0.0.1:18765/"});
await pause(500);
check("real missing Blender recovery notice", await evaluate(`document.querySelector('#engine-notice').innerText.includes('Blender is unavailable')`));
await upload('missing-engine.blend', 'BLENDER-synthetic-input');
check("missing Blender upload reports recovery", await evaluate(`document.querySelector('[data-intake]').innerText.includes('SCULPT_BLENDER_PATH')`));
await shot('missing-blender');
check("no uncaught browser exceptions", errors.length, 0);
await writeFile(new URL("browser-results.json", artifacts), JSON.stringify({ syntheticBlender: true, results, errors }, null, 2));
console.log(JSON.stringify({ passed: results.filter(r=>r.passed).length, failed: results.filter(r=>!r.passed), errors }, null, 2));
socket.close();
process.exitCode = results.some(r => !r.passed) ? 1 : 0;
