# Icon attribution

The SVG icons in this folder (and the extension-to-icon mapping in
`../file-icon-ext-map.json`, used as a fallback for extensions not bundled
here) are derived from the **vscode-icons** project:

- Project: https://github.com/vscode-icons/vscode-icons
- Version bundled from: v12.19.0 (`icons/file_type_*.svg`)
- Copyright (c) vscode-icons project contributors

## License

Unlike the rest of this codebase, these icon files are **not** MIT-licensed.
Per the vscode-icons project's own README:

- The vscode-icons **source code** is MIT-licensed.
- The vscode-icons **icons** (what's actually bundled in this folder) are
  licensed under **Creative Commons Attribution-ShareAlike 4.0 International
  (CC BY-SA 4.0)**: https://creativecommons.org/licenses/by-sa/4.0/
- Some individual icons represent **branded/trademarked logos** (specific
  languages, frameworks, and tools) and are licensed under their own
  respective owners' terms *in addition to* the above — CC BY-SA doesn't
  necessarily cover trademark use on its own.

CC BY-SA is a share-alike (copyleft) license: redistributing this folder
requires attribution (this file), and any modified/derivative versions of
*these icon files specifically* must be distributed under the same CC BY-SA
4.0 terms. This obligation applies only to the contents of this
`static/file-icons/` folder and `file-icon-ext-map.json` — it does not
extend to the rest of this project's own codebase.

## Modifications

No modifications were made to the individual icon files themselves. They
were selected and renamed (from `file_type_<name>.svg` to `<extension>.svg`,
one copy per matched file extension) via an extension-to-icon mapping
derived from vscode-icons' own `src/iconsManifest/supportedExtensions.ts`
and `src/iconsManifest/languages.ts` source files.
