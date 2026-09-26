import { useEffect, useRef, useState } from "react";
import { Button } from "./ui/button";

// Store Markdown in the existing goal contract; never persist or render pasted HTML.
export function editorMarkdown(node) {
  if (node.nodeType === 3) return node.textContent.replace(/([\\`*_\[\]])/g, "\\$1");
  const content = [...node.childNodes].map(editorMarkdown).join("");
  switch (node.nodeName) {
    case "B": case "STRONG": return `**${content}**`;
    case "I": case "EM": return `_${content}_`;
    case "BR": return "\n";
    case "LI": return `- ${content.trim()}\n`;
    case "DIV": case "P": case "UL": return `\n${content}\n`;
    default: return content;
  }
}

function AttachmentPreview({ file }) {
  const [url, setUrl] = useState("");
  useEffect(() => {
    const next = URL.createObjectURL(file);
    setUrl(next);
    return () => URL.revokeObjectURL(next);
  }, [file]);
  if (file.type.startsWith("image/")) return <img src={url} alt={file.name} />;
  if (file.type.startsWith("audio/")) return <audio src={url} controls aria-label={file.name} />;
  return null;
}

export function IntakeEditor({ label }) {
  const root = useRef(null);
  const editor = useRef(null);
  const selection = useRef(null);
  const [files, setFiles] = useState([]);
  const [error, setError] = useState("");
  useEffect(() => {
    const form = root.current.closest("form");
    const collect = event => {
      const plain = editor.current.innerText.trim();
      const goal = plain ? editorMarkdown(editor.current).trim() : "";
      const title = plain.split(/\n/)[0] || files[0]?.name || "";
      event.formData.set("goal", goal || (files.length ? `请根据附件完成：${files.map(file => file.name).join("、")}` : ""));
      event.formData.set("title", title.slice(0, 120));
      for (const file of files) event.formData.append("visual_references", file);
    };
    const reset = () => { editor.current.replaceChildren(); setFiles([]); setError(""); selection.current = null; };
    form.addEventListener("formdata", collect);
    form.addEventListener("reset", reset);
    return () => { form.removeEventListener("formdata", collect); form.removeEventListener("reset", reset); };
  }, [files]);
  const addFiles = incoming => {
    const next = [...files, ...incoming];
    if (next.length > 8) { setError("最多添加 8 个附件"); return; }
    if (next.some(file => !file.size || file.size > 10 * 1024 * 1024)) {
      setError("附件不能为空，每个附件不超过 10 MiB"); return;
    }
    setFiles(next); setError("");
  };
  const rememberSelection = () => {
    const current = window.getSelection();
    if (current.rangeCount && editor.current.contains(current.anchorNode)) selection.current = current.getRangeAt(0).cloneRange();
  };
  const format = command => {
    editor.current.focus();
    if (selection.current) { const current = window.getSelection(); current.removeAllRanges(); current.addRange(selection.current); }
    document.execCommand(command);
    rememberSelection();
  };
  return <section ref={root} className="intake-editor" aria-label={label}>
    <div className="intake-editor-toolbar" role="toolbar" aria-label="内容格式与附件">
      <Button type="button" variant="ghost" onClick={() => format("bold")} aria-label="加粗">加粗</Button>
      <Button type="button" variant="ghost" onClick={() => format("italic")} aria-label="斜体">斜体</Button>
      <Button type="button" variant="ghost" onClick={() => format("insertUnorderedList")} aria-label="项目列表">列表</Button>
      {[["文件", undefined], ["图片", "image/png,image/jpeg,image/gif,image/webp"], ["录音", "audio/*"]].map(([name, accept]) =>
        <label key={name} className="intake-upload">上传{name}<input type="file" aria-label={`上传${name}`} accept={accept} multiple
          onChange={event => { addFiles([...event.target.files]); event.target.value = ""; }} /></label>)}
    </div>
    <div ref={editor} className="intake-editor-content" contentEditable suppressContentEditableWarning role="textbox"
      aria-label={label} aria-multiline="true" data-placeholder="描述你想完成的内容，也可以添加文件、图片或录音…"
      onKeyUp={rememberSelection} onMouseUp={rememberSelection} onInput={rememberSelection}
      onPaste={event => {
        event.preventDefault();
        if (event.clipboardData.files.length) addFiles([...event.clipboardData.files]);
        const text = event.clipboardData.getData("text/plain");
        if (text) document.execCommand("insertText", false, text);
      }} onDrop={event => { event.preventDefault(); if (event.dataTransfer.files.length) addFiles([...event.dataTransfer.files]); }}
      onDragOver={event => event.preventDefault()} />
    {files.length > 0 && <ul className="intake-attachments">{files.map((file, index) => <li key={`${index}-${file.name}`}>
      <div><span>{file.name}</span><small>{Math.ceil(file.size / 1024)} KiB</small>
        <Button type="button" variant="ghost" aria-label={`移除 ${file.name}`} onClick={() => setFiles(files.filter((_, i) => i !== index))}>移除</Button></div>
      <AttachmentPreview file={file} />
    </li>)}</ul>}
    <small className="intake-editor-hint">最多 8 个附件，每个不超过 10 MiB；首行内容作为标题。</small>
    {error && <p role="alert">{error}</p>}
  </section>;
}
