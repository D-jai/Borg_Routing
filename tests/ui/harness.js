// Node harness: loads ui/assets/app.js into a vm context with a minimal fake DOM.
// Used only by tests/ui/test_app_js.py. No npm packages.
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..', 'ui', 'assets');

class FakeEl {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.attrs = {};
    this.children = [];
    this.className = '';
    this.textContent = '';
    this.classList = { add() {}, remove() {}, contains() { return false; } };
    this.handlers = {};
    this.parentNode = null;
    this.value = '';
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  insertBefore(c) { this.children.unshift(c); c.parentNode = this; return c; }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentNode = null; return c; }
  addEventListener(ev, fn) { (this.handlers[ev] = this.handlers[ev] || []).push(fn); }
  click() { (this.handlers.click || []).forEach((f) => f()); }
  get firstChild() { return this.children[0] || null; }
}

function load(opts) {
  opts = opts || {};
  const doc = {
    readyState: 'loading', // keeps app.js from building the shell; helpers still work
    addEventListener() {},
    createElement: (t) => new FakeEl(t),
    getElementById: (id) => (doc.body.children.find((c) => c.attrs.id === id) || null),
    documentElement: new FakeEl('html'),
    body: new FakeEl('body'),
  };
  const origin = opts.origin || 'http://127.0.0.1:8000';
  const store = () => ({
    _d: {},
    getItem(k) { return k in this._d ? this._d[k] : null; },
    setItem(k, v) { this._d[k] = String(v); },
  });
  const thrower = () => ({
    getItem() { throw new Error('storage blocked'); },
    setItem() { throw new Error('storage blocked'); },
  });
  const sandbox = {
    console, URL, Blob, setTimeout, JSON, Math, Date, Promise, Object, Array, String, Number, RegExp, Error, isFinite, isNaN,
    document: doc,
    location: { href: origin + '/ui/ask.html', origin: origin, pathname: '/ui/ask.html' },
    localStorage: opts.throwingStorage ? thrower() : store(),
    sessionStorage: opts.throwingStorage ? thrower() : store(),
    fetch: opts.fetch,
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  const ctx = vm.createContext(sandbox);
  for (const f of opts.vendor || []) {
    vm.runInContext(fs.readFileSync(path.join(ROOT, 'vendor', f), 'utf8'), ctx, { filename: f });
  }
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'app.js'), 'utf8'), ctx, { filename: 'app.js' });
  return { Borg: sandbox.Borg, window: sandbox, doc };
}

module.exports = { load, FakeEl };
