import { describe, expect, it } from "vitest"

import { fitViewport } from "@/features/agents/components/browser/browserViewport"

describe("fitViewport", () => {
  it("uses the panel's size when it is within the supported range", () => {
    expect(fitViewport({ width: 1200, height: 800 })).toEqual({
      width: 1200,
      height: 800,
    })
  })

  it("scales a narrow panel up to a usable layout, keeping its aspect ratio", () => {
    expect(fitViewport({ width: 400, height: 700 })).toEqual({
      width: 800,
      height: 1200,
    })
  })

  it("scales a very large panel down to the supported maximum", () => {
    expect(fitViewport({ width: 3840, height: 1920 })).toEqual({
      width: 1920,
      height: 960,
    })
  })

  it("snaps to a step so a drag does not resize the page per pixel", () => {
    expect(fitViewport({ width: 1001, height: 703 })).toEqual(
      fitViewport({ width: 1005, height: 699 })
    )
  })

  it("returns null for a hidden or collapsed panel", () => {
    expect(fitViewport({ width: 0, height: 0 })).toBeNull()
    expect(fitViewport({ width: 900, height: 10 })).toBeNull()
  })
})
