import { EventType, IncrementalSource, NodeType } from "@rrweb/types"
import type {
  addedNodeMutation,
  eventWithTime,
  serializedNodeWithId,
} from "@rrweb/types"

import {
  mapFamilyList,
  splitFamilyList,
  webFamilyName,
} from "@/features/agents/components/browser/mirror/fontCatalog"
import type { FontCatalog } from "@/features/agents/components/browser/mirror/fontCatalog"

/** What the agent's browser reports for each media feature a page may query. */
const MEDIA_FEATURES: Record<string, string> = {
  "prefers-color-scheme": "light",
  "prefers-reduced-motion": "no-preference",
  "prefers-contrast": "no-preference",
  "prefers-reduced-transparency": "no-preference",
  "prefers-reduced-data": "no-preference",
  "forced-colors": "none",
  "inverted-colors": "none",
}
const MEDIA_TRUE = "(min-width: 0px)"
const MEDIA_FALSE = "(max-width: -1px)"
const MEDIA_FEATURE_PATTERN = new RegExp(
  `\\(\\s*(${Object.keys(MEDIA_FEATURES).join("|")})\\s*(?::\\s*([a-z-]+))?\\s*\\)`,
  "gi"
)

const URL_ATTRIBUTES = new Set([
  "src",
  "poster",
  "data",
  "background",
  "manifest",
  "icon",
  "xlink:href",
])
const HREF_LOADING_TAGS = new Set(["link", "image", "use", "feimage"])
const NO_NAVIGATION_ATTRIBUTES = new Set(["action", "formaction", "ping"])
const PASSIVE_LINK_RELATIONS =
  /\b(preload|prefetch|dns-prefetch|preconnect|modulepreload|prerender)\b/i
const SCRIPT_URL_ATTRIBUTES = new Set([
  "href",
  "src",
  "data",
  "action",
  "formaction",
  "xlink:href",
  "poster",
  "background",
  "cite",
  "longdesc",
  "usemap",
])
const SCRIPT_URL = /^[\s\p{Cc}]*javascript:/iu
const KEPT_URL_PREFIXES = ["data:", "blob:", "about:", "#"]
const URL_IN_CSS = /url\(\s*(?:"([^"]*)"|'([^']*)'|([^)\s'"]*))\s*\)/gi
const IMPORT_IN_CSS = /@import\s+(["'])([^"']+)\1/gi
const FONT_FACE_BLOCK = /@font-face\s*\{[^}]*\}/gi
const FONT_FAMILY_DECLARATION = /((?<![\w-])font-family\s*:\s*)([^;}!]+)/gi
const FONT_VARIABLE_DECLARATION =
  /(--[\w-]*(?:font|typeface)[\w-]*\s*:\s*)([^;}!]+)/gi
const COLOR_SCHEME_DECLARATION = /((?<![\w-])color-scheme\s*:\s*)([^;}!]+)/gi
const FONT_FACE_FAMILY = /(font-family\s*:\s*)([^;}]+)/i
const FONT_FACE_LOCAL_SOURCE = /local\(\s*(?:"[^"]*"|'[^']*'|[^)]*)\)\s*,?\s*/gi
const FONT_SHORTHAND_DECLARATION = /((?<![\w-])font\s*:\s*)([^;}!]+)/gi
const FONT_SIZE_TOKEN =
  /^(?:xx-small|x-small|small|medium|large|x-large|xx-large|xxx-large|larger|smaller|[\d.]+(?:[a-z%]+)?)(?:\/\S+)?$/i

export interface RewriteOptions {
  /** The origin-preserving prefix every page resource is loaded from, ending in a slash. */
  assetBase: string
  catalog: FontCatalog | null
}

type NodeIdTags = Map<number, string>

function drop<T>(object: Record<string, T>, key: string): void {
  delete object[key]
}

/** Rewrites rrweb events so a replay shows the agent's page, loads only through the asset route, and runs nothing. */
export class EventRewriter {
  private baseHref = "about:blank"
  private readonly tags: NodeIdTags = new Map()
  private readonly styleTextIds = new Set<number>()

  constructor(private options: RewriteOptions) {}

  reset(): void {
    this.baseHref = "about:blank"
    this.tags.clear()
    this.styleTextIds.clear()
  }

  setCatalog(catalog: FontCatalog | null): void {
    this.options = { ...this.options, catalog }
  }

  setAssetBase(assetBase: string): void {
    this.options = { ...this.options, assetBase }
  }

  rewrite(event: eventWithTime): eventWithTime {
    switch (event.type) {
      case EventType.Meta:
        this.baseHref = event.data.href || this.baseHref
        return event
      case EventType.FullSnapshot:
        this.tags.clear()
        this.styleTextIds.clear()
        this.node(event.data.node, false)
        return event
      case EventType.IncrementalSnapshot:
        this.incremental(event.data)
        return event
      default:
        return event
    }
  }

  assetUrl(absolute: string): string | null {
    let url: URL
    try {
      url = new URL(absolute, this.baseHref)
    } catch {
      return null
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") return null
    const scheme = url.protocol.slice(0, -1)
    return `${this.options.assetBase}${scheme}/${url.host}${url.pathname}${url.search}`
  }

  private url(value: string): string {
    const text = value.trim()
    if (!text) return value
    const lowered = text.toLowerCase()
    if (KEPT_URL_PREFIXES.some((prefix) => lowered.startsWith(prefix)))
      return value
    return this.assetUrl(text) ?? "about:blank"
  }

  private srcset(value: string): string {
    const out: string[] = []
    let rest = value
    while (rest.trim()) {
      rest = rest.replace(/^[\s,]+/, "")
      const match = /^(\S+)/.exec(rest)
      if (!match?.[1]) break
      let candidate = match[1]
      rest = rest.slice(candidate.length)
      let descriptor = ""
      if (candidate.endsWith(",")) candidate = candidate.replace(/,+$/, "")
      else {
        const end = rest.indexOf(",")
        descriptor = end === -1 ? rest : rest.slice(0, end)
        rest = end === -1 ? "" : rest.slice(end + 1)
      }
      out.push(
        `${this.url(candidate)}${descriptor.trim() ? ` ${descriptor.trim()}` : ""}`
      )
    }
    return out.join(", ")
  }

  private media(value: string): string {
    return value.replaceAll(
      MEDIA_FEATURE_PATTERN,
      (_match, feature: string, wanted: string | undefined) => {
        const actual = MEDIA_FEATURES[feature.toLowerCase()]
        const matches =
          wanted === undefined
            ? actual !== "no-preference" && actual !== "none"
            : wanted.toLowerCase() === actual
        return matches ? MEDIA_TRUE : MEDIA_FALSE
      }
    )
  }

  private colorScheme(value: string): string {
    const words = value.toLowerCase().split(/\s+/).filter(Boolean)
    if (words.length === 0 || words.includes("normal")) return value
    return words.includes("light") || !words.includes("dark") ? "light" : "dark"
  }

  private fontFace(block: string): string {
    const withoutLocal = block.replace(FONT_FACE_LOCAL_SOURCE, "")
    const renamed = withoutLocal.replace(
      FONT_FACE_FAMILY,
      (_match, head: string, family: string) =>
        `${head}"${webFamilyName(family.trim().replaceAll(/^["']|["']$/g, ""))}"`
    )
    return renamed
  }

  private urls(text: string): string {
    return text
      .replaceAll(
        URL_IN_CSS,
        (_match, double?: string, single?: string, bare?: string) =>
          `url("${this.url(double ?? single ?? bare ?? "")}")`
      )
      .replaceAll(
        IMPORT_IN_CSS,
        (_match, quoteChar: string, target: string) =>
          `@import ${quoteChar}${this.url(target)}${quoteChar}`
      )
  }

  /** CSS text with its URLs routed, its media queries pinned, and its fonts mapped. */
  css(text: string): string {
    const fonts = this.options.catalog ? this.fontFamilies(text) : text
    const schemes = fonts.replaceAll(
      COLOR_SCHEME_DECLARATION,
      (_match, head: string, value: string) =>
        `${head}${this.colorScheme(value)}`
    )
    return this.media(this.urls(schemes))
  }

  private fontFamilies(text: string): string {
    const parts: string[] = []
    let last = 0
    for (const block of text.matchAll(FONT_FACE_BLOCK)) {
      const start = block.index ?? 0
      parts.push(this.fontDeclarations(text.slice(last, start)))
      parts.push(this.fontFace(block[0]))
      last = start + block[0].length
    }
    parts.push(this.fontDeclarations(text.slice(last)))
    return parts.join("")
  }

  private fontDeclarations(text: string): string {
    const catalog = this.options.catalog
    return text
      .replaceAll(
        FONT_FAMILY_DECLARATION,
        (_match, head: string, list: string) =>
          `${head}${mapFamilyList(list, catalog)}`
      )
      .replaceAll(
        FONT_VARIABLE_DECLARATION,
        (match, head: string, list: string) =>
          /[,"']/.test(list) ||
          /\b(?:serif|sans-serif|monospace|system-ui)\b/i.test(list)
            ? `${head}${mapFamilyList(list, catalog)}`
            : match
      )
      .replaceAll(
        FONT_SHORTHAND_DECLARATION,
        (match, head: string, value: string) =>
          this.fontShorthand(match, head, value)
      )
  }

  private fontShorthand(match: string, head: string, value: string): string {
    if (/var\(|^\s*(?:inherit|initial|unset|revert)/i.test(value)) return match
    const tokens = value.trim().split(/\s+/)
    const sizeAt = tokens.findIndex((token) => FONT_SIZE_TOKEN.test(token))
    if (sizeAt === -1) return match
    const prefix = tokens.slice(0, sizeAt + 1).join(" ")
    const list = tokens.slice(sizeAt + 1).join(" ")
    if (!list) return match
    return `${head}${prefix} ${mapFamilyList(list, this.options.catalog)}`
  }

  private declaration(property: string, value: string): string {
    const name = property.toLowerCase()
    if (this.options.catalog && name === "font-family")
      return mapFamilyList(value, this.options.catalog)
    if (this.options.catalog && name === "font")
      return this.fontShorthand(value, "", value)
    if (name === "color-scheme") return this.colorScheme(value)
    return this.css(value)
  }

  private attributes(
    tagName: string,
    attributes: Record<string, unknown>
  ): void {
    const tag = tagName.toLowerCase()
    for (const name of Object.keys(attributes)) {
      const value = attributes[name]
      if (typeof value !== "string") continue
      const key = name.toLowerCase()
      if (
        key.startsWith("on") ||
        key === "srcdoc" ||
        (SCRIPT_URL_ATTRIBUTES.has(key) && SCRIPT_URL.test(value))
      )
        drop(attributes, name)
      else if (NO_NAVIGATION_ATTRIBUTES.has(key)) drop(attributes, name)
      else if (tag === "script" && key === "src") drop(attributes, name)
      else if ((tag === "iframe" || tag === "frame") && key === "src")
        drop(attributes, name)
      else if (tag === "base" && key === "href") drop(attributes, name)
      else if (tag === "meta" && key === "http-equiv") drop(attributes, name)
      else if (
        (tag === "object" || tag === "embed") &&
        (key === "data" || key === "src")
      )
        drop(attributes, name)
      else if (key === "srcset" || key === "imagesrcset")
        attributes[name] = this.srcset(value)
      else if (URL_ATTRIBUTES.has(key)) attributes[name] = this.url(value)
      else if (key === "href" && HREF_LOADING_TAGS.has(tag)) {
        const relation =
          typeof attributes.rel === "string" ? attributes.rel : ""
        if (tag === "link" && PASSIVE_LINK_RELATIONS.test(relation))
          drop(attributes, name)
        else attributes[name] = this.url(value)
      } else if (key === "style" || key === "_csstext")
        attributes[name] = this.css(value)
      else if (key === "media") attributes[name] = this.media(value)
      else if (
        key === "content" &&
        tag === "meta" &&
        attributes.name === "color-scheme"
      )
        attributes[name] = this.colorScheme(value)
      else if (key.startsWith("--") && /font|typeface/i.test(key))
        attributes[name] = mapFamilyList(value, this.options.catalog)
    }
  }

  private node(node: serializedNodeWithId, insideStyle: boolean): void {
    switch (node.type) {
      case NodeType.Document:
        for (const child of node.childNodes) this.node(child, false)
        return
      case NodeType.Element: {
        const tag = node.tagName.toLowerCase()
        this.tags.set(node.id, tag)
        this.attributes(node.tagName, node.attributes)
        if (tag === "canvas") {
          node.tagName = "img"
          node.childNodes = []
          this.tags.set(node.id, "img")
          return
        }
        node.childNodes = node.childNodes.filter(
          (child) =>
            child.type !== NodeType.Element ||
            child.tagName.toLowerCase() !== "script"
        )
        for (const child of node.childNodes) this.node(child, tag === "style")
        return
      }
      case NodeType.Text:
        if (insideStyle || node.isStyle) {
          this.styleTextIds.add(node.id)
          node.textContent = this.css(node.textContent)
        }
        return
      default:
        return
    }
  }

  private added(adds: addedNodeMutation[]): void {
    for (const add of adds) {
      const parent = this.tags.get(add.parentId)
      this.node(add.node, parent === "style")
    }
  }

  private styleObject(values: Record<string, unknown>): void {
    for (const [property, value] of Object.entries(values)) {
      if (typeof value === "string")
        values[property] = this.declaration(property, value)
      else if (Array.isArray(value) && typeof value[0] === "string")
        value[0] = this.declaration(property, value[0])
    }
  }

  private incremental(
    data: Extract<
      eventWithTime,
      { type: EventType.IncrementalSnapshot }
    >["data"]
  ): void {
    switch (data.source) {
      case IncrementalSource.Mutation:
        this.added(data.adds)
        for (const change of data.attributes) {
          const tag = this.tags.get(change.id) ?? ""
          const style = change.attributes.style
          if (style && typeof style === "object") this.styleObject(style)
          this.attributes(tag, change.attributes)
        }
        for (const change of data.texts)
          if (change.value !== null && this.styleTextIds.has(change.id))
            change.value = this.css(change.value)
        return
      case IncrementalSource.StyleSheetRule:
        for (const add of data.adds ?? []) add.rule = this.css(add.rule)
        if (data.replace !== undefined) data.replace = this.css(data.replace)
        if (data.replaceSync !== undefined)
          data.replaceSync = this.css(data.replaceSync)
        return
      case IncrementalSource.StyleDeclaration:
        if (data.set?.value)
          data.set.value = this.declaration(data.set.property, data.set.value)
        return
      case IncrementalSource.AdoptedStyleSheet:
        for (const sheet of data.styles ?? [])
          for (const rule of sheet.rules) rule.rule = this.css(rule.rule)
        return
      case IncrementalSource.Font:
        data.family = webFamilyName(data.family)
        if (!data.buffer) data.fontSource = this.urls(data.fontSource)
        return
      default:
        return
    }
  }
}

export { splitFamilyList }
