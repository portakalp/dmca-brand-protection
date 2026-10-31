// The form and link inventory of one frame (architecture.md §10, stage 2). Evaluated by
// scanning/capture.py in the main frame and in child frames; it only reads the DOM (no clicks,
// no navigation). Python validates and classifies the result (form_patterns.yaml), so this
// collects raw attributes, each cut to the limits it is given. Attributes are read with
// getAttribute: a form input named "action" or "method" shadows form.action and form.method.
(limits) => {
  const cut = (value, max) => (value == null ? "" : String(value)).replace(/\s+/g, " ").trim().slice(0, max);
  const label = (value) => cut(value, limits.label);
  const short = (value) => cut(value, 40);
  const absolute = (value) => {
    if (value == null) return null;
    const raw = String(value).trim();
    if (!raw || raw.length > limits.url) return null;
    try {
      const url = new URL(raw, document.baseURI);
      return (url.protocol === "http:" || url.protocol === "https:") && url.href.length <= limits.url
        ? url.href : null;
    } catch (error) {
      return null;
    }
  };
  const visible = (element) => {
    if (!element.getClientRects().length) return false;
    const style = getComputedStyle(element);
    return style.visibility !== "hidden" && style.display !== "none" && style.opacity !== "0";
  };
  const textOf = (element) => label(
    element.innerText || element.value || element.getAttribute("aria-label")
      || element.getAttribute("title") || element.getAttribute("alt") || ""
  );
  const labelOf = (element) => {
    let text = element.labels && element.labels.length ? element.labels[0].innerText : "";
    if (!text) text = element.getAttribute("aria-label") || "";
    if (!text) {
      const ids = (element.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean);
      text = ids.map((id) => (document.getElementById(id) || {}).innerText || "").join(" ");
    }
    return label(text || element.getAttribute("title") || "");
  };
  const ONCLICK = /(?:location(?:\.href)?\s*=|location\.(?:assign|replace)\(|open\()\s*['"`]([^'"`]{1,2048})['"`]/i;
  const scriptTarget = (element) => {
    const data = element.getAttribute("data-href") || element.getAttribute("data-url")
      || element.getAttribute("data-link");
    if (data) return { url: absolute(data), tag: "data-href" };
    const found = ONCLICK.exec(element.getAttribute("onclick") || "");
    return found ? { url: absolute(found[1]), tag: "onclick" } : null;
  };
  const targetOf = (element) => {
    const tag = element.tagName;
    if ((tag === "A" || tag === "AREA") && element.hasAttribute("href")) {
      const href = absolute(element.getAttribute("href"));
      if (href) return href;
    }
    if (element.hasAttribute("formaction")) return absolute(element.getAttribute("formaction"));
    const script = scriptTarget(element);
    return script ? script.url : null;
  };

  const SKIPPED = new Set(["hidden", "submit", "button", "image", "reset"]);
  const isInput = (element) => (element.tagName === "INPUT" && !SKIPPED.has(element.type))
    || element.tagName === "SELECT" || element.tagName === "TEXTAREA";
  const isButton = (element) => element.tagName === "BUTTON"
    || (element.tagName === "INPUT" && ["submit", "button", "image"].includes(element.type));
  const inputInfo = (element) => ({
    type: short(element.tagName === "INPUT" ? element.type : element.tagName.toLowerCase()),
    name: label(element.getAttribute("name")),
    id: label(element.getAttribute("id")),
    placeholder: label(element.getAttribute("placeholder")),
    autocomplete: short(element.getAttribute("autocomplete")),
    inputmode: short(element.getAttribute("inputmode")),
    maxlength: element.maxLength > 0 ? element.maxLength : null,
    label: labelOf(element),
    visible: visible(element),
  });
  const buttonInfo = (element) => ({
    text: textOf(element),
    type: short(isButton(element) ? element.type : element.tagName.toLowerCase()),
    target: targetOf(element),
    visible: visible(element),
  });

  const links = [];
  const addLink = (url, text, tag) => {
    if (url && links.length < limits.links) links.push({ url, text: label(text), tag });
  };
  const elementsOf = Object.getOwnPropertyDescriptor(HTMLFormElement.prototype, "elements").get;
  const BUTTONS = "button, input[type=submit], input[type=button], input[type=image], [role=button],"
    + " a[class*=btn], a[class*=button]";

  const forms = [];
  const loose = { action: null, method: "", inputs: [], buttons: [] };
  for (const form of Array.from(document.querySelectorAll("form")).slice(0, limits.forms)) {
    const action = absolute(form.hasAttribute("action") ? form.getAttribute("action") : document.URL);
    const entry = { action, method: short((form.getAttribute("method") || "get").toLowerCase()), inputs: [], buttons: [] };
    for (const element of Array.from(elementsOf.call(form))) {
      if (isInput(element) && entry.inputs.length < limits.inputs) entry.inputs.push(inputInfo(element));
    }
    for (const element of Array.from(form.querySelectorAll(BUTTONS))) {
      if (entry.buttons.length < limits.buttons) entry.buttons.push(buttonInfo(element));
    }
    forms.push(entry);
    addLink(action, "", "form");
  }
  // Inputs outside any form (script-driven pages) are listed under one form without an action.
  for (const element of Array.from(document.querySelectorAll("input, select, textarea"))) {
    if (!element.form && !element.closest("form") && isInput(element)) {
      if (loose.inputs.length >= limits.inputs) break;
      loose.inputs.push(inputInfo(element));
    }
  }
  if (loose.inputs.length) forms.push(loose);

  const buttons = [];
  for (const element of Array.from(document.querySelectorAll(BUTTONS))) {
    const info = buttonInfo(element);
    if (info.target) addLink(info.target, info.text, "button");
    if (!element.closest("form") && buttons.length < limits.buttons) buttons.push(info);
  }
  for (const element of Array.from(document.querySelectorAll("a[href], area[href]")).slice(0, limits.scan)) {
    addLink(absolute(element.getAttribute("href")), textOf(element), element.tagName.toLowerCase());
  }
  for (const element of Array.from(document.querySelectorAll("[onclick], [data-href], [data-url], [data-link]")).slice(0, limits.scan)) {
    const script = scriptTarget(element);
    if (script) addLink(script.url, textOf(element), script.tag);
  }
  for (const element of Array.from(document.querySelectorAll("iframe[src], frame[src]")).slice(0, limits.scan)) {
    addLink(absolute(element.getAttribute("src")), "", element.tagName.toLowerCase());
  }
  for (const element of Array.from(document.querySelectorAll("meta[http-equiv]"))) {
    if ((element.getAttribute("http-equiv") || "").toLowerCase() !== "refresh") continue;
    const found = /url\s*=\s*['"]?([^'"]+)/i.exec(element.getAttribute("content") || "");
    if (found) addLink(absolute(found[1]), "", "meta-refresh");
  }

  return {
    url: cut(location.href, limits.url),
    title: cut(document.title, limits.title),
    text: limits.text > 0 && document.body ? String(document.body.innerText || "").slice(0, limits.text) : "",
    forms,
    buttons,
    links,
  };
}
