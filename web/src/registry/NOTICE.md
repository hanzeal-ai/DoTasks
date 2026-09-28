# Tiptap Simple Editor UI (MIT)

Source: https://tiptap.dev/docs/ui-components/templates/simple-editor
Registry: https://template.tiptap.dev/api/registry/components/
Retrieved: 2026-09-28. Original file SHA-256 hashes and component names are recorded in upstream-manifest.json. License copied from https://github.com/ueberdosis/tiptap-ui-components/blob/main/LICENSE; see LICENSE.

These are official source components, not a independently designed toolbar. The application composition is ../components/intake-simple-editor.jsx. Imports preserve the registry layout. Only the dependency closure for the enabled Markdown-safe toolbar is included. Font/color/alignment and other non-Markdown controls are not enabled, rather than silently discarding their output. Global demo layout, remote fonts and demo content are not imported.

Local changes:
- button/button.tsx: default type=button for safe embedding in existing forms.
- image-upload-node: real upload callback, transient pasted/dropped files, cancellation on unmount, batch cancellation and partial-success preservation, retry/removal, accessible controls and Chinese upload messages. Standard upstream upload node layout retained.
- lib/tiptap-utils.ts: removed fake/demo upload implementation.
- styles/_variables.scss: only theme variables retained from global root styles.

Do not overwrite the local upload lifecycle/security adaptations when updating upstream. Re-run browser and real proxy integration tests after updates.
