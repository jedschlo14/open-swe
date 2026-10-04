import { describe, expect, it } from "vitest"

import {
  cursorStyle,
  parseScreen,
} from "@/features/agents/components/browser/browserMessages"

function cursorMessage(op: number, png: number[]): ArrayBuffer {
  const bytes = new Uint8Array(11 + png.length)
  const view = new DataView(bytes.buffer)
  view.setUint8(0, op)
  view.setUint16(1, 8 + png.length)
  view.setUint16(3, 24)
  view.setUint16(5, 24)
  view.setUint16(7, 6)
  view.setUint16(9, 3)
  bytes.set(png, 11)
  return bytes.buffer
}

describe("neko's cursor image message", () => {
  it("becomes a CSS cursor at the page's render scale", () => {
    const style = cursorStyle(cursorMessage(0x02, [1, 2, 3]), 1.5)
    expect(style).toBe(
      `image-set(url("data:image/png;base64,${btoa("\u0001\u0002\u0003")}") 1.5x) 4 2, default`
    )
  })

  it("ignores cursor positions and messages with no image", () => {
    expect(cursorStyle(cursorMessage(0x01, [1]), 1.5)).toBeNull()
    expect(cursorStyle(cursorMessage(0x02, []), 1.5)).toBeNull()
  })
})

describe("the display size message", () => {
  it("gives the page's CSS size from the display's pixels", () => {
    expect(
      parseScreen({ type: "screen", width: 1440, height: 900, scale: 1.5 })
    ).toEqual({
      width: 1440,
      height: 900,
      scale: 1.5,
      cssWidth: 960,
      cssHeight: 600,
    })
    expect(parseScreen({ width: 1440, height: 900, scale: 0 })).toBeNull()
  })
})
