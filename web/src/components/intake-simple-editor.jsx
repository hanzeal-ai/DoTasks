// Adapted from Tiptap's MIT Simple Editor template; see registry/NOTICE.md.
import { useEffect } from 'react';
import { EditorContent, EditorContext, useEditor } from '@tiptap/react';
import { StarterKit } from '@tiptap/starter-kit';
import { Image } from '@tiptap/extension-image';
import { Markdown } from '@tiptap/markdown';
import { Placeholder } from '@tiptap/extensions';
import { Toolbar, ToolbarGroup, ToolbarSeparator } from '@/registry/tiptap-ui-primitive/toolbar';
import { HeadingDropdownMenu } from '@/registry/tiptap-ui/heading-dropdown-menu';
import { ListDropdownMenu } from '@/registry/tiptap-ui/list-dropdown-menu';
import { BlockquoteButton } from '@/registry/tiptap-ui/blockquote-button';
import { CodeBlockButton } from '@/registry/tiptap-ui/code-block-button';
import { LinkPopover } from '@/registry/tiptap-ui/link-popover';
import { MarkButton } from '@/registry/tiptap-ui/mark-button';
import { UndoRedoButton } from '@/registry/tiptap-ui/undo-redo-button';
import { ImageUploadButton } from '@/registry/tiptap-ui/image-upload-button';
import { ImageUploadNode } from '@/registry/tiptap-node/image-upload-node/image-upload-node-extension';
import { imageArtifactId, imageMarkdown, safeLink } from '../intake-content';
import '@/registry/styles/_variables.scss';
import '@/registry/styles/_keyframe-animations.scss';
import '@/registry/tiptap-node/blockquote-node/blockquote-node.scss';
import '@/registry/tiptap-node/code-block-node/code-block-node.scss';
import '@/registry/tiptap-node/list-node/list-node.scss';
import '@/registry/tiptap-node/image-node/image-node.scss';
import '@/registry/tiptap-node/heading-node/heading-node.scss';
import '@/registry/tiptap-node/paragraph-node/paragraph-node.scss';

// Never fetch arbitrary pasted image URLs. Managed images remain ordinary image nodes.
const ManagedImage = Image.extend({
  addInputRules() { return []; },
  parseMarkdown(token, helpers) { return imageArtifactId(token.href || '') ? helpers.createNode('image', { src: token.href, alt: token.text }) : helpers.createTextNode(token.raw || token.text || ''); },
  renderMarkdown(node) { return imageMarkdown(node.attrs.src, node.attrs.alt || '图片').trim(); },
  parseHTML() { return [{ tag: 'img[src]', getAttrs: el => imageArtifactId(el.getAttribute('src') || '') ? null : false }]; },
});

export function IntakeSimpleEditor({ label, editorRef, upload, onError, addFiles }) {
  const editor = useEditor({
    immediatelyRender: false,
    enablePasteRules: false,
    onCreate: ({ editor }) => editor.commands.setTextSelection(1),
    extensions: [
      StarterKit.configure({ underline: false, strike: false, heading: { levels: [1, 2, 3] },
        link: { openOnClick: false, isAllowedUri: safeLink } }),
      Markdown,
      ManagedImage,
      Placeholder.configure({ placeholder: '描述你想完成的内容，粘贴或拖入图片即可上传…' }),
      ImageUploadNode.configure({ accept: 'image/png,image/jpeg,image/gif,image/webp', maxSize: 10 * 1024 * 1024, limit: 8, upload, onError }),
    ],
    content: '',
    editorProps: {
      attributes: { role: 'textbox', 'aria-label': label, 'aria-multiline': 'true', class: 'intake-editor-content simple-editor' },

    },
  });
  useEffect(() => { editorRef.current = editor; return () => { editorRef.current = null; }; }, [editor, editorRef]);
  return <div className="simple-editor-wrapper"><EditorContext.Provider value={{ editor }}>
    <Toolbar aria-label="文本格式">
      <ToolbarGroup><UndoRedoButton action="undo" aria-label="撤销" tooltip="撤销" /><UndoRedoButton action="redo" aria-label="重做" tooltip="重做" /></ToolbarGroup>
      <ToolbarSeparator />
      <ToolbarGroup><HeadingDropdownMenu modal={false} levels={[1, 2, 3]} aria-label="段落格式" tooltip="段落格式" /><ListDropdownMenu modal={false} types={['bulletList', 'orderedList']} aria-label="列表" tooltip="列表" /><BlockquoteButton tooltip="引用" /><CodeBlockButton tooltip="代码块" /></ToolbarGroup>
      <ToolbarSeparator />
      <ToolbarGroup><MarkButton type="bold" aria-label="加粗" tooltip="加粗" /><MarkButton type="italic" aria-label="斜体" tooltip="斜体" /><MarkButton type="code" tooltip="行内代码" /><LinkPopover aria-label="插入链接" tooltip="插入链接" /></ToolbarGroup>
      <ToolbarSeparator />
      <ToolbarGroup><ImageUploadButton text="图片" aria-label="上传图片" tooltip="上传图片" /></ToolbarGroup>
    </Toolbar>
    <EditorContent editor={editor} />
  </EditorContext.Provider></div>;
}
