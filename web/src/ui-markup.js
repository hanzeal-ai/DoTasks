import { createElement } from "react";
import { createRoot } from "react-dom/client";
import { flushSync } from "react-dom";
import { Button } from "./components/ui/button";
import { Input } from "./components/ui/input";
import { Textarea } from "./components/ui/textarea";
import { NativeSelect } from "./components/ui/native-select";
import { Checkbox } from "./components/ui/checkbox";
import { Card } from "./components/ui/card";
import { Badge } from "./components/ui/badge";

import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from "./components/ui/table";

const tableElements = { table: Table, thead: TableHeader, tbody: TableBody, tr: TableRow, th: TableHead, td: TableCell };
const roots = new Map();
const booleanAttributes = new Set(["disabled", "hidden", "required", "multiple", "readonly", "checked", "autofocus"]);
const attributeNames = { class: "className", for: "htmlFor", tabindex: "tabIndex", readonly: "readOnly", autofocus: "autoFocus", maxlength: "maxLength", minlength: "minLength", colspan: "colSpan", rowspan: "rowSpan" };

// Adapt the existing escaped view templates to React primitives. Business actions
// stay in the controller's data attributes and delegated event handlers.
function viewNode(node, key) {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent;
  if (node.nodeType !== Node.ELEMENT_NODE) return null;
  const tag = node.localName;
  const props = { key };
  for (const { name, value } of node.attributes) {
    if (name === "style") {
      props.style = Object.fromEntries(Array.from(node.style).map(property => [
        property.startsWith("--") ? property : property.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()),
        node.style.getPropertyValue(property),
      ]));
    } else {
      const property = attributeNames[name] || (name.startsWith("data-") || name.startsWith("aria-")
        ? name : name.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()));
      props[property] = booleanAttributes.has(name) ? true : value;
    }
  }
  const children = Array.from(node.childNodes, viewNode);
  if (tag === "button") {
    props.variant = node.classList.contains("danger") ? "destructive" : node.classList.contains("primary") ? "default" : "outline";
    props.size = node.classList.contains("small") ? "sm" : "default";
    return createElement(Button, props, ...children);
  }
  if (tag === "input") {
    if (props.value !== undefined) { props.defaultValue = props.value; delete props.value; }
    if (props.checked !== undefined) { props.defaultChecked = props.checked; delete props.checked; }
    if (props.type === "checkbox") { delete props.type; return createElement(Checkbox, props); }
    return createElement(Input, props);
  }
  if (tag === "textarea") return createElement(Textarea, { ...props, defaultValue: node.textContent });
  if (tag === "select") return createElement(NativeSelect, props, ...children);
  if (tag === "span" && node.classList.contains("tag")) return createElement(Badge, props, ...children);
  if (tag === "article" && ["card", "requirement-card", "attention-task", "trace-item"].some(name => node.classList.contains(name))) {
    return createElement(Card, {key, asChild: true}, createElement(tag, props, ...children));
  }
  return createElement(tableElements[tag] || tag, props, ...children);
}

export function releaseContent(container) {
  if (!container) return;
  const descendants = [...roots.keys()].filter(element => element !== container && container.contains(element)).reverse();
  for (const element of [...descendants, container]) {
    if (roots.has(element)) { roots.get(element).unmount(); roots.delete(element); }
  }
}

export function setContent(selector, markup) {
  const container = document.querySelector(selector);
  releaseContent(container);
  const template = document.createElement("template");
  template.innerHTML = markup;
  const root = createRoot(container);
  roots.set(container, root);
  flushSync(() => root.render(Array.from(template.content.childNodes, viewNode)));
}
