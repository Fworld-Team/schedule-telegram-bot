const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const script = fs.readFileSync(0, "utf8").match(/<script>([\s\S]+)<\/script>/)[1];

function viewer(storage = new Map()) {
  const nodes = new Map();
  const events = new Map();
  let clock = Date.parse("2026-11-15T20:59:00Z");
  let tick;
  class Element {
    constructor() { this.children = []; }
    replaceChildren() { this.children = []; }
    appendChild(child) { this.children.push(child); }
    setAttribute() {}
  }
  const get = id => {
    if (!nodes.has(id)) nodes.set(id, new Element());
    return nodes.get(id);
  };
  const widget = { publicData: "", onEvent: (event, fn) => events.set(event, fn) };
  vm.runInNewContext(script, {
    window: { TelegramWidget: widget },
    TelegramWidget: widget,
    document: { getElementById: get, createElement: () => new Element() },
    Date: class extends Date { static now() { return clock; } },
    localStorage: { getItem: key => storage.get(key), setItem: (key, val) => storage.set(key, val) },
    setInterval: fn => { tick = fn; },
    fetch: () => { throw new Error("Widgets must render offline"); },
  });
  return {
    get, storage,
    tap: (id, index) => get(id).children[index].onclick(),
    monday: () => { clock = Date.parse("2026-11-15T21:00:00Z"); tick(); },
    apply: state => {
      widget.publicData = state ? JSON.stringify(state) : "";
      events.get("publicDataChanged")();
    },
  };
}

const a = viewer(), b = viewer();
assert.equal(a.get("kind").textContent, "Верхняя");
assert.equal(a.get("weeks").children.length, 2);
assert.equal(a.get("weeks").children[0].textContent, "09.11–15.11");
a.tap("groups", 1);
a.tap("days", 2);
assert.equal(a.get("name").textContent, "ДЭ 24-24");
assert.equal(a.get("date").textContent, "Среда, 11.11");
assert.equal(b.get("name").textContent, "ДЭ 23-24");
assert.equal(b.get("date").textContent, "Понедельник, 09.11");
assert.equal(viewer(a.storage).get("date").textContent, "Среда, 11.11");
a.tap("weeks", 1);
assert.equal(a.get("kind").textContent, "Нижняя");
assert.equal(a.get("date").textContent, "Понедельник, 16.11");

a.monday();
assert.equal(a.get("weeks").children[0].textContent, "16.11–22.11");
assert.equal(a.get("days").children[0].textContent, "Пн 16");
assert.equal(a.get("kind").textContent, "Нижняя");
const subject = () => a.get("lessons").children[0].children[1].children[0].textContent;
a.apply({ patches: [[1, 1, 0, 1, { subject: "Обновлено" }]] });
assert.equal(subject(), "Обновлено");
a.apply({ patches: [[1, 1, 0, 1, null]] });
assert.equal(a.get("lessons").children[0].textContent, "Пар нет");
a.apply(null);
assert.equal(subject(), "Базы данных");
