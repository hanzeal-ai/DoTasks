import { useEffect, useRef, useState } from "react";
import { Button } from "./ui/button";
import { IntakeSimpleEditor } from "./intake-simple-editor";
import { intakeDocument, managedImageUrl } from "../intake-content";

const imageTypes = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);
export function editorMarkdown(editor) { return editor?.getMarkdown().trim() || ""; }
function AttachmentPreview({ file }) {
  const [url, setUrl] = useState(null);
  useEffect(() => {
    const next = URL.createObjectURL(file);
    setUrl(next);
    return () => URL.revokeObjectURL(next);
  }, [file]);
  if (!url) return null;
  if (file.type.startsWith("image/")) return <img src={url} alt={file.name} />;
  if (file.type.startsWith("audio/")) return <audio src={url} controls aria-label={file.name} />;
  return null;
}

export function IntakeEditor({ label }) {
  const root = useRef(null);
  const editor = useRef(null);
  const filesRef = useRef([]);
  const uploads = useRef(new Map());
  const generation = useRef(0);
  const [version, setVersion] = useState(0);
  const [files, setFiles] = useState([]);
  const [error, setError] = useState("");
  const updateFiles = next => { filesRef.current = next; setFiles(next); };
  function imageCount() {
    let count = 0;
    editor.current?.state.doc.descendants(node => { if (node.type.name === 'image') count++; });
    return count;
  }
  function checkIncoming(incoming) {
    if (filesRef.current.length + uploads.current.size + imageCount() + incoming.length > 8) throw new Error("最多添加 8 个附件（含正文图片）");
    if (incoming.some(file => !file.size || file.size > 10 * 1024 * 1024)) throw new Error("附件不能为空，每个附件不超过 10 MiB");
    if (incoming.some(file => file.type.startsWith("image/") && !imageTypes.has(file.type))) throw new Error("图片仅支持 PNG、JPEG、GIF、WebP");
  }
  async function uploadImage(file, onProgress, signal) {
    checkIncoming([file]);
    if (!imageTypes.has(file.type)) throw new Error("图片仅支持 PNG、JPEG、GIF、WebP");
    const epoch = generation.current;
    const xhr = new XMLHttpRequest();
    const id = crypto.randomUUID();
    uploads.current.set(id, xhr);
    const cancel = () => xhr.abort();
    signal?.addEventListener('abort', cancel);
    setError("");
    try {
      const bytes = new Uint8Array(await file.arrayBuffer());
      if (signal?.aborted || epoch !== generation.current) throw new Error("上传已取消");
      let binary = "";
      for (let offset = 0; offset < bytes.length; offset += 8192) binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
      const artifact = await new Promise((resolve, reject) => {
        xhr.open("POST", "/api/visual-artifacts");
        xhr.setRequestHeader("Content-Type", "application/json");
        xhr.timeout = 60000;
        xhr.upload.onprogress = event => { if (event.lengthComputable) onProgress?.({ progress: Math.round(event.loaded / event.total * 100) }); };
        xhr.onload = () => {
          try {
            const value = JSON.parse(xhr.responseText);
            if (xhr.status < 200 || xhr.status >= 300) throw new Error(value.error || "图片上传失败");
            resolve(value);
          } catch (failure) { reject(failure); }
        };
        xhr.onerror = () => reject(new Error("网络异常，图片上传失败"));
        xhr.ontimeout = () => reject(new Error("图片上传超时"));
        xhr.onabort = () => reject(new Error("上传已取消"));
        xhr.send(JSON.stringify({ filename: file.name, content_base64: btoa(binary), purpose: "正文图片" }));
      });
      if (signal?.aborted || epoch !== generation.current) throw new Error("上传已取消");
      const url = managedImageUrl(artifact.artifact_id);
      setError("");
      return url;
    } finally { uploads.current.delete(id); signal?.removeEventListener('abort', cancel); }
  }
  const addFiles = incoming => {
    try {
      checkIncoming(incoming);
      setError("");
      updateFiles([...filesRef.current, ...incoming.filter(file => !imageTypes.has(file.type))]);
      const images = incoming.filter(file => imageTypes.has(file.type));
      if (images.length) editor.current.chain().focus().insertContentAt(editor.current.state.selection.to, { type: 'imageUpload', attrs: { files: images } }).run();
    } catch (failure) { setError(failure.message); }
  };
  useEffect(() => {
    const form = root.current.closest("form");
    const collect = event => {
      if (event.target !== form) return;
      try {
        let pending = false;
        editor.current?.state.doc.descendants(node => { if (node.type.name === 'imageUpload') pending = true; });
        if (!editor.current || pending || uploads.current.size) throw new Error("请完成图片上传，或移除未完成的图片上传块");
        const markdown = editorMarkdown(editor.current);
        const { plain, images } = intakeDocument(markdown);
        if (imageCount() + filesRef.current.length > 8) throw new Error("最多添加 8 个附件（含正文图片）");
        const names = [...images.map(image => image.filename), ...filesRef.current.map(file => file.name)];
        event.formData.set("goal", markdown || (names.length ? `请根据附件完成：${names.join("、")}` : ""));
        event.formData.set("title", (plain.split(/\n/)[0] || names[0] || "").slice(0, 120));
        event.formData.set("uploaded_visual_references", JSON.stringify(images));
        for (const file of filesRef.current) event.formData.append("visual_references", file);
      } catch (failure) { event.formData.set("intake_error", failure.message); setError(failure.message); }
    };
    const cancelUploads = () => {
      generation.current++;
      for (const xhr of uploads.current.values()) xhr.abort();
      uploads.current.clear();
    };
    const reset = event => {
      if (event.target !== form) return;
      cancelUploads(); updateFiles([]); setError(""); setVersion(generation.current);
    };
    form.addEventListener("formdata", collect); form.addEventListener("reset", reset);
    return () => { cancelUploads(); form.removeEventListener("formdata", collect); form.removeEventListener("reset", reset); };
  }, []);
  return <section ref={root} className="intake-editor" aria-label={label}
    onPasteCapture={event => { if (event.clipboardData.files.length) { event.preventDefault(); event.stopPropagation(); addFiles([...event.clipboardData.files]); } }}
    onDragOver={event => { if (event.dataTransfer.types.includes('Files')) event.preventDefault(); }}
    onDropCapture={event => { if (event.dataTransfer.files.length && !event.target.closest('.tiptap-image-upload')) { event.preventDefault(); event.stopPropagation(); addFiles([...event.dataTransfer.files]); } }}>

    <IntakeSimpleEditor key={version} label={label} editorRef={editor} upload={uploadImage} onError={failure => setError(failure.message)} addFiles={addFiles} />
    <div className="intake-file-actions">
      {[["文件", undefined], ["录音", "audio/*"]].map(([name, accept]) => <label key={name} className="intake-upload">＋ 添加{name}
        <input type="file" aria-label={`上传${name}`} accept={accept} multiple onChange={event => { addFiles([...event.target.files]); event.target.value = ""; }} />
      </label>)}<span>图片直接插入正文</span>
    </div>
    {files.length > 0 && <ul className="intake-attachments">{files.map((file, index) => <li key={`${index}-${file.name}`}>
      <div><span>{file.name}</span><small>{Math.ceil(file.size / 1024)} KiB</small>
        <Button type="button" variant="ghost" aria-label={`移除 ${file.name}`} onClick={() => updateFiles(filesRef.current.filter((_, i) => i !== index))}>移除</Button></div>
      <AttachmentPreview file={file} />
    </li>)}</ul>}
    <small className="intake-editor-hint">最多 8 个附件（含正文图片），每个不超过 10 MiB；首行内容作为标题。</small>
    {error && <p role="alert">{error}</p>}
  </section>;
}
