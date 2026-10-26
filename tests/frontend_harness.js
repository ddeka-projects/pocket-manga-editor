// A small DOM/transport substitute; actual layout remains a browser/device check.
const nodes = new Map();
class Element {
  constructor() {
    this.hidden = false;
    this.disabled = false;
    this.textContent = "";
    this.children = [];
    const classes = new Set();
    this.classList = {
      add(...names) { names.forEach((name) => classes.add(name)); },
      remove(...names) { names.forEach((name) => classes.delete(name)); },
      toggle(name, force = !classes.has(name)) {
        if (force) classes.add(name); else classes.delete(name);
      },
    };
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  removeAttribute() {}
  setAttribute() {}
  addEventListener() {}
  focus() {}
}
function element(id) {
  if (!nodes.has(id)) nodes.set(id, new Element());
  return nodes.get(id);
}
globalThis.document = { getElementById: element, body: new Element(), createElement: () => new Element() };
let intervals = 0;
globalThis.window = {
  setTimeout() { return 1; }, clearTimeout() {},
  setInterval() { intervals += 1; },
  history: { pushState() {}, replaceState() {} }, location: { pathname: "/" },
};
globalThis.crypto = { randomUUID: () => "test-page-id" };
globalThis.AbortController = class { signal = {}; abort() {} };
globalThis.Headers = class {
  constructor() { this.values = new Map(); }
  set(name, value) { this.values.set(name.toLowerCase(), value); }
  get(name) { return this.values.get(name.toLowerCase()); }
};
let transport;
globalThis.fetch = (...args) => transport(...args);
function reply(body, status = 200) {
  return { status, ok: status < 400, headers: { get: () => "application/json" }, json: async () => body };
}
function assert(condition, message) {
  if (!condition) throw new Error(message);
}
