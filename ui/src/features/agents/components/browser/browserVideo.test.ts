import { describe, expect, it } from "vitest"

import { parseVideoMessage } from "@/features/agents/components/browser/browserVideo"

describe("a live view video message", () => {
  it("carries the key frame flag and then the frame", () => {
    const key = parseVideoMessage(new Uint8Array([1, 0, 0, 1, 9]).buffer)
    expect(key?.key).toBe(true)
    expect([...(key?.data ?? [])]).toEqual([0, 0, 1, 9])
    expect(parseVideoMessage(new Uint8Array([0, 7]).buffer)?.key).toBe(false)
  })

  it("ignores a message with no frame", () => {
    expect(parseVideoMessage(new Uint8Array([1]).buffer)).toBeNull()
  })
})
