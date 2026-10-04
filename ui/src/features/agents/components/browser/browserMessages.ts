export type BrowserLiveRecord = Record<string, unknown>

export interface BrowserPage {
  url: string
  title: string
  loading: boolean
}

/** The page's size in CSS pixels and the display's size in device pixels. */
export interface BrowserGeometry {
  cssWidth: number
  cssHeight: number
  width: number
  height: number
  scale: number
}

export function isRecord(value: unknown): value is BrowserLiveRecord {
  return typeof value === "object" && value !== null
}

export function parseScreen(
  message: BrowserLiveRecord
): BrowserGeometry | null {
  const { width, height, scale } = message
  if (
    typeof width === "number" &&
    typeof height === "number" &&
    typeof scale === "number" &&
    scale > 0
  )
    return {
      width,
      height,
      scale,
      cssWidth: width / scale,
      cssHeight: height / scale,
    }
  return null
}

export function parsePage(message: BrowserLiveRecord): BrowserPage | null {
  const { url, title, loading } = message
  if (
    typeof url === "string" &&
    typeof title === "string" &&
    typeof loading === "boolean"
  )
    return { url, title, loading }
  return null
}

const CURSOR_OP = 0x02
const CURSOR_HEADER_BYTES = 3 + 8

/**
 * A CSS `cursor` value for neko's cursor-image data channel message: a one-byte
 * opcode, a two-byte length, four 16-bit fields (width, height, hotspot x and y),
 * then a PNG, in device pixels.
 */
export function cursorStyle(data: ArrayBuffer, scale: number): string | null {
  if (data.byteLength <= CURSOR_HEADER_BYTES || !(scale > 0)) return null
  const view = new DataView(data)
  if (view.getUint8(0) !== CURSOR_OP) return null
  const hotX = view.getUint16(7)
  const hotY = view.getUint16(9)
  const png = new Uint8Array(data, CURSOR_HEADER_BYTES)
  const hotspot = `${Math.round(hotX / scale)} ${Math.round(hotY / scale)}`
  return `image-set(url("data:image/png;base64,${bytesToBase64(png)}") ${scale}x) ${hotspot}, default`
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = ""
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary)
}
