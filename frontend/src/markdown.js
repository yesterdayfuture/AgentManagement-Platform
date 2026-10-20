import katex from "katex";

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function renderTex(tex, displayMode) {
  try {
    return katex.renderToString(tex.trim(), {
      displayMode,
      throwOnError: false,
      strict: "ignore",
      trust: false,
    });
  } catch {
    return `<code>${escapeHtml(tex.trim())}</code>`;
  }
}

function inline(value) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

function table(rows) {
  const cells = rows.map((row) => row.replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim()));
  const body = cells.filter((row) => !row.every((cell) => /^:?-+:?$/.test(cell)));
  if (!body.length) return "";
  const head = body[0].map((cell) => `<th>${inline(cell)}</th>`).join("");
  const rest = body.slice(1).map((row) => `<tr>${row.map((cell) => `<td>${inline(cell)}</td>`).join("")}</tr>`).join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${rest}</tbody></table>`;
}

function restore(html, slots) {
  return html.replace(/\uE000(\d+)\uE001/g, (_, index) => slots[Number(index)] || "");
}

function extractMath(source) {
  const slots = [];
  const stash = (html) => {
    const key = `\uE000${slots.length}\uE001`;
    slots.push(html);
    return key;
  };
  let text = source;

  // Keep fenced code away from math parsing.
  text = text.replace(/```([^\n`]*)\n([\s\S]*?)```/g, (_, lang, code) =>
    stash(`<pre><code>${escapeHtml(code.replace(/\n$/, ""))}</code></pre>`),
  );

  text = text.replace(/\$\$([\s\S]+?)\$\$/g, (_, tex) => stash(renderTex(tex, true)));
  text = text.replace(/\\\[([\s\S]+?)\\\]/g, (_, tex) => stash(renderTex(tex, true)));
  text = text.replace(/\\\(([\s\S]+?)\\\)/g, (_, tex) => stash(renderTex(tex, false)));
  text = text.replace(/(?<![\\$])\$([^$\n]+?)\$(?!\$)/g, (_, tex) => stash(renderTex(tex, false)));

  return { text, slots };
}

function renderBlocks(text) {
  const blocks = [];
  const lines = text.split("\n");
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    if (/^---+$/.test(line.trim())) {
      blocks.push("<hr>");
      index += 1;
      continue;
    }
    if (line.trim().startsWith("|")) {
      const rows = [];
      while (index < lines.length && lines[index].trim().startsWith("|")) {
        rows.push(lines[index].trim());
        index += 1;
      }
      blocks.push(table(rows));
      continue;
    }
    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    if (heading) {
      const level = heading[1].length;
      blocks.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      const items = [];
      while (index < lines.length && /^\s*[-*]\s+/.test(lines[index])) {
        items.push(`<li>${inline(lines[index].replace(/^\s*[-*]\s+/, ""))}</li>`);
        index += 1;
      }
      blocks.push(`<ul>${items.join("")}</ul>`);
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      const items = [];
      while (index < lines.length && /^\s*\d+\.\s+/.test(lines[index])) {
        items.push(`<li>${inline(lines[index].replace(/^\s*\d+\.\s+/, ""))}</li>`);
        index += 1;
      }
      blocks.push(`<ol>${items.join("")}</ol>`);
      continue;
    }
    if (line.includes("\uE000") && !line.replace(/\uE000\d+\uE001/g, "").trim()) {
      blocks.push(line.trim());
      index += 1;
      continue;
    }
    const paragraph = [];
    while (
      index < lines.length &&
      lines[index].trim() &&
      !/^---+$/.test(lines[index].trim()) &&
      !lines[index].trim().startsWith("|") &&
      !/^#{1,3}\s+/.test(lines[index]) &&
      !/^\s*[-*]\s+/.test(lines[index]) &&
      !/^\s*\d+\.\s+/.test(lines[index])
    ) {
      const current = lines[index];
      if (current.includes("\uE000") && !current.replace(/\uE000\d+\uE001/g, "").trim()) {
        break;
      }
      paragraph.push(current);
      index += 1;
    }
    if (paragraph.length) {
      blocks.push(`<p>${inline(paragraph.join("\n")).replace(/\n/g, "<br>")}</p>`);
    }
  }
  return blocks.join("");
}

export function renderMarkdown(source) {
  const { text, slots } = extractMath(String(source || "").replace(/\r\n/g, "\n"));
  return restore(renderBlocks(text), slots);
}
