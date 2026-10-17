# katet brand assets

| File | Use |
| --- | --- |
| `katet-wordmark.svg` / `katet-wordmark-dark.svg` | Logo for light / dark backgrounds |
| `katet-mark.svg` / `katet-mark-dark.svg` | The `k·` mark on its own |
| `katet-app-icon.svg`, `katet-app-icon-1024.png` | macOS app icon source (Apple icon grid: 824 px body on a 1024 canvas) |
| `menubar-idle.svg`, `menubar-attention.svg` | Menu-bar template sources (the dot appears when an agent needs you) |
| `social-preview.png` | GitHub social preview, 1280×640 (Settings › Social preview) |
| `build_brand.py` | Regenerates every asset; dev-only, not part of katet's runtime |

Colors: ink `#15171C`, paper `#F5F6F8`, amber `#D07A1F`.

Type: the wordmark is Bricolage Grotesque ExtraBold, converted to outlines, so no font is needed to display it. Both fonts used here (Bricolage Grotesque, JetBrains Mono) are under the SIL Open Font License 1.1.

The `·` in the wordmark is always the amber dot, never a typed character. In the menu bar, the dot only shows when an agent is waiting for a human.

## In the project README

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/brand/katet-wordmark-dark.svg">
  <img alt="katet" src="assets/brand/katet-wordmark.svg" height="64">
</picture>
```
