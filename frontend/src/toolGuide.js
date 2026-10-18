export const GUIDE_START = "【可用工具】";
export const GUIDE_END = "【可用工具结束】";
export const SKILL_START = "【可用技能】";
export const SKILL_END = "【可用技能结束】";

function schemaFields(tool) {
  const schema = tool?.input_schema || tool?.inputSchema || {};
  const properties = schema.properties || {};
  const required = new Set(schema.required || []);
  return Object.entries(properties).map(([name, spec]) => ({
    name,
    required: required.has(name),
    description: spec?.description || "",
  }));
}

function toolBlock(tool) {
  const detail = tool.detail || {};
  const lines = [`- ${tool.name}：${detail.summary || tool.description || "没有说明"}`];
  for (const param of detail.parameters || []) {
    lines.push(`  - ${param.name}（${param.required ? "必填" : "可选"}）：${param.description || ""}`);
  }
  if (detail.example) lines.push(`  - 示例：${detail.example}`);
  return lines.join("\n");
}

function mcpBlock(server, catalog) {
  if (!catalog || catalog.loading) return `- ${server.name}：正在读取其中的工具说明。`;
  if (catalog.error) return `- ${server.name}：${catalog.error}`;
  const listed = catalog.tools || [];
  if (!listed.length) return `- ${server.name}：没有列出工具。`;
  const tools = listed.map((item) => {
    const lines = [`- ${item.name}：${item.description || "没有说明"}`];
    for (const field of schemaFields(item)) {
      lines.push(`  - ${field.name}（${field.required ? "必填" : "可选"}）：${field.description || ""}`);
    }
    return lines.join("\n");
  });
  return [`服务 ${server.name}`, ...tools].join("\n");
}

export function guideText({ tools, toolNames, servers, mcpIds, catalog }) {
  const chosen = (tools || []).filter((item) => toolNames.includes(item.name));
  const chosenServers = (servers || []).filter((item) => (mcpIds || []).includes(item.id));
  if (!chosen.length && !chosenServers.length) return "";
  const parts = [GUIDE_START, "按下面的说明调用工具。"];
  if (chosen.length) parts.push(chosen.map(toolBlock).join("\n"));
  if (chosenServers.length) parts.push(chosenServers.map((server) => mcpBlock(server, catalog?.[server.id])).join("\n"));
  parts.push(GUIDE_END);
  return parts.join("\n");
}

function marked(source, start, end) {
  const text = source || "";
  const left = text.indexOf(start);
  const right = text.indexOf(end);
  if (left < 0 || right <= left) return "";
  return text.slice(left, right + end.length).trim();
}

function withoutMarked(source, start, end) {
  const text = source || "";
  const left = text.indexOf(start);
  const right = text.indexOf(end);
  if (left < 0 || right <= left) return text;
  return `${text.slice(0, left)}${text.slice(right + end.length)}`;
}

export function skillGuideText(skills, skillIds) {
  const chosen = (skills || []).filter((item) => (skillIds || []).includes(item.id));
  if (!chosen.length) return "";
  const parts = [SKILL_START, "先根据用户要办的事对照下面的「何时使用」。只有匹配时才调用函数 skill_script：skill 填该技能标识，script 填该技能列出的文件名，args 填参数。没有匹配的技能时不要调用，也不要自己猜测脚本名。"];
  for (const skill of chosen) {
    const scripts = (skill.scripts || []).join("、") || "无";
    const lines = [`【${skill.name} / ${skill.key}】`, `何时使用：${skill.description || "没有说明"}`];
    if (skill.body) lines.push(skill.body);
    lines.push(`可用脚本：${scripts}`);
    parts.push(lines.join("\n"));
  }
  parts.push(SKILL_END);
  return parts.join("\n\n");
}

export function applyGuides(text, toolBlock, skillBlock) {
  const base = withoutMarked(withoutMarked(text, GUIDE_START, GUIDE_END), SKILL_START, SKILL_END).trim();
  return [base, toolBlock, skillBlock].filter(Boolean).join("\n\n").trim();
}

export function applyGuide(text, block) {
  return applyGuides(text, block, marked(text, SKILL_START, SKILL_END));
}
