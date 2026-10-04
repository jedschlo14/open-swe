export interface FontFaceInfo {
  id: string
  weight: string
  style: string
  stretch: string
  ranges: string | null
}

/** The fonts the agent's browser has, as the sandbox reports them. */
export interface FontCatalog {
  families: Record<string, FontFaceInfo[]>
  /** A lowercase family name a page may use, mapped to the installed family the browser resolves it to. */
  names: Record<string, string>
  /** A generic family keyword, mapped to the installed family the browser uses for it. */
  generics: Record<string, string>
  /** Installed families in the order the browser falls back to them for characters a font lacks. */
  fallbacks: string[]
}

const FAMILY_PREFIX = "ows-f-"
const WEB_PREFIX = "ows-w-"
const CSS_WIDE_KEYWORDS = new Set([
  "inherit",
  "initial",
  "unset",
  "revert",
  "revert-layer",
])
const GENERIC_ALIASES: Record<string, string> = {
  "ui-sans-serif": "sans-serif",
  "ui-serif": "serif",
  "ui-monospace": "monospace",
  "ui-rounded": "sans-serif",
  "-apple-system": "system-ui",
  blinkmacsystemfont: "system-ui",
  fangsong: "serif",
  math: "serif",
  emoji: "sans-serif",
}
const GENERICS = new Set([
  "serif",
  "sans-serif",
  "monospace",
  "cursive",
  "fantasy",
  "system-ui",
])

function quote(name: string): string {
  return `"${name.replaceAll("\\", "\\\\").replaceAll('"', '\\"')}"`
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function stringRecord(value: unknown): Record<string, string> {
  const out: Record<string, string> = {}
  if (isRecord(value))
    for (const [key, item] of Object.entries(value))
      if (typeof item === "string") out[key] = item
  return out
}

function parseFaces(value: unknown): FontFaceInfo[] {
  if (!Array.isArray(value)) return []
  const faces: FontFaceInfo[] = []
  for (const item of value) {
    if (!isRecord(item)) continue
    const { id, weight, style, stretch, ranges } = item
    if (
      typeof id === "string" &&
      typeof weight === "string" &&
      typeof style === "string" &&
      typeof stretch === "string"
    )
      faces.push({
        id,
        weight,
        style,
        stretch,
        ranges: typeof ranges === "string" ? ranges : null,
      })
  }
  return faces
}

export function parseFontCatalog(
  message: Record<string, unknown>
): FontCatalog {
  const families: Record<string, FontFaceInfo[]> = {}
  if (isRecord(message.families))
    for (const [name, faces] of Object.entries(message.families))
      families[name] = parseFaces(faces)
  const fallbacks = Array.isArray(message.fallbacks)
    ? message.fallbacks.filter(
        (item): item is string => typeof item === "string"
      )
    : []
  return {
    families,
    names: stringRecord(message.names),
    generics: stringRecord(message.generics),
    fallbacks,
  }
}

export function splitFamilyList(list: string): string[] {
  const items: string[] = []
  let current = ""
  let quoteChar = ""
  let depth = 0
  for (const char of list) {
    if (quoteChar) {
      current += char
      if (char === quoteChar) quoteChar = ""
      continue
    }
    if (char === '"' || char === "'") {
      quoteChar = char
      current += char
    } else if (char === "(") {
      depth += 1
      current += char
    } else if (char === ")") {
      depth = Math.max(0, depth - 1)
      current += char
    } else if (char === "," && depth === 0) {
      items.push(current)
      current = ""
    } else current += char
  }
  if (current.trim()) items.push(current)
  return items
}

function unquote(token: string): string {
  const text = token.trim()
  const first = text[0]
  if (
    (first === '"' || first === "'") &&
    text.endsWith(first) &&
    text.length > 1
  )
    return text.slice(1, -1).replaceAll(/\\(.)/g, "$1")
  return text.replaceAll(/\s+/g, " ")
}

export function webFamilyName(name: string): string {
  return `${WEB_PREFIX}${name.trim().toLowerCase()}`
}

/** A page's font-family list as the agent's browser resolves it, so the replay uses the same fonts. */
export function mapFamilyList(
  list: string,
  catalog: FontCatalog | null
): string {
  const trimmed = list.trim()
  if (!trimmed || CSS_WIDE_KEYWORDS.has(trimmed.toLowerCase())) return list
  const mapped: string[] = []
  for (const token of splitFamilyList(trimmed)) {
    const raw = token.trim()
    if (raw.includes("(")) {
      mapped.push(raw)
      continue
    }
    const name = unquote(raw)
    const key = name.toLowerCase()
    const generic = GENERICS.has(key) ? key : GENERIC_ALIASES[key]
    if (generic !== undefined) {
      const installed = catalog?.generics[generic]
      if (installed !== undefined) mapped.push(quote(FAMILY_PREFIX + installed))
      continue
    }
    mapped.push(quote(webFamilyName(name)))
    const installed = catalog?.names[key]
    if (installed !== undefined) mapped.push(quote(FAMILY_PREFIX + installed))
  }
  if (catalog)
    for (const family of catalog.fallbacks)
      mapped.push(quote(FAMILY_PREFIX + family))
  return mapped.join(", ")
}

/** `@font-face` rules that give the replay the agent's installed fonts, loaded from ``assetUrl``. */
export function installedFontCss(
  catalog: FontCatalog,
  assetUrl: (id: string) => string
): string {
  const rules: string[] = []
  for (const [family, faces] of Object.entries(catalog.families))
    for (const face of faces) {
      const ranges = face.ranges ? `unicode-range:${face.ranges};` : ""
      rules.push(
        `@font-face{font-family:${quote(FAMILY_PREFIX + family)};src:url(${quote(
          assetUrl(face.id)
        )});font-weight:${face.weight};font-style:${face.style};font-stretch:${face.stretch};${ranges}font-display:block}`
      )
    }
  return rules.join("\n")
}
