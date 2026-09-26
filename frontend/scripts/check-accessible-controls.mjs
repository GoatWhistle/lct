import fs from "node:fs";
import path from "node:path";
import ts from "typescript";

const root = path.resolve("src");
const files = [];
function collect(directory) {
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const target = path.join(directory, entry.name);
    if (entry.isDirectory()) collect(target);
    else if (/\.tsx?$/.test(entry.name)) files.push(target);
  }
}
collect(root);

const issues = [];
for (const file of files) {
  const source = fs.readFileSync(file, "utf8");
  const tree = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const labelIds = new Set();
  function findLabels(node) {
    if (ts.isJsxOpeningElement(node) && node.tagName.getText(tree) === "label") {
      const htmlFor = node.attributes.properties.find((item) =>
        ts.isJsxAttribute(item) && item.name.getText(tree) === "htmlFor");
      if (htmlFor?.initializer && ts.isStringLiteral(htmlFor.initializer)) {
        labelIds.add(htmlFor.initializer.text);
      }
    }
    ts.forEachChild(node, findLabels);
  }
  findLabels(tree);

  function inspect(node) {
    let opening = null;
    if (ts.isJsxSelfClosingElement(node)) opening = node;
    else if (ts.isJsxElement(node)) opening = node.openingElement;
    if (opening) {
      const tag = opening.tagName.getText(tree);
      if (["input", "select", "textarea"].includes(tag)) {
        const attrs = opening.attributes.properties.filter(ts.isJsxAttribute);
        const hasAccessibleName = attrs.some((attr) => {
          const name = attr.name.getText(tree);
          if (!["aria-label", "aria-labelledby", "title"].includes(name)) return false;
          return Boolean(attr.initializer && (
            (ts.isStringLiteral(attr.initializer) && attr.initializer.text.trim())
            || ts.isJsxExpression(attr.initializer)
          ));
        });
        const idAttr = attrs.find((attr) => attr.name.getText(tree) === "id");
        const id = idAttr?.initializer && ts.isStringLiteral(idAttr.initializer)
          ? idAttr.initializer.text : null;
        let parent = node.parent;
        let wrappedByLabel = false;
        while (parent) {
          if (ts.isJsxElement(parent) && parent.openingElement.tagName.getText(tree) === "label") {
            wrappedByLabel = true;
            break;
          }
          parent = parent.parent;
        }
        const typeAttr = attrs.find((attr) => attr.name.getText(tree) === "type");
        const hidden = typeAttr?.initializer && ts.isStringLiteral(typeAttr.initializer)
          && typeAttr.initializer.text === "hidden";
        if (!hidden && !hasAccessibleName && !wrappedByLabel && !(id && labelIds.has(id))) {
          const { line } = tree.getLineAndCharacterOfPosition(opening.getStart(tree));
          issues.push(`${path.relative(root, file)}:${line + 1} <${tag}>`);
        }
      }
    }
    ts.forEachChild(node, inspect);
  }
  inspect(tree);
}

if (issues.length) {
  console.error(`Элементы формы без доступного имени (${issues.length}):\n${issues.join("\n")}`);
  process.exitCode = 1;
} else {
  console.log("Доступные имена input/select/textarea: проверены.");
}
