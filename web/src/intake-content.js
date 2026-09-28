import { fromMarkdown } from "mdast-util-from-markdown";
import { toMarkdown } from "mdast-util-to-markdown";

const artifactPattern = /^artifact:\/\/visuals\/[a-f0-9]{64}\.(png|jpg|gif|webp)$/;
const imageRoute = "/api/visual-artifacts/content?artifact_id=";

export function managedImageUrl(artifactId) {
  if (!artifactPattern.test(artifactId)) throw new Error("图片上传返回了无效引用");
  return imageRoute + encodeURIComponent(artifactId);
}

export function imageArtifactId(url) {
  if (!url.startsWith(imageRoute)) return null;
  try {
    const id = decodeURIComponent(url.slice(imageRoute.length));
    return artifactPattern.test(id) && managedImageUrl(id) === url ? id : null;
  } catch { return null; }
}

export function safeLink(url) {
  return /^(https?:\/\/|mailto:)/i.test(url) || /^\/(?!\/)/.test(url) || /^#/.test(url);
}

export function imageMarkdown(url, name) {
  return toMarkdown({ type: "root", children: [{ type: "paragraph", children: [{ type: "image", url, alt: name }] }] });
}

// Read the mature Markdown AST, not editor DOM or a second rich-text serializer.
export function intakeDocument(markdown) {
  const tree = fromMarkdown(markdown);
  const images = new Map();
  function text(node) {
    if (node.type === "html") throw new Error("请移除 HTML 内容，使用编辑器格式工具");
    if (node.type === "imageReference") throw new Error("请使用上传图片功能插入图片");
    if (node.type === "image") {
      const id = imageArtifactId(node.url);
      if (!id) throw new Error("仅支持上传到本应用的图片，请移除外部图片后重新上传");
      images.set(id, { artifact_id: id, filename: node.alt || "图片" });
      return "";
    }
    if (["link", "definition"].includes(node.type) && !safeLink(node.url)) throw new Error("链接地址不安全");
    if (node.type === "break") return "\n";
    if (node.type === "definition") return "";
    if (typeof node.value === "string") return node.value;
    const separator = ["root", "list", "listItem", "blockquote"].includes(node.type) ? "\n" : "";
    return (node.children || []).map(text).join(separator);
  }
  const plain = text(tree).trim();
  return { plain, images: [...images.values()] };
}
